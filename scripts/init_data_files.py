"""Idempotent setup: creates data/users.csv, this month's chat-log folder under
data/chat_logs/, logs/, and the ChromaDB persist directory if they don't already exist.
Safe to re-run at any time.

Usage:
    python scripts/init_data_files.py
    python scripts/init_data_files.py --merge-pending
    python scripts/init_data_files.py --split-legacy
"""
import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from filelock import Timeout  # noqa: E402

from backend.services.chat_log_store import LegacySplitError  # noqa: E402
from backend.services.logging_service import (  # noqa: E402
    FIELDNAMES,
    ensure_log_file,
    get_chat_log_store,
)
from config.settings import get_settings  # noqa: E402

settings = get_settings()

PLACEHOLDER_USERS = (
    "user_id,user_name,user_email,role\n"
    "U001,Demo User,demo.user@example.com,user\n"
)


def merge_pending() -> int:
    """Fold every *.pending.csv back into the chat-log file it was meant for.

    Rows land there when a log file could not be written - almost always because it was
    open in Excel. Merging is a separate, explicit step so it never races a live writer.
    """
    status = 0
    try:
        outcomes = get_chat_log_store().merge_pending()
    except Timeout:
        print(
            "FAILED: the chat-log lock is held by another process (the backend is writing). "
            "Retry in a few seconds."
        )
        return 1
    for outcome in outcomes:
        if outcome.error:
            status = 1
            print(
                f"FAILED: {outcome.target.name} - {outcome.error} (close it in Excel and "
                f"retry). Rows remain in {outcome.pending.name}."
            )
        else:
            print(
                f"Merged {outcome.rows} pending row(s) into {outcome.target.name} and "
                f"removed {outcome.pending.name}."
            )
    legacy_status = _merge_legacy_pending()
    if not outcomes and legacy_status is None:
        print("No pending chat-log rows to merge.")
    return status or (legacy_status or 0)


def _merge_legacy_pending() -> int | None:
    """data/chat_logs.pending.csv from before the log was partitioned. None = none there."""
    csv_path = settings.resolved(settings.CHAT_LOGS_CSV)
    pending = csv_path.with_name(csv_path.stem + ".pending.csv")
    if not pending.exists() or pending.stat().st_size == 0:
        return None

    with open(pending, "r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        pending.unlink()
        print(f"{pending.name} was empty; removed it.")
        return 0

    try:
        is_new = not csv_path.exists() or csv_path.stat().st_size == 0
        with open(csv_path, "a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
            if is_new:
                writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in FIELDNAMES})
    except PermissionError:
        print(
            f"FAILED: {csv_path.name} is still locked (close it in Excel and retry). "
            f"{len(rows)} row(s) remain in {pending.name}."
        )
        return 1

    pending.unlink()
    print(f"Merged {len(rows)} pending row(s) into {csv_path.name} and removed {pending.name}.")
    return 0


def split_legacy() -> int:
    """Move the old single data/chat_logs.csv into the weekly/monthly partitions."""
    legacy = settings.resolved(settings.CHAT_LOGS_CSV)
    store = get_chat_log_store()
    try:
        report = store.split_legacy(legacy)
    except LegacySplitError as exc:
        print(f"FAILED: {exc}")
        return 1

    print(
        f"Split {report.routed} row(s) from {legacy.name} into {store.root} "
        f"(filed by each session's login time, this machine's time zone)."
    )
    if report.skipped_expired:
        print(
            f"Skipped {report.skipped_expired} row(s) older than {report.oldest_kept} - "
            f"outside CHAT_LOG_KEEP_MONTHS={store.keep_months}."
        )
    if report.skipped_unparseable:
        print(f"Skipped {report.skipped_unparseable} row(s) with an unreadable login_time.")
    if report.skipped_expired or report.skipped_unparseable:
        print(f"Skipped rows are still in {report.migrated_to.name}.")
    for name in report.spilled_to_pending:
        print(
            f"NOTE: a target file was locked; its rows went to {name}. Close Excel and run "
            f"--merge-pending."
        )
    print(f"Renamed {legacy.name} to {report.migrated_to.name} so it cannot be imported twice.")
    return 0


def _ensure_users_file(users_path: Path) -> None:
    if not users_path.exists():
        users_path.parent.mkdir(parents=True, exist_ok=True)
        users_path.write_text(PLACEHOLDER_USERS, encoding="utf-8")
        print(f"Created {users_path} with a placeholder user - edit it to add real authorized users.")
    else:
        print(f"OK: {users_path} already exists.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--merge-pending", action="store_true",
        help="append every *.pending.csv under the chat-log folder (and the old "
             "data/chat_logs.pending.csv) into its main file, then delete it",
    )
    parser.add_argument(
        "--split-legacy", action="store_true",
        help="move the old single data/chat_logs.csv into the weekly/monthly files, then "
             "rename it to chat_logs.migrated.csv",
    )
    args = parser.parse_args()

    if args.merge_pending:
        return merge_pending()
    if args.split_legacy:
        return split_legacy()

    _ensure_users_file(settings.resolved(settings.USERS_CSV))

    ensure_log_file()
    print(f"OK: {settings.resolved(settings.CHAT_LOGS_DIR)} ready "
          f"(keeping {settings.CHAT_LOG_KEEP_MONTHS} month(s)).")
    legacy = settings.resolved(settings.CHAT_LOGS_CSV)
    if legacy.exists():
        print(f"NOTE: {legacy.name} is the old single-file log and is no longer written. "
              f"Import it with: python scripts\\init_data_files.py --split-legacy")

    settings.resolved(settings.LOG_DIR).mkdir(parents=True, exist_ok=True)
    settings.resolved(settings.CHROMA_PERSIST_DIR).mkdir(parents=True, exist_ok=True)
    settings.resolved("./data/cache").mkdir(parents=True, exist_ok=True)
    print("OK: logs/, chroma_store/ and data/cache/ directories ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
