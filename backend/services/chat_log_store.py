"""Chat audit log, partitioned into weekly and monthly CSV files, with automatic retention.

Layout under CHAT_LOGS_DIR - one folder per calendar month:

    2026-09/chat_logs_2026-09_week1.csv    days 1-7
    2026-09/chat_logs_2026-09_week2.csv    days 8-14
    2026-09/chat_logs_2026-09_week3.csv    days 15-21
    2026-09/chat_logs_2026-09_week4.csv    days 22-end of month (7 to 10 days)
    2026-09/chat_logs_2026-09_month.csv    every row of the month

Every row is appended to its week file AND its month file, routed by the LOCAL date at
write time: the business reads these files by the office calendar. login_time inside the
row stays UTC, as it always has.

The month is in the file NAME, not only the folder, because Excel refuses to open two
workbooks with the same file name at once - and week1 of two months is exactly what
people open side by side.

The week boundaries are the ones backend/core/date_windows.py uses for "1st week"
questions, so "week 2" means the same days everywhere in the app. They are restated here
rather than imported so the audit log depends on nothing but the standard library and
filelock; tests/test_chat_log_partitions.py fails if the two ever disagree.

Retention keeps `keep_months` calendar months INCLUDING the current one and deletes older
month folders. Only folders named exactly YYYY-MM directly under the root are ever
deleted, and never through a link.
"""
from __future__ import annotations

import csv
import io
import os
import re
import stat
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, tzinfo
from pathlib import Path
from typing import Callable, Sequence

from filelock import FileLock, Timeout

from backend.core.logging_config import get_logger

logger = get_logger(__name__)

LOCK_TIMEOUT_SECONDS = 10
PENDING_SUFFIX = ".pending.csv"
MERGE_HINT = r"python scripts\init_data_files.py --merge-pending"

_MONTH_DIR_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_PARTITION_FILE_RE = re.compile(
    r"^chat_logs_\d{4}-(?:0[1-9]|1[0-2])_(?:week[1-4]|month)(?:\.pending)?\.csv$"
)

# Root -> local day retention last ran. Per process, so each worker purges at most once a
# day; the directory lock makes concurrent purges harmless.
_purged_on: dict[str, date] = {}
_purged_lock = threading.Lock()


def _now() -> datetime:
    """Local wall-clock time. Module-level so tests can pin the calendar."""
    return datetime.now()


