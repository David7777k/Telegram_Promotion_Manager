"""Explicit import and local search for venue candidates."""
import csv
import io
import json
import re
from uuid import uuid4

from .job_queue import transaction


def parse_catalog(raw, *, csv_format):
    if len(raw) > 1_000_000:
        raise ValueError("Каталог больше 1 МБ.")
    text = raw.decode("utf-8-sig")
    if csv_format:
        reader = csv.DictReader(io.StringIO(text), strict=True)
        if not reader.fieldnames or "chat_id" not in reader.fieldnames:
            raise ValueError("В CSV нужна колонка chat_id.")
        rows = ((row.get("chat_id"), row.get("name") or "", row.get("category") or "") for row in reader)
    else:
        rows = ((line.strip(), "", "") for line in text.splitlines() if line.strip())
    candidates = {}
    rejected = duplicates = 0
    for identifier, name, category in rows:
        identifier = (identifier or "").strip()
        if not re.fullmatch(r"-[0-9]{1,19}", identifier) or not -(2**63) < int(identifier) < 0:
            rejected += 1
            continue
        chat = int(identifier)
        if chat in candidates:
            duplicates += 1
            continue
        candidates[chat] = (name.strip()[:120], category.strip()[:80])
    return candidates, rejected, duplicates


def import_catalog(db, protector, candidates, now):
    if not candidates:
        return 0
    count = 0
    with transaction(db):
        run_id = db.execute("""INSERT INTO import_runs(source_kind,sha256,source_zone,run_at)
                               VALUES ('catalog',?,'UTC',?)""", (uuid4().hex, now)).lastrowid
        for chat, (name, category) in candidates.items():
            if db.execute("SELECT 1 FROM venues WHERE canonical_chat_id=?", (chat,)).fetchone():
                continue
            payload = protector.protect(json.dumps({"chat_id": chat, "name": name, "category": category},
                                                   ensure_ascii=False).encode("utf-8"))
            db.execute("""INSERT INTO venues(id,run_id,consent,payload,canonical_chat_id)
                          VALUES (?,?,'recheck',?,?)""", (uuid4().hex, run_id, payload, chat))
            count += 1
        if not count:
            db.execute("DELETE FROM import_runs WHERE id=?", (run_id,))
    return count


def search_catalog(db, protector, query=""):
    query = query.strip().casefold()
    results = []
    for (payload,) in db.execute("SELECT payload FROM venues ORDER BY id"):
        data = json.loads(protector.unprotect(payload))
        values = tuple(str(data.get(key) or "") for key in ("chat_id", "name", "category"))
        if any(query in value.casefold() for value in values):
            results.append(values)
    return sorted(results, key=lambda item: (item[1].casefold(), item[0]))

