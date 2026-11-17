"""Explicit, transactional staging of legacy snapshots before domain import."""
import json
from dataclasses import dataclass
from hashlib import sha256
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .legacy import LegacyInputError, inspect_bytes


@dataclass(frozen=True)
class ArchiveResult:
    run_id: int
    created: bool
    report: dict


def stage_legacy(connection, source, version, source_zone, protector):
    """Stage original bytes and safe counters. This never activates legacy jobs."""
    if source_zone != "UTC":
        try:
            ZoneInfo(source_zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise LegacyInputError("Не удалось загрузить часовой пояс источника.") from exc
    raw = source.read_bytes()
    report = inspect_bytes(raw, version)
    kind = f"v{version}"
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = connection.execute(
            "SELECT id, source_zone FROM import_runs WHERE source_kind=? AND sha256=?",
            (kind, report["sha256"]),
        ).fetchone()
        if existing is not None:
            if existing[1] != source_zone:
                raise LegacyInputError("Этот снимок уже принят с другим часовым поясом.")
            connection.commit()
            return ArchiveResult(existing[0], False, report)

        encrypted = protector.protect(raw)
        if sha256(source.read_bytes()).hexdigest() != report["sha256"]:
            raise LegacyInputError("Исходный файл изменился. Повтори операцию после закрытия v2.")
        run_id = connection.execute(
            "INSERT INTO import_runs(source_kind, sha256, source_zone, run_at) VALUES (?,?,?,unixepoch())",
            (kind, report["sha256"], source_zone),
        ).lastrowid
        connection.execute("INSERT INTO legacy_archives VALUES (?, ?, ?)",
                           (run_id, encrypted, json.dumps(report, sort_keys=True)))
        connection.commit()
        return ArchiveResult(run_id, True, report)
    except BaseException:
        connection.rollback()
        raise

