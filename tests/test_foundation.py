import json
import socket
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tests.support import FakeClock, OfflineProfile


class FoundationTests(unittest.TestCase):
    def test_fixture_shapes(self):
        fixtures = Path(__file__).parent / "fixtures"
        v1 = json.loads((fixtures / "v1.json").read_text(encoding="utf-8"))
        v2 = json.loads((fixtures / "v2.json").read_text(encoding="utf-8"))
        self.assertEqual(v1["last_sent"]["venue1"], 1762000000)
        self.assertEqual({post["status"] for post in v2["posts"]},
                         {"planned", "sending", "sent", "failed", "blocked"})
        self.assertIn("📚", v2["campaigns"][0]["name"])

    def test_offline_profile_and_clock(self):
        clock = FakeClock(datetime(2026, 11, 12, tzinfo=timezone.utc))
        clock.advance(hours=2)
        self.assertEqual(clock.now_utc().hour, 2)
        with OfflineProfile() as profile, socket.socket() as connection:
            self.assertTrue(profile.is_dir())
            with self.assertRaisesRegex(AssertionError, "Network disabled"):
                connection.connect(("127.0.0.1", 1))
        self.assertFalse(profile.exists())

