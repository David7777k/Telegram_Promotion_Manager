"""Offline smoke entry point for the new package."""
import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from time import time

from .legacy import LegacyInputError, inspect_legacy
from .backup import BackupError, create_backup, restore_backup
from .legacy_archive import stage_legacy
from .legacy_import import import_archived
from .job_queue import QueueConflict, clear_indefinite_hold, control_job, reconcile
from .photos import PhotoError, store_photo
from .diagnostics import read_profile_diagnostics, read_queue_status
from .security import DPAPI, SecretError
from .storage import MigrationError, ProfileBusy, open_profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    command = parser.add_mutually_exclusive_group()
    command.add_argument("--smoke", action="store_true", help="Check the Python and SQLite runtime")
    command.add_argument("--desktop", action="store_true", help="Open the local PySide6 queue window")
    command.add_argument("--inspect-legacy", type=Path, help="Inspect a legacy JSON file read-only")
    command.add_argument("--stage-legacy", type=Path, help="Save an encrypted legacy snapshot")
    command.add_argument("--import-archive", type=int, help="Import a staged snapshot by run ID")
    command.add_argument("--diagnostics", action="store_true", help="Print a report without user data")
    command.add_argument("--queue-status", action="store_true", help="List publication and subscription states")
    command.add_argument("--store-photo", type=Path, help="Store an encrypted photo and print its digest")
    command.add_argument("--queue-action", nargs=2, metavar=("ACTION", "JOB_ID"),
                         help="Pause, resume or cancel an undispatched job")
    command.add_argument("--reconcile", metavar="JOB_ID", help="Resolve a reviewed job locally")
    command.add_argument("--clear-reviewed-hold", metavar="BOT_ID",
                         help="Clear an indefinite hold after checking the bot in Telegram")
    command.add_argument("--backup", type=Path, help="Save a credentials-free SQLite backup")
    command.add_argument("--restore", type=Path, help="Restore a backup to a new profile")
    parser.add_argument("--legacy-version", type=int, choices=(1, 2), default=2)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--source-zone")
    review = parser.add_mutually_exclusive_group()
    review.add_argument("--verified-receipt", nargs=3, type=int,
                        metavar=("CHAT_ID", "MESSAGE_ID", "SENT_AT_UNIX_UTC"))
    review.add_argument("--close-reviewed", action="store_true", help="Close without recording a receipt")
    args = parser.parse_args()
    if args.desktop:
        if args.profile is None:
            parser.error("--profile is required")
        try:
            from .desktop import run_desktop
        except ModuleNotFoundError as error:
            if error.name != "PySide6":
                raise
            parser.exit(2, "PySide6 is not installed. Install the desktop extra.\n")
        parser.exit(run_desktop(args.profile))
    if bool(args.reconcile) != bool(args.verified_receipt or args.close_reviewed):
        parser.error("--reconcile requires --verified-receipt or --close-reviewed")
    if args.queue_action or args.reconcile or args.clear_reviewed_hold:
        if args.profile is None:
            parser.error("--profile is required")
        if args.queue_action and args.queue_action[0] not in ("pause", "resume", "cancel"):
            parser.error("Expected pause, resume or cancel")
        if not (args.profile / "studio.sqlite3").is_file():
            parser.exit(2, "База профиля не найдена.\n")
        try:
            with open_profile(args.profile) as connection:
                now = int(time())
                if args.reconcile:
                    state = reconcile(connection, args.reconcile, now, receipt=args.verified_receipt)
                    report = {"job_id": args.reconcile, "state": state}
                elif args.clear_reviewed_hold:
                    clear_indefinite_hold(connection, args.clear_reviewed_hold, now)
                    report = {"bot_id": args.clear_reviewed_hold, "hold_cleared": True}
                else:
                    action, job_id = args.queue_action
                    state = control_job(connection, job_id, action, now)
                    report = {"job_id": job_id, "state": state}
        except (QueueConflict, ProfileBusy, MigrationError) as error:
            parser.exit(2, f"{error}\n")
        except (OSError, sqlite3.Error):
            parser.exit(2, "Не удалось изменить очередь.\n")
        print(json.dumps(report))
        return
    if args.backup or args.restore:
        if args.profile is None:
            parser.error("--profile is required")
        try:
            if args.backup:
                create_backup(args.profile, args.backup)
            else:
                restore_backup(args.restore, args.profile)
        except (BackupError, ProfileBusy, MigrationError) as error:
            parser.exit(2, f"{error}\n")
        except (OSError, sqlite3.Error):
            parser.exit(2, "Не удалось сохранить или восстановить резервную копию.\n")
        print("Операция завершена.")
        return
    if args.stage_legacy or args.diagnostics or args.queue_status or args.store_photo or args.import_archive is not None:
        if args.profile is None:
            parser.error("--profile is required")
        if args.stage_legacy and not args.source_zone:
            parser.error("--source-zone is required when staging")
        try:
            if args.stage_legacy:
                with open_profile(args.profile) as connection:
                    result = stage_legacy(connection, args.stage_legacy, args.legacy_version,
                                          args.source_zone, DPAPI())
                report = {"run_id": result.run_id, "created": result.created, **result.report}
            elif args.import_archive is not None:
                with open_profile(args.profile) as connection:
                    report = import_archived(connection, args.import_archive, DPAPI())
            elif args.store_photo:
                with open_profile(args.profile) as connection:
                    report = {"photo_digest": store_photo(connection, DPAPI(), args.store_photo)}
            elif args.queue_status:
                report = read_queue_status(args.profile)
            else:
                report = read_profile_diagnostics(args.profile)
            print(json.dumps(report, indent=2))
        except (LegacyInputError, PhotoError, SecretError, ProfileBusy, MigrationError) as error:
            parser.exit(2, f"{error}\n")
        except (OSError, sqlite3.Error):
            parser.exit(2, "Не удалось открыть файл или профиль.\n")
        return
    if args.inspect_legacy:
        try:
            print(json.dumps(inspect_legacy(args.inspect_legacy, args.legacy_version), indent=2))
        except (OSError, LegacyInputError) as error:
            parser.exit(2, f"{error}\n")
        return
    if not args.smoke:
        parser.print_help()
        return
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute("SELECT 1").fetchone()
    print(f"Studio 3 smoke OK. Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}")


if __name__ == "__main__":
    main()

