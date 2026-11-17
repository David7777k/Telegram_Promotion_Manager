"""Verify the distributed archive on a temporary profile with no Python on PATH."""
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile


def verify(archive, fixture):
    fixture = fixture.resolve()
    environment = os.environ.copy()
    windows = Path(os.environ["SystemRoot"])
    environment["PATH"] = os.pathsep.join((str(windows / "System32"), str(windows)))
    environment.pop("PYTHONPATH", None)
    with TemporaryDirectory(prefix="Studio-архив-") as directory:
        root = Path(directory)
        with ZipFile(archive) as bundle:
            bad = bundle.testzip()
            if bad is not None:
                raise RuntimeError(f"Archive integrity error: {bad}")
            bundle.extractall(root)
        exe = root / "TelegramPromotionStudio" / "TelegramPromotionStudio.exe"
        if not any(exe.parent.rglob("qwindows.dll")):
            raise RuntimeError("Qt Windows plugin is missing")

        def run(*args):
            return subprocess.run([str(exe), *map(str, args)], cwd=root, env=environment,
                                  check=True, timeout=40, capture_output=True).stdout

        if b"Packaged Windows Qt, SQLite, Telethon, QR and tzdata smoke OK" not in run("--package-smoke"):
            raise RuntimeError("Packaged Qt smoke output missing")
        profile, restored = root / "Данные", root / "Восстановлено"
        backup = root / "Резерв.sqlite3"
        staged = json.loads(run("--profile", profile, "--stage-legacy", fixture, "--source-zone", "Europe/Kyiv"))
        run("--profile", profile, "--import-archive", staged["run_id"])
        run("--profile", profile, "--backup", backup)
        run("--profile", restored, "--restore", backup)
        report = json.loads(run("--profile", restored, "--diagnostics"))
        status = json.loads(run("--profile", restored, "--queue-status"))
        if not report["integrity_ok"] or report["foreign_key_violations"]:
            raise RuntimeError("Restored SQLite checks failed")
        states = sorted(job["state"] for job in status["jobs"])
        if states != ["needs_review", "needs_review", "needs_review", "paused", "sent"]:
            raise RuntimeError(f"Unexpected restored queue: {states}")
        print("Extracted EXE passed Qt startup, Kyiv timezone, Cyrillic paths, v2 import, backup and restore.")


if __name__ == "__main__":
    verify(Path(sys.argv[1]), Path(sys.argv[2]))

