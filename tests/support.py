from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


@dataclass
class FakeClock:
    current: datetime

    def now_utc(self):
        return self.current

    def advance(self, **interval):
        self.current += timedelta(**interval)


class OfflineProfile:
    def __enter__(self):
        self.directory = TemporaryDirectory()
        self.guard = patch("socket.socket.connect", side_effect=AssertionError("Network disabled"))
        self.guard.start()
        return Path(self.directory.name)

    def __exit__(self, *_):
        try:
            self.guard.stop()
        finally:
            self.directory.cleanup()

