"""SQLite bootstrap and exclusive ownership of a local Windows profile."""
import os
import sqlite3
from contextlib import contextmanager
from hashlib import sha256


MIGRATIONS = (
    (1, (
        "CREATE TABLE settings (key TEXT PRIMARY KEY NOT NULL, value_json TEXT NOT NULL CHECK(json_valid(value_json)))",
        """CREATE TABLE audit_events (
            id INTEGER PRIMARY KEY, at INTEGER NOT NULL, level TEXT NOT NULL,
            event_code TEXT NOT NULL, entity_type TEXT, entity_id TEXT,
            details_json TEXT NOT NULL CHECK(json_valid(details_json)), correlation_id TEXT
        )""",
    )),
    (2, (
        """CREATE TABLE import_runs (
            id INTEGER PRIMARY KEY, source_kind TEXT NOT NULL, sha256 TEXT NOT NULL,
            source_zone TEXT NOT NULL, run_at INTEGER NOT NULL, UNIQUE(source_kind, sha256)
        )""",
        """CREATE TABLE import_issues (
            id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES import_runs(id),
            entity_kind TEXT NOT NULL, legacy_id TEXT, reason_code TEXT NOT NULL, raw_ref TEXT
        )""",
    )),
    (3, (
        """CREATE TABLE accounts (
            id TEXT PRIMARY KEY NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('bot','user')),
            telegram_user_id INTEGER, label TEXT NOT NULL, username TEXT,
            status TEXT NOT NULL, last_sync_at INTEGER, disabled_at INTEGER,
            UNIQUE(kind, telegram_user_id)
        )""",
        """CREATE TABLE account_secrets (
            account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            secret_kind TEXT NOT NULL, ciphertext BLOB NOT NULL,
            format_version INTEGER NOT NULL CHECK(format_version=1),
            PRIMARY KEY(account_id,secret_kind)
        )""",
    )),
    (4, (
        """CREATE TABLE legacy_archives (
            run_id INTEGER PRIMARY KEY REFERENCES import_runs(id) ON DELETE CASCADE,
            ciphertext BLOB NOT NULL,
            report_json TEXT NOT NULL CHECK(json_valid(report_json))
        )""",
    )),
    (5, (
        """CREATE TABLE campaigns (id TEXT PRIMARY KEY NOT NULL, run_id INTEGER NOT NULL REFERENCES import_runs(id),
            status TEXT NOT NULL CHECK(status='draft'), payload BLOB NOT NULL)""",
        """CREATE TABLE venues (id TEXT PRIMARY KEY NOT NULL, run_id INTEGER NOT NULL REFERENCES import_runs(id),
            consent TEXT NOT NULL CHECK(consent='recheck'), legacy_last_sent_at INTEGER, payload BLOB NOT NULL)""",
        """CREATE TABLE templates (id TEXT PRIMARY KEY NOT NULL, run_id INTEGER NOT NULL REFERENCES import_runs(id),
            syntax_version INTEGER NOT NULL CHECK(syntax_version=2), payload BLOB NOT NULL)""",
        """CREATE TABLE jobs (id TEXT PRIMARY KEY NOT NULL, run_id INTEGER NOT NULL REFERENCES import_runs(id),
            campaign_id TEXT NOT NULL REFERENCES campaigns(id), venue_id TEXT NOT NULL REFERENCES venues(id),
            template_id TEXT NOT NULL REFERENCES templates(id),
            state TEXT NOT NULL CHECK(state IN ('paused','needs_review','sent')), scheduled_utc INTEGER,
            sent_at INTEGER, price_at_approval INTEGER, actual_cost_minor INTEGER, payload BLOB NOT NULL)""",
        """CREATE TABLE legacy_activity (run_id INTEGER NOT NULL REFERENCES import_runs(id),
            source_index INTEGER NOT NULL, payload BLOB NOT NULL, PRIMARY KEY(run_id,source_index))""",
        """CREATE TABLE legacy_import_results (run_id INTEGER PRIMARY KEY REFERENCES import_runs(id),
            report_json TEXT NOT NULL CHECK(json_valid(report_json)))""",
    )),
    (6, (
        "ALTER TABLE jobs RENAME TO legacy_jobs_v5",
        """CREATE TABLE jobs (
            id TEXT PRIMARY KEY NOT NULL, run_id INTEGER REFERENCES import_runs(id),
            campaign_id TEXT NOT NULL REFERENCES campaigns(id), venue_id TEXT NOT NULL REFERENCES venues(id),
            template_id TEXT NOT NULL REFERENCES templates(id),
            state TEXT NOT NULL CHECK(state IN ('paused','needs_review','sent','queued','claimed','dispatching','cancelled')),
            scheduled_utc INTEGER, sent_at INTEGER, price_at_approval INTEGER, actual_cost_minor INTEGER,
            payload BLOB NOT NULL, canonical_chat_id INTEGER, bot_account_id TEXT REFERENCES accounts(id),
            client_key TEXT UNIQUE, lease_id TEXT, lease_until INTEGER, telegram_message_id INTEGER,
            CHECK(run_id IS NOT NULL OR (canonical_chat_id IS NOT NULL AND bot_account_id IS NOT NULL
                AND client_key IS NOT NULL AND scheduled_utc IS NOT NULL)),
            CHECK((state IN ('claimed','dispatching')) = (lease_id IS NOT NULL AND lease_until IS NOT NULL))
        )""",
        """INSERT INTO jobs(id,run_id,campaign_id,venue_id,template_id,state,scheduled_utc,
                            sent_at,price_at_approval,actual_cost_minor,payload)
           SELECT id,run_id,campaign_id,venue_id,template_id,state,scheduled_utc,
                  sent_at,price_at_approval,actual_cost_minor,payload FROM legacy_jobs_v5""",
        "DROP TABLE legacy_jobs_v5",
        "CREATE INDEX jobs_due ON jobs(state,scheduled_utc,id)",
        "CREATE INDEX jobs_chat_slot ON jobs(canonical_chat_id,scheduled_utc)",
        """CREATE UNIQUE INDEX jobs_receipt ON jobs(canonical_chat_id,telegram_message_id)
           WHERE telegram_message_id IS NOT NULL""",
        """CREATE TABLE job_attempts (
            lease_id TEXT PRIMARY KEY NOT NULL, job_id TEXT NOT NULL REFERENCES jobs(id),
            claimed_at INTEGER NOT NULL, intent_at INTEGER, finished_at INTEGER, outcome TEXT
        )""",
    )),
    (7, (
        """CREATE TABLE venue_permissions (
            id TEXT PRIMARY KEY NOT NULL, venue_id TEXT NOT NULL REFERENCES venues(id),
            chat_id INTEGER NOT NULL CHECK(chat_id<0),
            bot_account_id TEXT NOT NULL REFERENCES accounts(id), thread_id INTEGER,
            valid_from INTEGER NOT NULL, expires_at INTEGER, revoked_at INTEGER,
            CHECK(expires_at IS NULL OR expires_at>valid_from)
        )""",
        "ALTER TABLE job_attempts ADD COLUMN reason_code TEXT",
    )),
    (8, (
        """CREATE TABLE bot_holds (
            bot_account_id TEXT PRIMARY KEY NOT NULL REFERENCES accounts(id),
            until_utc INTEGER, recorded_at INTEGER NOT NULL
        )""",
    )),
    (9, (
        "ALTER TABLE jobs ADD COLUMN cooldown_seconds INTEGER NOT NULL DEFAULT 0 CHECK(cooldown_seconds>=0)",
    )),
    (10, (
        """CREATE TABLE venue_permission_windows (
            permission_id TEXT NOT NULL REFERENCES venue_permissions(id) ON DELETE CASCADE,
            weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
            start_minute INTEGER NOT NULL CHECK(start_minute BETWEEN 0 AND 1439),
            end_minute INTEGER NOT NULL CHECK(end_minute BETWEEN 0 AND 1439),
            CHECK(start_minute<>end_minute),
            PRIMARY KEY(permission_id,weekday,start_minute,end_minute)
        )""",
        "ALTER TABLE venue_permissions ADD COLUMN timezone_name TEXT",
    )),
    (11, (
        """CREATE TABLE publication_photos (
            digest TEXT PRIMARY KEY NOT NULL CHECK(length(digest)=64),
            mime TEXT NOT NULL CHECK(mime IN ('image/jpeg','image/png')),
            size INTEGER NOT NULL CHECK(size BETWEEN 1 AND 10000000),
            ciphertext BLOB NOT NULL
        )""",
        "ALTER TABLE venue_permissions ADD COLUMN allows_photo INTEGER NOT NULL DEFAULT 0 CHECK(allows_photo IN (0,1))",
    )),
    (12, (
        """CREATE TABLE subscription_requests (
            id TEXT PRIMARY KEY NOT NULL, campaign_id TEXT NOT NULL REFERENCES campaigns(id),
            bot_account_id TEXT NOT NULL REFERENCES accounts(id),
            chat_id INTEGER NOT NULL CHECK(chat_id<0), telegram_bot_id INTEGER NOT NULL,
            price INTEGER NOT NULL CHECK(price BETWEEN 1 AND 10000), name TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('pending','needs_review','created','rejected','closed')),
            started_at INTEGER NOT NULL, resolved_at INTEGER, ciphertext BLOB,
            CHECK((state='created')=(ciphertext IS NOT NULL))
        )""",
        """CREATE UNIQUE INDEX subscription_open_scope ON subscription_requests(campaign_id,chat_id)
           WHERE state IN ('pending','needs_review','created')""",
    )),
    (13, (
        "ALTER TABLE venue_permissions ADD COLUMN evidence BLOB",
        "ALTER TABLE venues ADD COLUMN canonical_chat_id INTEGER",
        "CREATE UNIQUE INDEX venues_canonical_id ON venues(canonical_chat_id) WHERE canonical_chat_id IS NOT NULL",
    )),
    (14, (
        "ALTER TABLE accounts ADD COLUMN api_id INTEGER",
        "ALTER TABLE accounts ADD COLUMN avatar BLOB",
        "ALTER TABLE accounts ADD COLUMN dialog_count INTEGER",
        "ALTER TABLE jobs ADD COLUMN auto_approved INTEGER NOT NULL DEFAULT 0 CHECK(auto_approved IN (0,1))",
        "CREATE INDEX jobs_auto_due ON jobs(auto_approved,state,scheduled_utc,id)",
        """CREATE TABLE venue_directory (
            venue_id TEXT PRIMARY KEY REFERENCES venues(id), title TEXT NOT NULL, username TEXT,
            kind TEXT, participants INTEGER, category TEXT NOT NULL DEFAULT '', favorite INTEGER NOT NULL DEFAULT 0,
            excluded INTEGER NOT NULL DEFAULT 0, decision TEXT NOT NULL DEFAULT 'review'
            CHECK(decision IN ('review','allowed','denied')), checked_at INTEGER NOT NULL)""",
        """CREATE TABLE venue_access (
            venue_id TEXT NOT NULL REFERENCES venues(id), account_id TEXT NOT NULL REFERENCES accounts(id),
            can_text INTEGER NOT NULL, can_photo INTEGER NOT NULL, checked_at INTEGER NOT NULL,
            PRIMARY KEY(venue_id,account_id))""",
        """CREATE TABLE campaign_editor (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, changed_at INTEGER NOT NULL, payload BLOB NOT NULL,
            published_campaign TEXT REFERENCES campaigns(id))""",
        """CREATE TABLE editable_templates (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0)""",
        """CREATE TABLE editable_template_versions (
            template_id TEXT NOT NULL REFERENCES editable_templates(id), version INTEGER NOT NULL,
            created_at INTEGER NOT NULL, ciphertext BLOB NOT NULL, PRIMARY KEY(template_id,version))""",
    )),
)


