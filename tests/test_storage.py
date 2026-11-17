import os
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing

from promotion_studio.storage import MIGRATIONS, MigrationError, migrate, open_profile
from tests.support import OfflineProfile


@unittest.skipUnless(os.name == "nt", "Windows profile ownership")
class StorageTests(unittest.TestCase):
    def test_open_and_reopen_profile(self):
        with OfflineProfile() as path:
            with open_profile(path) as db:
                for key, expected in {"foreign_keys": 1, "busy_timeout": 5000,
                                      "synchronous": 2, "journal_mode": "delete"}.items():
                    self.assertEqual(db.execute(f"PRAGMA {key}").fetchone()[0], expected)
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("INSERT INTO import_issues (run_id, entity_kind, reason_code) VALUES (1,'x','y')")
                db.execute("INSERT INTO settings VALUES ('example','true')")
            with open_profile(path) as db:
                self.assertEqual(db.execute("SELECT value_json FROM settings").fetchone(), ("true",))

    def test_second_process_cannot_open_profile(self):
        script = """
from pathlib import Path
import sys
from promotion_studio.storage import ProfileBusy, open_profile
try:
    with open_profile(Path(sys.argv[1])):
        pass
except ProfileBusy:
    sys.exit(31)
"""
        with OfflineProfile() as path:
            with open_profile(path):
                self.assertEqual(subprocess.run([sys.executable, "-c", script, str(path)],
                                                timeout=10).returncode, 31)
            self.assertEqual(subprocess.run([sys.executable, "-c", script, str(path)],
                                            timeout=10).returncode, 0)


class MigrationTests(unittest.TestCase):
    def test_upgrade_checksum_and_rollback(self):
        with closing(sqlite3.connect(":memory:")) as db:
            migrate(db, MIGRATIONS[:1])
            migrate(db)
            self.assertEqual(db.execute("SELECT count(*) FROM schema_migrations").fetchone(), (len(MIGRATIONS),))
            with self.assertRaises(sqlite3.OperationalError):
                migrate(db, MIGRATIONS + ((len(MIGRATIONS) + 1, (
                    "CREATE TABLE disposable (id INTEGER)", "INVALID SQL",
                )),))
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='disposable'").fetchone())
            db.execute("UPDATE schema_migrations SET checksum='tampered' WHERE version=1")
            db.commit()
            with self.assertRaises(MigrationError):
                migrate(db)