def week_of_month(day: int) -> int:
    return min((day - 1) // 7 + 1, 4)


def month_key(when: date) -> str:
    return f"{when.year:04d}-{when.month:02d}"


def _month_index(year: int, month: int) -> int:
    return year * 12 + month - 1


def utc_iso_to_local(value: str, tz: tzinfo | None = None) -> datetime | None:
    """A stored login_time (naive UTC ISO) as a naive local datetime; None if unparseable.

    `tz=None` means this machine's time zone - the calendar the partitions follow.
    """
    try:
        parsed = datetime.fromisoformat(str(value or "").strip())
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(tz).replace(tzinfo=None)
    except (ValueError, OverflowError, OSError):
        return None


_NAME_SURROGATE_REPARSE_BIT = 0x20000000


def _is_link(path: Path) -> bool:
    # Path.is_symlink() misses NTFS junctions on Python 3.11, and deleting "through" one
    # would delete files that live somewhere else entirely. The name-surrogate bit marks
    # symlinks and junctions but not OneDrive cloud files, which are safe to treat as
    # ordinary files.
    try:
        info = path.lstat()
    except OSError:
        return True
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_reparse_tag", 0) & _NAME_SURROGATE_REPARSE_BIT)


class LegacySplitError(RuntimeError):
    """The legacy single-file log cannot be split; the message names why and the fix."""


@dataclass
class MergeOutcome:
    pending: Path
    target: Path
    rows: int
    error: str = ""


@dataclass
class SplitReport:
    migrated_to: Path
    oldest_kept: str
    routed: int = 0
    skipped_expired: int = 0
    skipped_unparseable: int = 0
    spilled_to_pending: list[str] = field(default_factory=list)


class ChatLogStore:
    def __init__(
        self,
        root: Path,
        fieldnames: Sequence[str],
        keep_months: int,
        clock: Callable[[], datetime] | None = None,
    ):
        if keep_months < 1:
            raise ValueError(
                f"CHAT_LOG_KEEP_MONTHS must be 1 or more (it counts the current month), "
                f"got {keep_months}."
            )
        self.root = Path(root)
        self.fieldnames = list(fieldnames)
        self.keep_months = keep_months
        self._clock = clock

    # -- layout ------------------------------------------------------------------------

    def now(self) -> datetime:
        return self._clock() if self._clock else _now()

    def targets(self, when: date) -> tuple[Path, Path]:
        """(week file, month file) that a row written at local time `when` belongs in."""
        key = month_key(when)
        folder = self.root / key
        return (
            folder / f"chat_logs_{key}_week{week_of_month(when.day)}.csv",
            folder / f"chat_logs_{key}_month.csv",
        )

    @staticmethod
    def pending_for(target: Path) -> Path:
        return target.with_name(target.stem + PENDING_SUFFIX)

    def oldest_kept_index(self, now: datetime) -> int:
        return _month_index(now.year, now.month) - (self.keep_months - 1)

    def _lock(self) -> FileLock:
        # One lock for the whole directory: writes are tiny, and a single lock keeps the
        # week file and the month file consistent with each other.
        self.root.mkdir(parents=True, exist_ok=True)
        return FileLock(str(self.root / ".chat_logs.lock"), timeout=LOCK_TIMEOUT_SECONDS)

    def _month_dirs(self) -> list[tuple[int, Path]]:
        try:
            entries = sorted(self.root.iterdir())
        except FileNotFoundError:
            return []
        found = []
        for entry in entries:
            match = _MONTH_DIR_RE.match(entry.name)
            if match and entry.is_dir() and not _is_link(entry):
                found.append((_month_index(int(match[1]), int(match[2])), entry))
        return found

    def partition_files(self) -> list[Path]:
        """Every week/month/pending file in every month folder still on disk."""
        files = []
        for _, folder in self._month_dirs():
            files.extend(
                sorted(p for p in folder.iterdir() if _PARTITION_FILE_RE.match(p.name))
            )
        return files

    # -- writing -----------------------------------------------------------------------

    def ensure(self) -> None:
        """Startup hook: create this month's folder and files, then apply retention."""
        now = self.now()
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            with self._lock():
                for target in self.targets(now):
                    try:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        self._append_records(target, [])
                    except OSError as exc:
                        logger.warning("Could not create chat log file %s: %s", target, exc)
                self._purge_expired(now)
        except Timeout:
            logger.warning(
                "Chat log lock %s is held by another process; startup skipped creating "
                "this month's files (they are created on the first write).",
                self.root / ".chat_logs.lock",
            )

    def append(self, row: dict) -> None:
        """Append one row to its week and month files. Never raises.

        A file that cannot be written (almost always: open in Excel) gets the row in its
        own .pending.csv sibling instead, and the other file is still written - an audit
        row is never dropped.
        """
        now = self.now()
        record = {key: row.get(key, "") for key in self.fieldnames}
        targets = self.targets(now)
        try:
            with self._lock():
                self._maybe_purge(now)
                for target in targets:
                    self._append_or_spill(target, [record])
            return
        except Exception as exc:  # noqa: BLE001 - the lock itself was unavailable
            reason = f"chat log lock unavailable: {exc}"
        for target in targets:
            self._spill(target, [record], reason)

    def _append_or_spill(self, target: Path, records: list[dict]) -> bool:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self._append_records(target, records)
            return True
        except Exception as exc:  # noqa: BLE001
            self._spill(target, records, str(exc))
            return False

    def _spill(self, target: Path, records: list[dict], reason: str) -> None:
        pending = self.pending_for(target)
        try:
            pending.parent.mkdir(parents=True, exist_ok=True)
            self._append_records(pending, records)
            logger.warning(
                "Could not write %s (%s). %d row(s) appended to %s instead - close the "
                "file in Excel and run: %s",
                target.name, reason, len(records), pending.name, MERGE_HINT,
            )
        except Exception as exc:  # noqa: BLE001 - never let logging break a response
            logger.error(
                "Chat log row(s) lost: neither %s nor %s could be written: %s",
                target.name, pending.name, exc,
            )

    def _append_records(self, path: Path, records: list[dict]) -> None:
        """Header when the file is new or empty; one write call so rows never interleave."""
        is_new = not path.exists() or path.stat().st_size == 0
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=self.fieldnames)
        if is_new:
            writer.writeheader()
        elif records and not _ends_with_newline(path):
            # A file hand-edited and saved without a final newline would otherwise glue
            # the next audit row onto its last line.
            buffer.write("\r\n")
        writer.writerows(records)
        if buffer.tell():
            with open(path, "a", encoding="utf-8", newline="") as handle:
                handle.write(buffer.getvalue())

    def _read_records(self, path: Path) -> list[dict]:
        # utf-8-sig: Excel's "CSV UTF-8" save prepends a BOM that would glue onto user_id.
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            return [
                {key: (row.get(key) or "") for key in self.fieldnames}
                for row in csv.DictReader(handle)
            ]

    # -- logout back-fill --------------------------------------------------------------

    def backfill_logout(self, user_id: str, login_time: str, logout_time: str) -> int:
        """Fill logout_time on this session's rows in every retained file. Never raises.

        A session can span a week or a month boundary, so every partition is checked;
        only files that actually changed are rewritten.
        """
        updated = 0
        try:
            with self._lock():
                for path in self.partition_files():
                    try:
                        updated += self._backfill_file(path, user_id, login_time, logout_time)
                    except Exception as exc:  # noqa: BLE001 - one locked file must not stop the rest
                        logger.warning(
                            "Could not back-fill logout_time in %s (close it in Excel): %s",
                            path.name, exc,
                        )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to update logout_time (non-fatal): %s", exc)
        return updated

    @staticmethod
    def _backfill_file(path: Path, user_id: str, login_time: str, logout_time: str) -> int:
        # Plain rows, not dicts, so a rewrite reproduces every cell exactly - even in a
        # file someone reshaped in Excel.
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        if not rows:
            return 0
        header = rows[0]
        try:
            uid, login, logout = (header.index(c) for c in ("user_id", "login_time", "logout_time"))
        except ValueError:
            return 0
        needed = max(uid, login, logout)
        changed = 0
        for row in rows[1:]:
            if len(row) <= needed:
                continue
            if row[uid] == user_id and row[login] == login_time and not row[logout]:
                row[logout] = logout_time
                changed += 1
        if changed:
            _rewrite(path, rows)
        return changed

    # -- retention ---------------------------------------------------------------------

    def _maybe_purge(self, now: datetime) -> None:
        with _purged_lock:
            if _purged_on.get(str(self.root)) == now.date():
                return
        self._purge_expired(now)

    def purge_expired(self) -> list[Path]:
        """Apply retention now. Returns the month folders removed. Never raises."""
        now = self.now()
        try:
            with self._lock():
                return self._purge_expired(now)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Chat log retention skipped (retried next time): %s", exc)
            return []

    def _purge_expired(self, now: datetime) -> list[Path]:
        with _purged_lock:
            _purged_on[str(self.root)] = now.date()
        oldest = self.oldest_kept_index(now)
        removed = []
        try:
            month_dirs = self._month_dirs()
        except OSError as exc:
            logger.warning("Chat log retention could not list %s: %s", self.root, exc)
            return removed
        for index, folder in month_dirs:
            if index >= oldest:
                continue
            try:
                if _remove_month_dir(folder):
                    removed.append(folder)
            except Exception as exc:  # noqa: BLE001 - retention must never break a write
                logger.warning(
                    "Chat log retention could not delete expired folder %s (retried next "
                    "time): %s", folder, exc,
                )
        if removed:
            logger.info(
                "Chat log retention (keep %d month(s)) deleted: %s",
                self.keep_months, ", ".join(p.name for p in removed),
            )
        return removed

    # -- maintenance (scripts/init_data_files.py) --------------------------------------

    def merge_pending(self) -> list[MergeOutcome]:
        """Fold every *.pending.csv back into the file it was meant for.

        Raises filelock.Timeout if the backend holds the lock for longer than the timeout.
        """
        outcomes = []
        with self._lock():
            for pending in self.partition_files():
                if not pending.name.endswith(PENDING_SUFFIX):
                    continue
                target = pending.with_name(pending.name[: -len(PENDING_SUFFIX)] + ".csv")
                try:
                    rows = self._read_records(pending)
                    if rows:
                        self._append_records(target, rows)
                except OSError as exc:
                    outcomes.append(MergeOutcome(pending, target, 0, f"{exc}"))
                    continue
                try:
                    pending.unlink()
                except OSError as exc:
                    outcomes.append(MergeOutcome(
                        pending, target, len(rows),
                        f"rows were merged but {pending.name} could not be deleted ({exc}) - "
                        f"delete it by hand, or the next merge will add them twice",
                    ))
                    continue
                outcomes.append(MergeOutcome(pending, target, len(rows)))
        return outcomes

    def split_legacy(self, legacy: Path, tz: tzinfo | None = None) -> SplitReport:
        """Route the pre-partition single log into the partitions by each row's login_time.

        The legacy file is RENAMED to <name>.migrated.csv first: a file locked by Excel is
        refused before any row is written, and a second run can never import the same rows
        twice. Rows outside the retention window are skipped, but stay in the renamed file.
        The legacy file has no per-question timestamp, so a row is filed under its
        session's login time.
        """
        legacy = Path(legacy)
        migrated = legacy.with_name(legacy.stem + ".migrated.csv")
        legacy_pending = legacy.with_name(legacy.stem + PENDING_SUFFIX)
        if not legacy.exists():
            raise LegacySplitError(f"No legacy chat log at {legacy} - nothing to split.")
        if legacy_pending.exists() and legacy_pending.stat().st_size:
            raise LegacySplitError(
                f"{legacy_pending.name} still holds rows that never reached {legacy.name}. "
                f"Run: {MERGE_HINT}  and then split again."
            )
        if migrated.exists():
            raise LegacySplitError(
                f"{migrated.name} already exists, so the legacy log was split before. If "
                f"{legacy.name} really holds new rows, move {migrated.name} elsewhere first."
            )
        try:
            legacy.rename(migrated)
        except PermissionError as exc:
            raise LegacySplitError(
                f"{legacy.name} is locked - close it in Excel and retry. Nothing was "
                f"changed. ({exc})"
            ) from exc

        oldest = self.oldest_kept_index(self.now())
        year, month_zero_based = divmod(oldest, 12)
        report = SplitReport(migrated_to=migrated, oldest_kept=f"{year:04d}-{month_zero_based + 1:02d}")
        try:
            rows = self._read_records(migrated)
            batches: dict[Path, list[dict]] = {}
            for row in rows:
                when = utc_iso_to_local(row["login_time"], tz)
                if when is None:
                    report.skipped_unparseable += 1
                    continue
                if _month_index(when.year, when.month) < oldest:
                    report.skipped_expired += 1
                    continue
                for target in self.targets(when):
                    batches.setdefault(target, []).append(row)
                report.routed += 1
            lock = self._lock()
            lock.acquire()
        except Exception as exc:
            # Nothing has been written yet, so undo the rename and leave everything as found.
            migrated.rename(legacy)
            raise LegacySplitError(f"Split aborted, {legacy.name} left unchanged: {exc}") from exc
        try:
            for target, batch in batches.items():
                if not self._append_or_spill(target, batch):
                    report.spilled_to_pending.append(self.pending_for(target).name)
        finally:
            lock.release()
        return report


