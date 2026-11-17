"""Durable scheduling transitions. No network I/O takes place here."""
from contextlib import contextmanager
import json
from uuid import uuid4


class QueueConflict(RuntimeError):
    pass


@contextmanager
def transaction(db):
    if db.in_transaction:
        savepoint = "nested_" + uuid4().hex
        db.execute(f"SAVEPOINT {savepoint}")
        try:
            yield
        except BaseException:
            db.execute(f"ROLLBACK TO {savepoint}")
            raise
        finally:
            db.execute(f"RELEASE {savepoint}")
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def reserve(db, *, client_key, campaign_id, venue_id, template_id, chat_id, bot_id,
            scheduled, cooldown, encrypted_payload, price, auto_approved=False):
    """Persist an approved, encrypted snapshot. Call only after domain preflight."""
    if (not client_key or type(chat_id) is not int or chat_id >= 0
            or type(cooldown) is not int or cooldown < 0
            or (price is not None and (type(price) is not int or not 1 <= price <= 10000))
            or not isinstance(encrypted_payload, bytes) or not encrypted_payload):
        raise ValueError("Invalid reservation")
    identity = (campaign_id, venue_id, template_id, chat_id, bot_id, scheduled, price)
    with transaction(db):
        existing = db.execute("""SELECT id,campaign_id,venue_id,template_id,canonical_chat_id,
                                  bot_account_id,scheduled_utc,price_at_approval,payload,cooldown_seconds,auto_approved
                                  FROM jobs WHERE client_key=?""", (client_key,)).fetchone()
        if existing is not None:
            if existing[1:] != (*identity, encrypted_payload, cooldown, int(auto_approved)):
                raise QueueConflict("This request key already identifies another publication")
            return existing[0]
        conflict = db.execute("""SELECT 1 FROM jobs WHERE (canonical_chat_id=? OR
                                  (canonical_chat_id IS NULL AND venue_id=?)) AND (
                                    state='needs_review' OR
                                    (state IN ('queued','claimed','dispatching','paused') AND
                                        (scheduled_utc=? OR abs(scheduled_utc-?)<max(?,cooldown_seconds))) OR
                                    (state='sent' AND ?>=sent_at AND ?-sent_at<max(?,cooldown_seconds))
                                 ) LIMIT 1""",
                              (chat_id, venue_id, scheduled, scheduled, cooldown,
                               scheduled, scheduled, cooldown)).fetchone()
        if conflict:
            raise QueueConflict("The destination already has a conflicting publication")
        job_id = uuid4().hex
        db.execute("""INSERT INTO jobs(id,client_key,campaign_id,venue_id,template_id,canonical_chat_id,
                         bot_account_id,scheduled_utc,price_at_approval,payload,cooldown_seconds,auto_approved,state)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'queued')""",
                   (job_id, client_key, *identity, encrypted_payload, cooldown, int(auto_approved)))
        return job_id


def claim_due(db, now, lease_seconds=60, *, job_id=None, auto_only=False):
    if lease_seconds <= 0:
        raise ValueError("Expected a positive lease duration")
    with transaction(db):
        db.execute("UPDATE jobs SET state='paused' WHERE state='queued' AND scheduled_utc<?",
                   (now - 3600,))
        if db.execute("SELECT 1 FROM jobs WHERE state IN ('claimed','dispatching') LIMIT 1").fetchone():
            return None
        row = db.execute("""SELECT id FROM jobs WHERE state='queued' AND scheduled_utc<=?
                            AND (? IS NULL OR id=?) AND (NOT ? OR auto_approved=1)
                            AND EXISTS (SELECT 1 FROM accounts WHERE id=jobs.bot_account_id
                                        AND kind IN ('user','bot') AND disabled_at IS NULL)
                            AND NOT EXISTS (SELECT 1 FROM bot_holds WHERE bot_account_id=jobs.bot_account_id
                                            AND (until_utc IS NULL OR until_utc>?))
                            AND (NOT ? OR NOT EXISTS (SELECT 1 FROM jobs AS prior
                                 WHERE prior.canonical_chat_id=jobs.canonical_chat_id AND prior.state='sent'
                                 AND prior.sent_at+max(prior.cooldown_seconds,jobs.cooldown_seconds)>?))
                            ORDER BY scheduled_utc,id LIMIT 1""",
                         (now, job_id, job_id, auto_only, now, auto_only, now)).fetchone()
        if row is None:
            return None
        job_id = row[0]
        lease = uuid4().hex
        db.execute("UPDATE jobs SET state='claimed',lease_id=?,lease_until=? WHERE id=?",
                   (lease, now + lease_seconds, job_id))
        db.execute("INSERT INTO job_attempts(lease_id,job_id,claimed_at) VALUES (?,?,?)",
                   (lease, job_id, now))
        return job_id, lease


