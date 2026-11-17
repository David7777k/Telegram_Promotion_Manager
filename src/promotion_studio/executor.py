"""Execute one explicitly requested publication through an injected transport."""
import json
from typing import Protocol
from dataclasses import dataclass
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .job_queue import QueueConflict, claim_due, finish, mark_dispatch, pause_claim, reject
from .photos import load_photo
from .policies import Consent, Offer, publication_issues
from .security import SecretError
from .schedule_time import WeeklyWindow


@dataclass(frozen=True)
class Access:
    chat_id: int
    chat_type: str
    telegram_bot_id: int
    can_publish: bool
    can_publish_photo: bool = False


class Publisher(Protocol):
    def inspect(self, account, chat_id, thread_id) -> Access: ...

    def send(self, account, chat_id, thread_id, text, button_url, *, photo=None) -> int: ...


class TransportUnavailable(RuntimeError):
    """Transport could not verify rights or return a reliable response."""


class PreflightDenied(ValueError):
    pass


class TransportRejected(RuntimeError):
    def __init__(self, *, rate_limited=False, retry_after=None, status=None):
        super().__init__("Telegram rejected the request")
        self.reason = ("telegram_rate_limited" if rate_limited else
                       {401: "telegram_unauthorized", 403: "telegram_forbidden"}.get(status, "telegram_rejected"))
        self.retry_after = retry_after


def _active_consent(db, permission, venue, chat, bot, thread, now):
    row = db.execute("""SELECT valid_from,expires_at,timezone_name,allows_photo FROM venue_permissions
                        WHERE id=? AND venue_id=? AND chat_id=? AND bot_account_id=?
                          AND thread_id IS ? AND valid_from<=? AND revoked_at IS NULL
                          AND (expires_at IS NULL OR expires_at>?)""",
                     (permission, venue, chat, bot, thread, now, now)).fetchone()
    if row is None:
        raise PreflightDenied("missing_consent")
    return row


def _utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc)


def _allowed_now(db, permission, zone_name, now):
    rows = db.execute("""SELECT weekday,start_minute,end_minute
                         FROM venue_permission_windows WHERE permission_id=?""", (permission,)).fetchall()
    if not rows and zone_name is None:
        return True
    if not rows or not zone_name:
        raise PreflightDenied("invalid_calendar")
    try:
        zone = timezone.utc if zone_name == "UTC" else ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise PreflightDenied("timezone_unavailable") from None
    return any(WeeklyWindow(day, time(*divmod(start, 60)), time(*divmod(end, 60))).contains(_utc(now), zone)
               for day, start, end in rows)


