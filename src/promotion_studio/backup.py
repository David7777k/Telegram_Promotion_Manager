"""Local, credentials-free SQLite backup and restore for the current schema."""
import os
import sqlite3
from contextlib import closing
from tempfile import TemporaryDirectory

from .storage import migrate, open_profile, profile_lock


class BackupError(RuntimeError):
    pass


def _check(connection):
    if (connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
            or connection.execute("PRAGMA foreign_key_check").fetchone()):
        raise BackupError("Проверка целостности резервной копии не пройдена.")


def _ensure_no_assets(profile):
    assets = profile / "assets"
    if assets.exists() and any(assets.iterdir()):
        raise BackupError("В профиле есть вложения. Текущий формат копии сохраняет только базу.")


def create_backup(profile, destination):
    destination = destination.resolve()
    if destination.exists():
        raise BackupError("Файл резервной копии уже существует.")
    if not (profile / "studio.sqlite3").is_file():
        raise BackupError("База профиля не найдена.")
    with open_profile(profile) as source, TemporaryDirectory(dir=destination.parent) as directory:
        _ensure_no_assets(profile)
        temporary = destination.parent / directory / "snapshot.sqlite3"
        with closing(sqlite3.connect(":memory:")) as sanitized:
            source.backup(sanitized)
            if sanitized.execute("""SELECT 1 FROM legacy_archives AS a
                                    LEFT JOIN legacy_import_results AS r USING(run_id)
                                    WHERE r.run_id IS NULL LIMIT 1""").fetchone():
                raise BackupError("Сначала заверши перенос подготовленных архивов.")
            sanitized.execute("DELETE FROM account_secrets")
            sanitized.execute("DELETE FROM legacy_archives")
            sanitized.commit()
            sanitized.execute("VACUUM")
            with closing(sqlite3.connect(temporary)) as snapshot:
                sanitized.backup(snapshot)
                _check(snapshot)
        # On Windows rename fails if the destination appeared in the meantime.
        os.rename(temporary, destination)


def restore_backup(backup, profile):
    if not backup.is_file():
        raise BackupError("Файл резервной копии не найден.")
    with profile_lock(profile), TemporaryDirectory(dir=profile) as directory:
        destination = profile / "studio.sqlite3"
        if destination.exists():
            raise BackupError("Для восстановления выбери новый профиль.")
        _ensure_no_assets(profile)
        temporary = profile / directory / "restore.sqlite3"
        with closing(sqlite3.connect(backup.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            _check(source)
            version = source.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            if version is None or version < 3:
                raise BackupError("Неподдерживаемая версия резервной копии.")
            if source.execute("SELECT 1 FROM account_secrets LIMIT 1").fetchone():
                raise BackupError("Резервная копия содержит учётные данные.")
            if source.execute("SELECT 1 FROM legacy_archives LIMIT 1").fetchone():
                raise BackupError("Резервная копия содержит исходный архив.")
            with closing(sqlite3.connect(temporary)) as restored:
                source.backup(restored)
                restored.execute("PRAGMA foreign_keys=ON")
                migrate(restored)
                restored.execute("DELETE FROM settings WHERE key IN ('auto_schedule','dry_run','live')")
                restored.execute("""UPDATE job_attempts SET finished_at=unixepoch(),
                                    outcome=CASE WHEN intent_at IS NULL THEN 'paused' ELSE 'needs_review' END
                                    WHERE finished_at IS NULL""")
                restored.execute("""UPDATE jobs SET
                                    state=CASE WHEN state='dispatching' THEN 'needs_review' ELSE 'paused' END,
                                    lease_id=NULL, lease_until=NULL
                                    WHERE state IN ('queued','claimed','dispatching')""")
                restored.commit()
                _check(restored)
        os.rename(temporary, destination)