def mark_dispatch(db, job_id, lease, now, *, check=None):
    with transaction(db):
        if check is not None:
            check()
        cursor = db.execute("""UPDATE jobs SET state='dispatching'
                               WHERE id=? AND state='claimed' AND lease_id=? AND lease_until>?""",
                            (job_id, lease, now))
        if cursor.rowcount != 1:
            raise QueueConflict("The claim is missing, stale or expired")
        db.execute("UPDATE job_attempts SET intent_at=? WHERE lease_id=?", (now, lease))


def pause_claim(db, job_id, lease, now, reason):
    with transaction(db):
        cursor = db.execute("""UPDATE jobs SET state='paused',lease_id=NULL,lease_until=NULL
                               WHERE id=? AND state='claimed' AND lease_id=?""", (job_id, lease))
        if cursor.rowcount != 1:
            raise QueueConflict("The claim is missing or stale")
        db.execute("""UPDATE job_attempts SET finished_at=?,outcome='paused',reason_code=?
                      WHERE lease_id=?""", (now, reason, lease))


def control_job(db, job_id, action, now):
    """Change only undispatched work. Re-check policy on the next execution."""
    if action not in ("pause", "resume", "cancel"):
        raise ValueError("Unknown queue action")
    with transaction(db):
        row = db.execute("""SELECT state,run_id,scheduled_utc,bot_account_id
                            FROM jobs WHERE id=?""", (job_id,)).fetchone()
        if row is None:
            raise QueueConflict("Job not found")
        state, run_id, scheduled, bot = row
        allowed = {"pause": ("queued",), "resume": ("paused",), "cancel": ("queued", "paused")}
        if state not in allowed[action]:
            raise QueueConflict("Job state does not allow this action")
        if action == "resume":
            if run_id is not None or scheduled is None or scheduled < now - 3600:
                raise QueueConflict("Job requires a new approval")
            if db.execute("""SELECT 1 FROM bot_holds WHERE bot_account_id=?
                             AND (until_utc IS NULL OR until_utc>?)""", (bot, now)).fetchone():
                raise QueueConflict("Telegram hold is still active")
            if not db.execute("SELECT 1 FROM accounts WHERE id=? AND disabled_at IS NULL", (bot,)).fetchone():
                raise QueueConflict("Сначала переподключите отправителя.")
        target = {"pause": "paused", "resume": "queued", "cancel": "cancelled"}[action]
        db.execute("UPDATE jobs SET state=? WHERE id=?", (target, job_id))
        return target


def reject(db, job_id, lease, now, reason, *, retry_after=None):
    with transaction(db):
        row = db.execute("""SELECT bot_account_id,canonical_chat_id FROM jobs WHERE id=? AND lease_id=?
                            AND state IN ('claimed','dispatching')""", (job_id, lease)).fetchone()
        if row is None:
            raise QueueConflict("The publication attempt is missing or stale")
        db.execute("UPDATE jobs SET state='paused',lease_id=NULL,lease_until=NULL WHERE id=?", (job_id,))
        db.execute("""UPDATE job_attempts SET finished_at=?,outcome='paused',reason_code=?
                      WHERE lease_id=?""", (now, reason, lease))
        if reason == "telegram_rate_limited":
            record_bot_hold(db, row[0], now, retry_after)
        elif reason == "telegram_unauthorized":
            db.execute("UPDATE accounts SET disabled_at=?,status='unauthorized' WHERE id=?", (now, row[0]))
            db.execute("UPDATE jobs SET state='paused' WHERE state='queued' AND bot_account_id=?", (row[0],))
        elif reason == "telegram_forbidden":
            db.execute("""UPDATE jobs SET state='paused' WHERE state='queued' AND bot_account_id=?
                          AND canonical_chat_id=?""", row)


