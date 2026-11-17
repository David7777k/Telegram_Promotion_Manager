import os
import sqlite3
import unittest
from pathlib import Path

from promotion_studio.backup import BackupError, create_backup, restore_backup
from promotion_studio.legacy_archive import stage_legacy
from promotion_studio.legacy_import import import_archived
from promotion_studio.security import DPAPI, SecretStore
from promotion_studio.storage import open_profile
from tests.support import OfflineProfile


@unittest.skipUnless(os.name == "nt", "Windows profile")
class BackupTests(unittest.TestCase):
    def test_round_trip_removes_credentials_and_preserves_jobs(self):
        with OfflineProfile() as root:
            source, target, backup = root / "source", root / "target", root / "backup.sqlite3"
            with open_profile(source) as db:
                fixture = Path(__file__).parent / "fixtures" / "v2.json"
                protector = DPAPI()
                archive = stage_legacy(db, fixture, 2, "UTC", protector)
                import_archived(db, archive.run_id, protector)
                db.execute("INSERT INTO settings VALUES ('auto_schedule','true')")
                db.execute("INSERT INTO accounts(id,kind,label,status) VALUES ('bot','bot','bot','ready')")
                store = SecretStore(db, protector)
                store.put("bot", "bot_token", b"test-only-token")
                cipher = db.execute("SELECT ciphertext FROM account_secrets").fetchone()[0]
                raw_archive = db.execute("SELECT ciphertext FROM legacy_archives").fetchone()[0]
            create_backup(source, backup)
            self.assertNotIn(cipher, backup.read_bytes())
            self.assertNotIn(raw_archive, backup.read_bytes())
            restore_backup(backup, target)
            with open_profile(target) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone(), (5,))
                self.assertEqual(db.execute("SELECT count(*) FROM account_secrets").fetchone(), (0,))
                self.assertEqual(db.execute("SELECT key FROM settings").fetchall(),
                                 [("selected_campaign",)])
                self.assertFalse(import_archived(db, archive.run_id, DPAPI())["created"])
            with self.assertRaises(BackupError):
                restore_backup(backup, target)
            with self.assertRaises(BackupError):
                create_backup(source, backup)

    def test_invalid_backup_and_assets_do_not_publish_partial_output(self):
        with OfflineProfile() as root:
            source, target, backup = root / "source", root / "target", root / "backup.sqlite3"
            with open_profile(source):
                (source / "assets").mkdir()
                (source / "assets" / "photo").write_bytes(b"example")
            with self.assertRaises(BackupError):
                create_backup(source, backup)
            self.assertFalse(backup.exists())
            backup.write_bytes(b"broken SQLite")
            with self.assertRaises(sqlite3.DatabaseError):
                restore_backup(backup, target)
            self.assertFalse((target / "studio.sqlite3").exists())

