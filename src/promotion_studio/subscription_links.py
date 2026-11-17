"""Durable paid invite creation and manual reconciliation."""
import json
import re
from hashlib import sha256

from .executor import TransportRejected, TransportUnavailable
from .job_queue import QueueConflict, record_bot_hold, transaction
from .security import SecretError

PERIOD_SECONDS = 30 * 24 * 60 * 60


def _receipt_link(receipt, bot_id, name, price):
    if not isinstance(receipt, dict):
        raise ValueError("Invalid subscription receipt")
    link = receipt.get("invite_link")
    creator = receipt.get("creator")
    if (not isinstance(link, str) or not re.fullmatch(r"https://t\.me/(?:\+|joinchat/)[A-Za-z0-9_-]+", link)
            or not isinstance(creator, dict) or creator.get("id") != bot_id
            or receipt.get("name") != name or receipt.get("is_revoked") is not False
            or type(receipt.get("subscription_price")) is not int or receipt["subscription_price"] != price
            or type(receipt.get("subscription_period")) is not int
            or receipt["subscription_period"] != PERIOD_SECONDS):
        raise ValueError("Subscription receipt does not match the request")
    return link


def create_subscription(db, protector, transport, clock, *, request_id, campaign_id, bot_account_id,
                        chat_id, price, content_confirmed):
    """Create one paid invite. Only call from a confirmed operator action."""
    if (content_confirmed is not True or not isinstance(request_id, str) or not 1 <= len(request_id) <= 128
            or type(chat_id) is not int or chat_id >= 0 or type(price) is not int or not 1 <= price <= 10000):
        raise ValueError("Invalid or unconfirmed subscription request")
    name = "TPS-" + sha256(request_id.encode("utf-8")).hexdigest()[:24]
    identity = (campaign_id, bot_account_id, chat_id, price)
    with transaction(db):
        existing = db.execute("""SELECT campaign_id,bot_account_id,chat_id,price,state
                                  FROM subscription_requests WHERE id=?""", (request_id,)).fetchone()
        if existing is not None:
            if existing[:4] != identity:
                raise QueueConflict("Request ID already belongs to another subscription")
            return "needs_review" if existing[4] == "pending" else existing[4]
        if db.execute("""SELECT 1 FROM subscription_requests WHERE campaign_id=? AND chat_id=?
                         AND state IN ('pending','needs_review','created')""", (campaign_id, chat_id)).fetchone():
            raise QueueConflict("An existing subscription request must be reconciled first")
        campaign = db.execute("SELECT payload FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if campaign is None:
            raise QueueConflict("Campaign not found")
        terms = json.loads(protector.unprotect(campaign[0]))
        if (str(terms.get("channel_chat_id")) != str(chat_id) or terms.get("price") != price
                or not isinstance(terms.get("description"), str) or not terms["description"].strip()):
            raise QueueConflict("Campaign needs a confirmed channel, price and content description")
        now = clock()
        if db.execute("""SELECT 1 FROM bot_holds WHERE bot_account_id=?
                         AND (until_utc IS NULL OR until_utc>?)""", (bot_account_id, now)).fetchone():
            raise QueueConflict("Telegram hold is still active")
        account = db.execute("""SELECT telegram_user_id FROM accounts
                                WHERE id=? AND kind='bot' AND disabled_at IS NULL""", (bot_account_id,)).fetchone()
        if account is None or type(account[0]) is not int or account[0] <= 0:
            raise QueueConflict("A verified bot is required")
        bot_id = account[0]
        db.execute("""INSERT INTO subscription_requests(id,campaign_id,bot_account_id,chat_id,telegram_bot_id,
                         price,name,state,started_at) VALUES (?,?,?,?,?,?,?,'pending',?)""",
                   (request_id, campaign_id, bot_account_id, chat_id, bot_id, price, name, now))

    encrypted = None
    try:
        receipt = transport.create_paid_invite(bot_account_id, chat_id, bot_id, name, price, PERIOD_SECONDS)
        link = _receipt_link(receipt, bot_id, name, price)
        encrypted = protector.protect(link.encode("ascii"))
        state = "created"
        rejection = None
    except TransportRejected as error:
        state, rejection = "rejected", error
    except (TransportUnavailable, SecretError, ValueError):
        state, rejection = "needs_review", None
    with transaction(db):
        now = clock()
        cursor = db.execute("""UPDATE subscription_requests SET state=?,resolved_at=?,ciphertext=?
                               WHERE id=? AND state='pending'""", (state, now, encrypted, request_id))
        if cursor.rowcount != 1:
            raise QueueConflict("Subscription request is no longer pending")
        if rejection is not None and rejection.reason == "telegram_rate_limited":
            record_bot_hold(db, bot_account_id, now, rejection.retry_after)
        elif rejection is not None and rejection.reason == "telegram_unauthorized":
            db.execute("UPDATE accounts SET disabled_at=?,status='unauthorized' WHERE id=?",
                       (now, bot_account_id))
    return state


def reconcile_subscription(db, protector, request_id, now, *, receipt=None, verified_chat_id=None):
    """Record an invite found in Telegram or close a checked, unresolved request."""
    with transaction(db):
        row = db.execute("""SELECT telegram_bot_id,name,price,chat_id FROM subscription_requests WHERE id=?
                            AND state IN ('pending','needs_review')""", (request_id,)).fetchone()
        if row is None:
            raise QueueConflict("Subscription request does not require review")
        if receipt is None:
            state, ciphertext = "closed", None
        else:
            if verified_chat_id != row[3]:
                raise QueueConflict("Checked channel does not match the request")
            link = _receipt_link(receipt, *row[:3])
            state, ciphertext = "created", protector.protect(link.encode("ascii"))
        db.execute("UPDATE subscription_requests SET state=?,resolved_at=?,ciphertext=? WHERE id=?",
                   (state, now, ciphertext, request_id))
        db.execute("""INSERT INTO audit_events(at,level,event_code,entity_type,entity_id,details_json)
                      VALUES (?,'info','manual_subscription_reconciliation','subscription',?,?)""",
                   (now, request_id, json.dumps({"state": state})))
        return state


def read_subscription_link(db, protector, request_id):
    row = db.execute("SELECT ciphertext FROM subscription_requests WHERE id=? AND state='created'",
                     (request_id,)).fetchone()
    return protector.unprotect(row[0]).decode("ascii") if row else None

