import json
import os
import sqlite3
import unittest
from pathlib import Path

from promotion_studio.legacy_archive import stage_legacy
from promotion_studio.legacy_import import import_archived
from promotion_studio.security import DPAPI
from promotion_studio.storage import open_profile
from tests.support import OfflineProfile


FIXTURES = Path(__file__).parent / "fixtures"


@unittest.skipUnless(os.name == "nt", "Windows profile and DPAPI")
class ImportTests(unittest.TestCase):
    def test_v2_preserves_history_and_disables_dispatch(self):
        with OfflineProfile() as profile:
            with open_profile(profile) as db:
                protector = DPAPI()
                run = stage_legacy(db, FIXTURES / "v2.json", 2, "UTC", protector)
                report = import_archived(db, run.run_id, protector)
                self.assertEqual(report["records"]["jobs"], 5)
                self.assertEqual(report["issues"], {})
                self.assertEqual(db.execute("SELECT state FROM jobs ORDER BY id").fetchall(),
                                 [(s,) for s in ("paused", "needs_review", "sent", "needs_review", "needs_review")])
                price, cost, payload = db.execute(
                    "SELECT price_at_approval,actual_cost_minor,payload FROM jobs WHERE id='post2'"
                ).fetchone()
                self.assertEqual((price, cost), (250, 1755))
                self.assertEqual(json.loads(protector.unprotect(payload))["message_id"], "17")
                self.assertEqual(db.execute("SELECT key,value_json FROM settings").fetchall(),
                                 [("selected_campaign", '"campaign"')])
            with open_profile(profile) as db:
                self.assertFalse(import_archived(db, run.run_id, DPAPI())["created"])
                self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone(), (5,))
            self.assertNotIn(b"ExampleV2", (profile / "studio.sqlite3").read_bytes())

    def test_v1_and_rollback(self):
        with OfflineProfile() as profile, open_profile(profile) as db:
            protector = DPAPI()
            run = stage_legacy(db, FIXTURES / "v1.json", 1, "UTC", protector)
            db.execute("""CREATE TEMP TRIGGER reject_template BEFORE INSERT ON templates
                          BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
            with self.assertRaises(sqlite3.IntegrityError):
                import_archived(db, run.run_id, protector)
            self.assertEqual(db.execute("SELECT count(*) FROM campaigns").fetchone(), (0,))
            db.execute("DROP TRIGGER reject_template")
            self.assertTrue(import_archived(db, run.run_id, protector)["created"])
            self.assertEqual(db.execute("SELECT consent,legacy_last_sent_at FROM venues").fetchone(),
                             ("recheck", 1762000000))

    def test_bad_references_and_duplicate_ids_are_quarantined(self):
        with OfflineProfile() as profile, open_profile(profile) as db:
            data = json.loads((FIXTURES / "v2.json").read_text(encoding="utf-8"))
            data["targets"].append(data["targets"][0])
            path = profile / "synthetic.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            protector = DPAPI()
            run = stage_legacy(db, path, 2, "UTC", protector)
            report = import_archived(db, run.run_id, protector)
            self.assertEqual(report["issues"], {"venues.duplicate_id": 2, "jobs.unresolved_reference": 5})
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone(), (0,))

