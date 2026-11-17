"""Local account and venue directory."""
import json
from uuid import uuid4

from .job_queue import QueueConflict, record_bot_hold, transaction
from .operator_workflow import _packed
from .telegram_accounts import Chat


def account_rows(db):
    rows = db.execute("""SELECT a.id,a.kind,a.label,a.username,a.status,a.last_sync_at,a.avatar,
                           h.recorded_at,h.until_utc,
                           COALESCE(a.dialog_count,(SELECT count(*) FROM venue_access v WHERE v.account_id=a.id))
                           FROM accounts a LEFT JOIN bot_holds h ON h.bot_account_id=a.id
                           WHERE a.telegram_user_id IS NOT NULL ORDER BY a.label,a.id""")
    keys = ("id", "kind", "name", "username", "status", "synced", "avatar", "held", "until", "venues")
    return [dict(zip(keys, row)) for row in rows]


def store_user(db, secrets, identity, api_id, api_hash, session):
    with transaction(db):
        existing = db.execute("SELECT id FROM accounts WHERE kind='user' AND telegram_user_id=?",
                              (identity.telegram_id,)).fetchone()
        account = existing[0] if existing else uuid4().hex
        db.execute("""INSERT INTO accounts(id,kind,telegram_user_id,label,username,status,api_id,avatar)
                   VALUES (?,'user',?,?,?,'ready',?,?) ON CONFLICT(id) DO UPDATE SET label=excluded.label,
                   username=excluded.username,api_id=excluded.api_id,avatar=excluded.avatar,
                   disabled_at=NULL,status='ready'""",
                   (account, identity.telegram_id, identity.name, identity.username, api_id, identity.avatar))
        secrets.put(account, "api_hash", api_hash.encode("ascii"))
        secrets.put(account, "mtproto_session", session.encode("ascii"))
    return account


def disable(db, secrets, account, now, *, erase=False):
    with transaction(db):
        if not db.execute("UPDATE accounts SET status=?,disabled_at=? WHERE id=?",
                          ("removed" if erase else "disabled", now, account)).rowcount:
            raise QueueConflict("Аккаунт не найден.")
        if erase:
            for kind in ("api_hash", "mtproto_session", "bot_token"):
                secrets.delete(account, kind)
            db.execute("UPDATE accounts SET api_id=NULL,avatar=NULL WHERE id=?", (account,))
        db.execute("UPDATE jobs SET state='paused' WHERE state='queued' AND bot_account_id=?", (account,))


def check_hold(db, account, now):
    if db.execute("""SELECT 1 FROM bot_holds WHERE bot_account_id=?
                     AND (until_utc IS NULL OR until_utc>?)""", (account, now)).fetchone():
        raise QueueConflict("Для отправителя действует ограничение Telegram.")


def record_rejection(db, account, error, now):
    with transaction(db):
        if error.reason == "telegram_rate_limited":
            record_bot_hold(db, account, now, error.retry_after)
        elif error.reason == "telegram_unauthorized":
            db.execute("UPDATE accounts SET status='unauthorized',disabled_at=? WHERE id=?", (now, account))
            db.execute("UPDATE jobs SET state='paused' WHERE state='queued' AND bot_account_id=?", (account,))


