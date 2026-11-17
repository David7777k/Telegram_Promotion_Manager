import io
import struct
import unittest
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from promotion_studio.schedule_time import LocalTimeError, WeeklyWindow, resolve_local


def transition_zone():
    # Small TZif with UTC+1 before March, UTC+2 in summer, then UTC+1 again.
    transitions = [int(datetime(2026, month, day, 1, tzinfo=timezone.utc).timestamp())
                   for month, day in ((3, 29), (10, 25))]
    header = struct.pack(">4sc15x6I", b"TZif", b"\0", 0, 0, 0, 2, 2, 8)
    body = struct.pack(">2i2B", *transitions, 1, 0)
    body += struct.pack(">iBBiBB", 3600, 0, 0, 7200, 1, 4) + b"STD\0DST\0"
    return ZoneInfo.from_file(io.BytesIO(header + body))


class ScheduleTimeTests(unittest.TestCase):
    def test_gap_overlap_and_explicit_fold(self):
        zone = transition_zone()
        with self.assertRaisesRegex(LocalTimeError, "nonexistent"):
            resolve_local(datetime(2026, 3, 29, 2, 30), zone)
        local = datetime(2026, 10, 25, 2, 30)
        with self.assertRaisesRegex(LocalTimeError, "ambiguous"):
            resolve_local(local, zone)
        self.assertEqual(resolve_local(local, zone, fold=0).hour, 0)
        self.assertEqual(resolve_local(local, zone, fold=1).hour, 1)

    def test_window_wraps_sunday_and_end_is_exclusive(self):
        zone = timezone.utc
        window = WeeklyWindow(6, time(23), time(2))
        self.assertTrue(window.contains(datetime(2026, 11, 15, 23, tzinfo=zone), zone))
        self.assertTrue(window.contains(datetime(2026, 11, 16, 1, 59, tzinfo=zone), zone))
        self.assertFalse(window.contains(datetime(2026, 11, 16, 2, tzinfo=zone), zone))

