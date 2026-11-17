"""Report known publication outcomes and legacy metrics without inferring payments."""
import csv
from collections import Counter
from pathlib import Path


def queue_report(db):
    counts = Counter(dict(db.execute("SELECT state,COUNT(*) FROM jobs GROUP BY state").fetchall()))
    known = db.execute("""SELECT COUNT(actual_cost_minor),SUM(actual_cost_minor)
                          FROM jobs WHERE state='sent'""").fetchone()
    return {"states": dict(sorted(counts.items())), "known_cost_records": known[0],
            "known_cost_uah_minor": known[1]}


def export_report(db, destination: Path):
    report = queue_report(db)
    with destination.open("x", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("metric", "value"))
        for state, value in report["states"].items():
            writer.writerow((state, value))
        writer.writerow(("known_cost_records", report["known_cost_records"]))
        writer.writerow(("known_cost_uah_minor", report["known_cost_uah_minor"]))
    return report

