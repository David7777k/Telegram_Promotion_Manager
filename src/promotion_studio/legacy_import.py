"""Promote a staged legacy snapshot into inert, relational application data."""
import json
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .legacy import LegacyInputError, inspect_bytes
from .schedule_time import resolve_local


FIELDS = {
    "campaigns": "name channel channel_chat_id description topic link price budget_uah active content_ready created_at",
    "venues": "name label chat_id topic audience contact cost_uah approved approval_note active cooldown_days last_sent_at type",
    "templates": "name body",
    "jobs": "scheduled_at sent_at message_id error source_link views clicks paid actual_cost_uah created_at rendered_text price_at_send",
    "activity": "at level message",
}
STATES = {"planned": "paused", "sending": "needs_review", "sent": "sent",
          "failed": "needs_review", "blocked": "needs_review"}


def _epoch(value, zone):
    if not value:
        return None
    local = datetime.strptime(value, "%Y-%m-%d %H:%M")
    return int(resolve_local(local, zone).timestamp())


def _v1_id(kind, value):
    return uuid5(NAMESPACE_URL, f"promotion-studio/v1/{kind}/{value}").hex


def _v1_rows(data):
    templates = data.get("templates")
    last_sent = data.get("last_sent")
    targets = data.get("targets")
    targets = targets if isinstance(targets, list) else []
    rows = []
    for target in targets:
        if not isinstance(target, dict) or not isinstance(target.get("id"), str) or not target["id"]:
            rows.append(None)
            continue
        key = target["id"]
        row = dict(target, id=_v1_id("venue", key))
        if isinstance(last_sent, dict) and key in last_sent:
            row["legacy_last_sent_epoch"] = last_sent[key]
        rows.append(row)
    return {
        "campaigns": [dict(id=_v1_id("campaign", "primary"), name="Импорт из первой версии",
                           channel=data.get("channel", ""), link=data.get("link", ""),
                           price=data.get("price", 250), content_ready=False)]
        if data.get("channel") or data.get("link") else [],
        "targets": rows,
        "templates": [dict(id=_v1_id("template", name), name=name, body=body)
                      for name, body in templates.items() if isinstance(body, str)]
        if isinstance(templates, dict) else [],
    }


def _cost_minor(value):
    if value is None:
        return None
    try:
        minor = Decimal(str(value)) * 100
        if not minor.is_finite() or minor < 0 or minor != minor.to_integral_value() or minor >= 2**63:
            raise ValueError
        return int(minor)
    except (InvalidOperation, ValueError):
        raise ValueError("Invalid legacy cost") from None


