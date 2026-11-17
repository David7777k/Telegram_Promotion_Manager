import json
import unittest
from pathlib import Path

from promotion_studio.legacy import LegacyInputError, inspect_legacy
from tests.support import OfflineProfile


class LegacyInspectionTests(unittest.TestCase):
    def test_fixture_reports_contain_no_source_values(self):
        fixtures = Path(__file__).parent / "fixtures"
        for version in (1, 2):
            path = fixtures / f"v{version}.json"
            before = path.read_bytes()
            report = inspect_legacy(path, version)
            self.assertEqual(report["issues"], {})
            self.assertEqual(path.read_bytes(), before)
            self.assertNotIn("Example", json.dumps(report))

    def test_invalid_json_and_duplicate_orphan(self):
        with OfflineProfile() as profile:
            path = profile / "state.json"
            path.write_text("{bad", encoding="utf-8")
            with self.assertRaises(LegacyInputError):
                inspect_legacy(path, 2)
            path.write_text(json.dumps({"version": 2, "campaigns": [{"id": "x"}, {"id": "x"}],
                                        "posts": [{"id": "p", "target_id": [], "status": "planned"}]}),
                            encoding="utf-8")
            report = inspect_legacy(path, 2)
            self.assertEqual(report["issues"]["campaigns.duplicate_id"], 1)
            self.assertEqual(report["issues"]["posts.missing_target_id"], 1)