def save_chat(db, protector, chat, now, *, category=""):
    with transaction(db):
        row = db.execute("SELECT id FROM venues WHERE canonical_chat_id=?", (chat.id,)).fetchone()
        venue = row[0] if row else uuid4().hex
        if not row:
            run = db.execute("""INSERT INTO import_runs(source_kind,sha256,source_zone,run_at)
                              VALUES ('catalog',?,'UTC',?)""", (venue, now)).lastrowid
            db.execute("""INSERT INTO venues(id,run_id,consent,payload,canonical_chat_id)
                        VALUES (?,?,'recheck',?,?)""",
                       (venue, run, _packed(protector, {"chat_id": chat.id, "name": chat.title}), chat.id))
        db.execute("""INSERT INTO venue_directory(venue_id,title,username,kind,participants,category,checked_at)
                    VALUES (?,?,?,?,?,?,?) ON CONFLICT(venue_id) DO UPDATE SET
                    title=COALESCE(NULLIF(excluded.title,''),venue_directory.title),
                    username=COALESCE(excluded.username,venue_directory.username),
                    kind=COALESCE(excluded.kind,venue_directory.kind),
                    participants=COALESCE(excluded.participants,venue_directory.participants),
                    category=COALESCE(NULLIF(excluded.category,''),venue_directory.category),
                    checked_at=excluded.checked_at""",
                   (venue, chat.title, chat.username, chat.kind, chat.participants, category, now))
    return venue


def observe(db, venue, account, access, now):
    db.execute("""INSERT INTO venue_access VALUES (?,?,?,?,?) ON CONFLICT(venue_id,account_id)
                DO UPDATE SET can_text=excluded.can_text,can_photo=excluded.can_photo,checked_at=excluded.checked_at""",
               (venue, account, -1 if access is None else int(access.can_publish),
                -1 if access is None else int(access.can_publish_photo), now))


def venue_rows(db, protector):
    rows = db.execute("""SELECT v.id,v.canonical_chat_id,v.payload,d.title,d.username,d.kind,
                         d.participants,d.category,d.favorite,d.excluded,d.decision,
                         (SELECT max(j.sent_at) FROM jobs j WHERE j.venue_id=v.id)
                         FROM venues v LEFT JOIN venue_directory d ON d.venue_id=v.id
                         WHERE v.canonical_chat_id IS NOT NULL""").fetchall()
    result = []
    for venue, chat, payload, title, username, kind, count, category, favorite, excluded, decision, sent in rows:
        if not title or category is None:
            data = json.loads(protector.unprotect(payload))
            title = title or data.get("name") or str(chat)
            category = data.get("category", "") if category is None else category
        access = list(db.execute("""SELECT a.id,a.label,v.can_text,v.can_photo FROM venue_access v
                           JOIN accounts a ON a.id=v.account_id WHERE v.venue_id=? AND a.disabled_at IS NULL
                           ORDER BY a.label""", (venue,)))
        result.append(dict(id=venue, chat=chat, title=title, username=username, kind=kind,
                           count=count, category=category, favorite=bool(favorite), excluded=bool(excluded),
                           decision=decision or "review", sent=sent, access=access))
    return sorted(result, key=lambda row: (not row["favorite"], row["title"].casefold(), row["chat"]))


def set_venue(db, venue, *, decision, category, favorite, excluded, now):
    if decision not in ("review", "allowed", "denied"):
        raise ValueError("Неизвестное решение по размещению.")
    with transaction(db):
        db.execute("""INSERT INTO venue_directory(venue_id,title,category,favorite,excluded,decision,checked_at)
                      VALUES (?,'',?,?,?,?,?) ON CONFLICT(venue_id) DO UPDATE SET
                      category=excluded.category,favorite=excluded.favorite,
                      excluded=excluded.excluded,decision=excluded.decision""",
                   (venue, category, favorite, excluded, decision, now))
        if excluded or decision != "allowed":
            db.execute("UPDATE venue_permissions SET revoked_at=? WHERE venue_id=? AND revoked_at IS NULL",
                       (now, venue))
            db.execute("UPDATE jobs SET state='paused' WHERE venue_id=? AND state='queued'", (venue,))


def resolve(publisher, account, reference):
    if hasattr(publisher, "resolve"):
        from .executor import TransportUnavailable
        chat = publisher.resolve(reference)
        if chat is None:
            raise TransportUnavailable("Ссылка должна вести на канал или группу.")
        return chat
    identifier, title, kind = publisher.lookup_chat(account, reference)
    return Chat(identifier, title, reference.lstrip("@") if isinstance(reference, str) else None, kind)