def import_archived(connection, run_id, protector):
    """Import one explicitly selected snapshot. Conflicting records stay in its archive."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing = connection.execute("SELECT report_json FROM legacy_import_results WHERE run_id=?",
                                      (run_id,)).fetchone()
        if existing is not None:
            connection.commit()
            return {"created": False, **json.loads(existing[0])}
        record = connection.execute(
            """SELECT source_kind, sha256, source_zone, ciphertext
               FROM import_runs JOIN legacy_archives ON run_id=import_runs.id WHERE import_runs.id=?""",
            (run_id,),
        ).fetchone()
        if record is None:
            raise LegacyInputError("Архив с указанным номером не найден.")
        kind, digest, zone_name, ciphertext = record
        raw = protector.unprotect(ciphertext)
        if sha256(raw).hexdigest() != digest:
            raise LegacyInputError("Контрольная сумма расшифрованного архива не совпала.")
        version = 1 if kind == "v1" else 2
        inspect_bytes(raw, version)
        try:
            zone = timezone.utc if zone_name == "UTC" else ZoneInfo(zone_name)
        except ZoneInfoNotFoundError as exc:
            raise LegacyInputError("Часовой пояс архива недоступен.") from exc
        data = json.loads(raw.decode("utf-8-sig"))
        rows = _v1_rows(data) if version == 1 else data
        report = _import_rows(connection, run_id, rows, zone, protector)
        connection.execute("INSERT INTO legacy_import_results VALUES (?, ?)",
                           (run_id, json.dumps(report, sort_keys=True)))
        connection.commit()
        return {"created": True, **report}
    except BaseException:
        connection.rollback()
        raise


def _import_rows(db, run_id, rows, zone, protector):
    counts, issues = Counter(), Counter()

    def issue(table, index, code):
        issues[f"{table}.{code}"] += 1
        db.execute(
            "INSERT INTO import_issues(run_id,entity_kind,reason_code,raw_ref) VALUES (?,?,?,?)",
            (run_id, table, code, f"archive:{run_id}/{table}/{index}"),
        )

    accepted = {}
    for source, table in (("campaigns", "campaigns"), ("targets", "venues"),
                          ("templates", "templates"), ("posts", "jobs"), ("activity", "activity")):
        records = rows.get(source, [])
        if not isinstance(records, list):
            issue(table, "-", "invalid_collection")
            continue
        accepted[table] = set()
        frequencies = Counter(row["id"] for row in records
                              if isinstance(row, dict) and isinstance(row.get("id"), str))
        for index, row in enumerate(records):
            key = row.get("id") if isinstance(row, dict) else None
            if not isinstance(row, dict) or (table != "activity" and
                                            (not isinstance(key, str) or not key.strip())):
                issue(table, index, "invalid_record")
                continue
            if table != "activity" and frequencies[key] > 1:
                issue(table, index, "duplicate_id")
                continue
            payload = {field: row[field] for field in FIELDS[table].split() if field in row}
            cipher = protector.protect(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            if table == "activity":
                db.execute("INSERT INTO legacy_activity VALUES (?,?,?)", (run_id, index, cipher))
            elif db.execute(f"SELECT 1 FROM {table} WHERE id=?", (key,)).fetchone():
                issue(table, index, "existing_id")
                continue
            elif table == "jobs":
                refs = tuple(row.get(field) for field in ("campaign_id", "target_id", "template_id"))
                if any(not isinstance(ref, str) or ref not in accepted.get(target, ())
                       for ref, target in zip(refs, ("campaigns", "venues", "templates"))):
                    issue(table, index, "unresolved_reference")
                    continue
                status = row.get("status")
                if not isinstance(status, str) or status not in STATES:
                    issue(table, index, "invalid_status")
                    continue
                state = STATES[status]
                dates = []
                for field in ("scheduled_at", "sent_at"):
                    try:
                        dates.append(_epoch(row.get(field), zone))
                    except (TypeError, ValueError, OverflowError, OSError):
                        dates.append(None)
                        if state != "sent":
                            state = "needs_review"
                        issue(table, index, f"invalid_{field}")
                scheduled, sent = dates
                receipt = row.get("message_id")
                if state == "sent" and (
                    sent is None or type(receipt) not in (int, str)
                    or not str(receipt).isascii() or not str(receipt).isdigit()
                    or not str(receipt).lstrip("0")
                ):
                    state = "needs_review"
                    issue(table, index, "missing_receipt")
                try:
                    cost = _cost_minor(row.get("actual_cost_uah"))
                except ValueError:
                    cost = None
                    issue(table, index, "invalid_cost")
                price = row.get("price_at_send")
                price = price if type(price) is int and 1 <= price <= 10000 else None
                db.execute("""INSERT INTO jobs(id,run_id,campaign_id,venue_id,template_id,state,
                                scheduled_utc,sent_at,price_at_approval,actual_cost_minor,payload)
                              VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                           (key, run_id, *refs, state, scheduled, sent, price, cost, cipher))
            elif table == "venues":
                last = None
                try:
                    if "legacy_last_sent_epoch" in row:
                        epoch = row["legacy_last_sent_epoch"]
                        if isinstance(epoch, bool):
                            raise ValueError
                        last = int(datetime.fromtimestamp(float(epoch), timezone.utc).timestamp())
                    else:
                        last = _epoch(row.get("last_sent_at"), zone)
                except (TypeError, ValueError, OverflowError, OSError):
                    issue(table, index, "invalid_local_time")
                db.execute("""INSERT INTO venues(id,run_id,consent,legacy_last_sent_at,payload)
                              VALUES (?,?,?,?,?)""", (key, run_id, "recheck", last, cipher))
            elif table == "templates":
                db.execute("INSERT INTO templates VALUES (?,?,?,?)", (key, run_id, 2, cipher))
            else:
                db.execute("INSERT INTO campaigns VALUES (?,?,?,?)", (key, run_id, "draft", cipher))
            accepted[table].add(key)
            counts[table] += 1
    legacy_settings = rows.get("settings")
    if isinstance(legacy_settings, dict):
        for name, minimum, maximum in (("pacing_sec", 3, 3600), ("default_cooldown_days", 1, 365)):
            value = legacy_settings.get(name)
            if type(value) is int and minimum <= value <= maximum:
                db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (name, json.dumps(value)))
    selected = rows.get("selected_campaign")
    if isinstance(selected, str) and selected in accepted.get("campaigns", ()):
        db.execute("INSERT OR IGNORE INTO settings VALUES ('selected_campaign',?)", (json.dumps(selected),))
    return {"run_id": run_id, "records": dict(counts), "issues": dict(issues)}