def _ends_with_newline(path: Path) -> bool:
    with open(path, "rb") as handle:
        handle.seek(-1, os.SEEK_END)
        return handle.read(1) in (b"\n", b"\r")


def _rewrite(path: Path, rows: list[list[str]]) -> None:
    # Written beside the target and swapped in, so a crash mid-write can never leave a
    # truncated audit file. os.replace fails cleanly while Excel holds the target open.
    temp = path.with_name(path.name + ".tmp")
    try:
        with open(temp, "w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(rows)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _is_chat_log_file(name: str) -> bool:
    # A .tmp is what _rewrite leaves if the process died mid-swap.
    return bool(_PARTITION_FILE_RE.match(name[:-4] if name.endswith(".tmp") else name))


def _remove_month_dir(folder: Path) -> bool:
    """Delete an expired month folder's chat-log files, then the folder. Never recurses.

    Anything else in it is left alone, and so is the folder: if CHAT_LOGS_DIR were ever
    pointed at a directory that holds other YYYY-MM folders, retention must not empty them.
    """
    leftovers = []
    for item in sorted(folder.iterdir()):
        if item.is_dir() or _is_link(item):
            leftovers.append(f"{item.name} (not a plain file)")
            continue
        if not _is_chat_log_file(item.name):
            leftovers.append(f"{item.name} (not a chat log file - left in place)")
            continue
        try:
            item.unlink()
        except OSError as exc:
            leftovers.append(f"{item.name} ({exc.strerror or exc})")
    if not leftovers:
        try:
            folder.rmdir()
            return True
        except OSError as exc:
            leftovers.append(f"folder itself ({exc.strerror or exc})")
    logger.warning(
        "Chat log retention could not fully delete expired folder %s: %s. Close the file "
        "in Excel - deletion is retried on the next backend start or the next day.",
        folder, "; ".join(leftovers),
    )
    return False
