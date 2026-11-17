"""Build a diagnostics report without serializing user data or provider errors."""
import sqlite3
from contextlib import closing


def read_profile_diagnostics(profile):
    uri = (profile / "studio.sqlite3").resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN")
        return diagnostics_report(connection)


def read_queue_status(profile, *, operator_details=False):
    """Return operational status without messages, chat IDs or invite links."""
    uri = (profile / "studio.sqlite3").resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("BEGIN")
        jobs = connection.execute("""SELECT id,state,scheduled_utc FROM jobs
                                     ORDER BY scheduled_utc IS NULL,scheduled_utc,id""")
        subscriptions = connection.execute("""SELECT id,state,started_at FROM subscription_requests
                                              ORDER BY started_at,id""")
        report = {
            "jobs": [{"id": identifier, "state": state, "scheduled_utc": when}
                     for identifier, state, when in jobs],
            "subscriptions": [{"id": identifier, "state": "needs_review" if state == "pending" else state,
                               "started_at": when} for identifier, state, when in subscriptions],
        }
        if operator_details:
            details = connection.execute("""SELECT j.id,j.canonical_chat_id,a.disabled_at,h.recorded_at,h.until_utc,
                (SELECT reason_code FROM job_attempts WHERE job_id=j.id ORDER BY rowid DESC LIMIT 1),
                e.title,d.title,a.label,j.sent_at,j.telegram_message_id,j.auto_approved,
                (SELECT max(p.sent_at+max(p.cooldown_seconds,j.cooldown_seconds)) FROM jobs p
                 WHERE p.canonical_chat_id=j.canonical_chat_id AND p.state='sent')
                FROM jobs AS j LEFT JOIN accounts AS a ON a.id=j.bot_account_id
                LEFT JOIN bot_holds AS h ON h.bot_account_id=j.bot_account_id
                LEFT JOIN campaign_editor e ON e.published_campaign=j.campaign_id
                LEFT JOIN venue_directory d ON d.venue_id=j.venue_id""")
            by_id = {row[0]: row[1:] for row in details}
            for job in report["jobs"]:
                chat, disabled, hold_at, hold_until, reason, name, venue, account, sent, message, auto, not_before = by_id[job["id"]]
                job.update(chat_id=chat, bot_disabled=disabled is not None, hold_at=hold_at,
                           hold_until=hold_until, reason=reason, campaign=name, venue=venue, sender=account,
                           sent_at=sent, message_id=message, auto=bool(auto), not_before=not_before)
        return report


def diagnostics_report(connection):
    return {
        "report_version": 1,
        "sqlite_version": sqlite3.sqlite_version,
        "schema_version": connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0],
        "integrity_ok": connection.execute("PRAGMA quick_check(1)").fetchone() == ("ok",),
        "foreign_key_violations": sum(1 for _ in connection.execute("PRAGMA foreign_key_check")),
        "import_runs": connection.execute("SELECT COUNT(*) FROM import_runs").fetchone()[0],
        "import_issues": connection.execute("SELECT COUNT(*) FROM import_issues").fetchone()[0],
    }