def execute_one(db, protector, transport: Publisher, clock, *, job_id=None, auto_only=False, stop=None):
    """Return (job ID, state), or None when no job is ready.

    The transport implements inspect(bot_account_id, chat_id, thread_id) and
    send(bot_account_id, chat_id, thread_id, text, button_url).
    """
    if stop is not None and stop.is_set():
        return None
    claim = claim_due(db, clock(), job_id=job_id, auto_only=auto_only)
    if claim is None:
        return None
    job, lease = claim
    venue, chat, bot, price, client_key, encrypted, cooldown = db.execute(
        """SELECT venue_id,canonical_chat_id,bot_account_id,price_at_approval,client_key,payload,cooldown_seconds
           FROM jobs WHERE id=?""", (job,)).fetchone()
    try:
        snapshot = json.loads(protector.unprotect(encrypted))
        permission = snapshot["permission_id"]
        thread = snapshot.get("thread_id")
        text, button = snapshot["text"], snapshot.get("button_url")
        plain = snapshot.get("kind") == "plain" and snapshot["offer"] is None
        offer = None if plain else Offer(**snapshot["offer"])
        if (snapshot["client_key"] != client_key or not isinstance(permission, str) or not isinstance(text, str)
                or (thread is not None and (type(thread) is not int or thread <= 0))
                or (button is not None and not isinstance(button, str))
                or (offer.price_stars if offer else None) != price):
            raise PreflightDenied("invalid_snapshot")
        text.encode("utf-8")
        photo_digest = snapshot.get("photo_digest")
        photo = load_photo(db, protector, photo_digest) if photo_digest is not None else None
    except (SecretError, ValueError, TypeError, KeyError, AttributeError):
        pause_claim(db, job, lease, clock(), "invalid_snapshot")
        return job, "paused"

    try:
        _active_consent(db, permission, venue, chat, bot, thread, clock())
        try:
            access = transport.inspect(bot, chat, thread)
        except (TransportRejected, TransportUnavailable, QueueConflict, SecretError):
            raise
        except Exception:
            raise TransportUnavailable("Provider inspection failed") from None
        now = clock()

        def check():
            if stop is not None and stop.is_set():
                raise PreflightDenied("operator_stopped")
            valid_from, expires, zone_name, allows_photo = _active_consent(
                db, permission, venue, chat, bot, thread, now)
            account = db.execute("""SELECT telegram_user_id FROM accounts
                                    WHERE id=? AND disabled_at IS NULL""", (bot,)).fetchone()
            if db.execute("""SELECT 1 FROM venue_directory WHERE venue_id=? AND
                             (excluded OR decision='denied' OR (? AND decision!='allowed'))""",
                          (venue, snapshot.get("kind") in ("plain", "subscription"))).fetchone():
                raise PreflightDenied("missing_consent")
            if (account is None or type(account[0]) is not int or account[0] <= 0
                    or not isinstance(access, Access) or access.chat_id != chat
                    or account[0] != access.telegram_bot_id):
                raise PreflightDenied("identity_mismatch")
            if photo is not None:
                if allows_photo != 1:
                    raise PreflightDenied("missing_photo_consent")
                if access.can_publish_photo is not True:
                    raise PreflightDenied("missing_photo_rights")
            if db.execute("""SELECT 1 FROM jobs WHERE id<>? AND
                             (canonical_chat_id=? OR (canonical_chat_id IS NULL AND venue_id=?))
                             AND (state='needs_review' OR
                                 (state='sent' AND ?-sent_at<max(?,cooldown_seconds)))
                             LIMIT 1""", (job, chat, venue, now, cooldown)).fetchone():
                raise PreflightDenied("conflicting_publication")
            issues = publication_issues(
                chat_id=chat, chat_type=access.chat_type, bot_id=account[0],
                can_publish=access.can_publish, when=_utc(now), offer=offer, preview=text,
                button_url=button, thread_id=thread,
                in_allowed_window=_allowed_now(db, permission, zone_name, now),
                has_photo=photo is not None, allow_plain=plain,
                consent=Consent(chat, account[0], _utc(valid_from),
                                _utc(expires) if expires is not None else None, thread))
            if issues:
                raise PreflightDenied(issues[0])

        mark_dispatch(db, job, lease, now, check=check)
    except TransportRejected as error:
        reject(db, job, lease, clock(), error.reason, retry_after=error.retry_after)
        return job, "paused"
    except (PreflightDenied, TransportUnavailable, QueueConflict, SecretError) as error:
        reason = str(error) if isinstance(error, PreflightDenied) else "rights_unavailable"
        pause_claim(db, job, lease, clock(), reason)
        return job, "paused"

    try:
        if stop is not None and stop.is_set():
            reject(db, job, lease, clock(), "operator_stopped")
            return job, "paused"
        options = {"photo": photo} if photo is not None else {}
        receipt = transport.send(bot, chat, thread, text, button, **options)
    except TransportRejected as error:
        reject(db, job, lease, clock(), error.reason, retry_after=error.retry_after)
        return job, "paused"
    except TransportUnavailable:
        receipt = None
    except Exception:
        receipt = None
    if type(receipt) is not int or receipt <= 0:
        receipt = None
    finish(db, job, lease, clock(), receipt=receipt)
    return job, "sent" if receipt is not None else "needs_review"

