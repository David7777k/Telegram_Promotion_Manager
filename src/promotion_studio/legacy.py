"""Read-only inspection of legacy migration input."""
import json
from collections import Counter
from hashlib import sha256


class LegacyInputError(ValueError):
    pass


def inspect_legacy(path, version):
    return inspect_bytes(path.read_bytes(), version)


def inspect_bytes(raw, version):
    if version not in (1, 2):
        raise ValueError("Unsupported legacy version")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError):
        raise LegacyInputError("Некорректный JSON. Исходный файл сохранён.") from None
    if not isinstance(payload, dict) or (version == 2 and payload.get("version") != 2):
        raise LegacyInputError("Некорректная структура или версия исходного файла.")

    issues = Counter()
    counts = {}
    indexes = {}
    collections = ("campaigns", "targets", "templates", "posts", "activity") if version == 2 else ("targets",)
    for name in collections:
        records = payload.get(name, [])
        if not isinstance(records, list):
            issues[f"{name}.invalid_collection"] += 1
            continue
        ids = set()
        counts[name] = len(records)
        for row in records:
            if not isinstance(row, dict):
                issues[f"{name}.invalid_record"] += 1
                continue
            if name == "activity":
                continue
            identifier = row.get("id")
            if not isinstance(identifier, str) or not identifier.strip():
                issues[f"{name}.missing_id"] += 1
            elif identifier in ids:
                issues[f"{name}.duplicate_id"] += 1
            else:
                ids.add(identifier)
        indexes[name] = ids

    if version == 2:
        posts = payload.get("posts", [])
        if isinstance(posts, list):
            for row in posts:
                if not isinstance(row, dict):
                    continue
                for field, target in (("campaign_id", "campaigns"), ("target_id", "targets"),
                                      ("template_id", "templates")):
                    key = row.get(field)
                    if not isinstance(key, str) or key not in indexes.get(target, set()):
                        issues[f"posts.missing_{field}"] += 1
                if row.get("status") not in ("planned", "sending", "sent", "failed", "blocked"):
                    issues["posts.unknown_status"] += 1
    else:
        for name in ("templates", "last_sent"):
            value = payload.get(name, {})
            if isinstance(value, dict):
                counts[name] = len(value)
            else:
                issues[f"{name}.invalid_collection"] += 1

    return {"legacy_version": version, "sha256": sha256(raw).hexdigest(),
            "records": counts, "issues": dict(issues)}

