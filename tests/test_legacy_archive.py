import json
import os
import sqlite3
import unittest
from pathlib import Path

from promotion_studio.diagnostics import diagnostics_report, read_profile_diagnostics
from promotion_studio.legacy import LegacyInputError
from promotion_studio.legacy_archive import stage_legacy
from promotion_studio.security import DPAPI
from promotion_studio.storage import open_profile
from tests.support import OfflineProfile


@unittest.skipUnless(os.name == "nt", "Windows DPAPI")
class LegacyArchiveTests(unittest.TestCase):
    def test_archive_is_encrypted_and_idempotent_across_reopen(self):
        for version in (1, 2):
            with self.subTest(version=version), OfflineProfile() as profile:
                source = Path(__file__).parent / "fixtures" / f"v{version}.json"
                raw = source.read_bytes()
                with open_profile(profile) as db:
                    first = stage_legacy(db, source, version, "UTC", DPAPI())
                    cipher = db.execute("SELECT ciphertext FROM legacy_archives").fetchone()[0]
                    self.assertEqual(DPAPI().unprotect(cipher), raw)
                    db.execute("INSERT INTO audit_events(at,level,event_code,details_json) VALUES (0,'info','test',?)",
                               (json.dumps({"token": "test-diagnostic-canary"}),))
                    self.assertNotIn("test-diagnostic-canary", json.dumps(diagnostics_report(db)))
                self.assertNotIn(b"https://t.me/+", (profile / "studio.sqlite3").read_bytes())
                with open_profile(profile) as db:
                    second = stage_legacy(db, source, version, "UTC", DPAPI())
                    self.assertFalse(second.created)
                    self.assertEqual(second.run_id, first.run_id)
                    self.assertEqual(diagnostics_report(db)["import_runs"], 1)
                self.assertEqual(source.read_bytes(), raw)

    def test_diagnostics_never_creates_or_modifies_profile(self):
        with OfflineProfile() as profile:
            with self.assertRaises(sqlite3.OperationalError):
                read_profile_diagnostics(profile)
            self.assertEqual(list(profile.iterdir()), [])
            with open_profile(profile):
                pass
            database = profile / "studio.sqlite3"
            original = database.read_bytes()
            self.assertTrue(read_profile_diagnostics(profile)["integrity_ok"])
            self.assertEqual(database.read_bytes(), original)

    def test_archive_insert_failure_rolls_back_run(self):
        source = Path(__file__).parent / "fixtures" / "v2.json"
        with OfflineProfile() as profile, open_profile(profile) as db:
            db.execute("""CREATE TEMP TRIGGER reject_archive BEFORE INSERT ON legacy_archives
                          BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
            with self.assertRaises(sqlite3.IntegrityError):
                stage_legacy(db, source, 2, "UTC", DPAPI())
            self.assertEqual(diagnostics_report(db)["import_runs"], 0)
            db.execute("DROP TRIGGER reject_archive")
            self.assertTrue(stage_legacy(db, source, 2, "UTC", DPAPI()).created)

    def test_changed_source_is_rejected(self):
        with OfflineProfile() as profile, open_profile(profile) as db:
            source = profile / "state.json"
            source.write_text('{"version": 2}', encoding="utf-8")

            class RewritingProtector:
                def protect(self, raw):
                    source.write_text('{"version": 2, "posts": []}', encoding="utf-8")
                    return DPAPI().protect(raw)

            with self.assertRaises(LegacyInputError):
                stage_legacy(db, source, 2, "UTC", RewritingProtector())
            self.assertEqual(diagnostics_report(db)["import_runs"], 0)