class ProfileBusy(RuntimeError):
    pass


class MigrationError(RuntimeError):
    pass


@contextmanager
def profile_lock(profile):
    import msvcrt

    profile.mkdir(parents=True, exist_ok=True)
    # Leave the lock file in place. Windows releases the byte lock on process exit.
    with (profile / "profile.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise ProfileBusy("Профиль уже открыт другим процессом") from exc
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def migrate(connection, migrations=MIGRATIONS):
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY, checksum TEXT NOT NULL,
            applied_at INTEGER NOT NULL DEFAULT (unixepoch())
        )""")
        applied = dict(connection.execute("SELECT version, checksum FROM schema_migrations"))
        expected = tuple(range(1, len(migrations) + 1))
        if tuple(version for version, _ in migrations) != expected or set(applied) != set(expected[:len(applied)]):
            raise MigrationError("Неизвестная версия или пропуск в журнале миграций")
        for version, statements in migrations:
            checksum = sha256("\0".join(statements).encode("utf-8")).hexdigest()
            if version in applied:
                if applied[version] != checksum:
                    raise MigrationError(f"Изменилась контрольная сумма миграции {version}")
                continue
            for statement in statements:
                connection.execute(statement)
            connection.execute("INSERT INTO schema_migrations(version, checksum) VALUES (?, ?)",
                               (version, checksum))
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise MigrationError("Нарушены связи SQLite")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


@contextmanager
def open_profile(profile):
    if os.name != "nt":
        raise RuntimeError("Рабочий профиль поддерживается на Windows")
    with profile_lock(profile):
        connection = sqlite3.connect(profile / "studio.sqlite3", isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            migrate(connection)
            yield connection
        finally:
            connection.close()