def revoke_venue(db, job_id, now):
    """Withdraw current consent and cancel unsent work for one destination."""
    with transaction(db):
        row = db.execute("SELECT canonical_chat_id FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or row[0] is None:
            raise QueueConflict("У записи нет подтверждённого ID площадки.")
        chat = row[0]
        revoked = db.execute("""UPDATE venue_permissions SET revoked_at=?
                                WHERE chat_id=? AND revoked_at IS NULL
                                AND (expires_at IS NULL OR expires_at>?)""", (now, chat, now)).rowcount
        cancelled = db.execute("""UPDATE jobs SET state='cancelled'
                                  WHERE canonical_chat_id=? AND state IN ('queued','paused')""",
                               (chat,)).rowcount
        if revoked or cancelled:
            db.execute("""INSERT INTO audit_events(at,level,event_code,entity_type,entity_id,details_json)
                          VALUES (?,'info','venue_consent_revoked','job',?,?)""",
                       (now, job_id, json.dumps({"revoked": revoked, "cancelled": cancelled})))
        return revoked


def record_bot_hold(db, bot, now, retry_after):
    until = now + retry_after if retry_after is not None else None
    db.execute("""INSERT INTO bot_holds VALUES (?,?,?) ON CONFLICT(bot_account_id)
                  DO UPDATE SET until_utc=CASE
                      WHEN excluded.until_utc IS NULL OR bot_holds.until_utc IS NULL THEN NULL
                      ELSE max(excluded.until_utc,bot_holds.until_utc) END,
                      recorded_at=excluded.recorded_at""", (bot, until, now))


def reconcile(db, job_id, now, *, receipt=None):
    """Record an operator-verified receipt or close the job without requeuing it."""
    with transaction(db):
        row = db.execute("""SELECT canonical_chat_id FROM jobs
                            WHERE id=? AND state='needs_review'""", (job_id,)).fetchone()
        if row is None:
            raise QueueConflict("Only a job requiring review can be reconciled")
        if receipt is None:
            state, sent_at, message_id = "cancelled", None, None
        else:
            chat_id, message_id, sent_at = receipt
            if (row[0] is None or type(chat_id) is not int or chat_id != row[0]
                    or type(message_id) is not int or not 0 < message_id < 2**63
                    or type(sent_at) is not int or not 0 < sent_at <= now):
                raise QueueConflict("Receipt must match the known chat and have a valid message ID and time")
            intent = db.execute("SELECT MAX(intent_at) FROM job_attempts WHERE job_id=?", (job_id,)).fetchone()[0]
            if intent is not None and sent_at < intent:
                raise QueueConflict("Receipt predates the dispatch attempt")
            if db.execute("""SELECT 1 FROM jobs WHERE canonical_chat_id=?
                             AND telegram_message_id=? AND id<>?""", (chat_id, message_id, job_id)).fetchone():
                raise QueueConflict("Receipt already belongs to another job")
            state = "sent"
        db.execute("UPDATE jobs SET state=?,sent_at=?,telegram_message_id=? WHERE id=?",
                   (state, sent_at, message_id, job_id))
        db.execute("""INSERT INTO audit_events(at,level,event_code,entity_type,entity_id,details_json)
                      VALUES (?,'info','manual_reconciliation','job',?,?)""",
                   (now, job_id, json.dumps({"state": state, "message_id": message_id})))
        return state


def clear_indefinite_hold(db, bot_id, now):
    """Clear a hold with no Telegram deadline after operator review."""
    with transaction(db):
        cursor = db.execute("DELETE FROM bot_holds WHERE bot_account_id=? AND until_utc IS NULL",
                            (bot_id,))
        if cursor.rowcount != 1:
            raise QueueConflict("No indefinite hold to clear")
        db.execute("""INSERT INTO audit_events(at,level,event_code,entity_type,entity_id,details_json)
                      VALUES (?,'info','manual_hold_clear','account',?,'{}')""", (now, bot_id))


def finish(db, job_id, lease, now, *, receipt=None):
    if receipt is not None and (type(receipt) is not int or receipt <= 0):
        raise ValueError("Expected a positive message ID")
    state = "sent" if receipt is not None else "needs_review"
    with transaction(db):
        cursor = db.execute("""UPDATE jobs SET state=?,lease_id=NULL,lease_until=NULL,
                               sent_at=?,telegram_message_id=?
                               WHERE id=? AND state='dispatching' AND lease_id=?""",
                            (state, now if receipt is not None else None, receipt, job_id, lease))
        if cursor.rowcount != 1:
            raise QueueConflict("The dispatch is missing or stale")
        db.execute("UPDATE job_attempts SET finished_at=?,outcome=? WHERE lease_id=?",
                   (now, state, lease))


def recover(db, now, *, restart=False):
    """On restart the profile lock guarantees that prior dispatchers have exited."""
    recovered = []
    with transaction(db):
        rows = db.execute("""SELECT id,state,lease_id,scheduled_utc FROM jobs
                             WHERE state IN ('claimed','dispatching') AND (? OR lease_until<=?)
                             ORDER BY id""", (restart, now)).fetchall()
        for job_id, state, lease, scheduled in rows:
            if state == "dispatching":
                next_state = "needs_review"
            else:
                next_state = "paused" if now - scheduled > 3600 else "queued"
            db.execute("UPDATE jobs SET state=?,lease_id=NULL,lease_until=NULL WHERE id=?",
                       (next_state, job_id))
            db.execute("UPDATE job_attempts SET finished_at=?,outcome=? WHERE lease_id=?",
                       (now, next_state, lease))
            recovered.append((job_id, next_state))
    return tuple(recovered)

