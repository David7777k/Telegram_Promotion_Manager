"""Resolve local scheduling input and evaluate recurring local-time windows."""
from dataclasses import dataclass
from datetime import datetime, time, timezone


class LocalTimeError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def resolve_local(local, zone, *, fold=None):
    """Convert a naive wall time to UTC, requiring explicit choice on an overlap."""
    if local.tzinfo is not None:
        raise ValueError("Expected a naive local datetime")
    if fold not in (None, 0, 1):
        raise ValueError("fold must be None, 0 or 1")
    candidates = {}
    for option in (0, 1):
        utc = local.replace(tzinfo=zone, fold=option).astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == local:
            candidates[option] = utc
    if not candidates:
        raise LocalTimeError("nonexistent_local_time")
    if len(set(candidates.values())) == 1:
        return next(iter(candidates.values()))
    if fold is None:
        raise LocalTimeError("ambiguous_local_time")
    return candidates[fold]


@dataclass(frozen=True)
class WeeklyWindow:
    weekday: int
    start: time
    end: time

    def __post_init__(self):
        if (self.weekday not in range(7) or self.start.tzinfo is not None
                or self.end.tzinfo is not None or self.start == self.end):
            raise ValueError("Invalid weekly window")

    def contains(self, instant: datetime, zone):
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("Expected an aware instant")
        local = instant.astimezone(zone)
        weekday, clock = local.weekday(), local.time()
        if self.start < self.end:
            return weekday == self.weekday and self.start <= clock < self.end
        return ((weekday == self.weekday and clock >= self.start)
                or (weekday == (self.weekday + 1) % 7 and clock < self.end))

