"""Chat audit log partitioning (week1-4 + month files per calendar month) and retention.

Everything runs under tmp_path with a pinned clock - never the project's real data/
folder, and never the real calendar. The guarantees locked down here:

  * week boundaries agree with backend/core/date_windows.py (1-7, 8-14, 15-21, 22-end);
  * every row lands in BOTH its week file and its month file, header written once;
  * a file open in Excel diverts the row to that file's .pending.csv - never dropped;
  * retention deletes only YYYY-MM folders directly under the root, never anything else.
"""
import csv
import importlib.util
import subprocess
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.core import date_windows
from backend.services import chat_log_store as cls_mod
from backend.services import logging_service
from backend.services.chat_log_store import (
    ChatLogStore,
    LegacySplitError,
    utc_iso_to_local,
    week_of_month,
)

FIELDS = logging_service.FIELDNAMES
IST = timezone(timedelta(hours=5, minutes=30))
PROJECT_ROOT = Path(__file__).resolve().parent.parent
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows file locking")


class Clock:
    def __init__(self, when: datetime):
        self.when = when

    def __call__(self) -> datetime:
        return self.when


def _store(tmp_path, when, keep=2):
    clock = Clock(when)
    return ChatLogStore(tmp_path / "chat_logs", FIELDS, keep, clock=clock), clock


def _row(question="q", user_id="U001", login_time="2026-09-01T05:00:00", logout=""):
    return {
        "user_id": user_id, "user_name": "Demo", "user_question": question,
        "generated_sql_query": "SELECT 1", "generated_output": "ok",
        "login_time": login_time, "logout_time": logout, "ques_no": 1,
    }


def _read(path: Path) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _header_count(path: Path) -> int:
    return path.read_text(encoding="utf-8").count("user_id,user_name")


@contextmanager
def _held_like_excel(path: Path):
    """Open `path` the way Excel holds a workbook: others may read, nobody may write,
    rename or delete it until it is closed."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    generic_read_write, file_share_read, open_always = 0xC0000000, 0x1, 4
    handle = kernel32.CreateFileW(
        str(path), generic_read_write, file_share_read, None, open_always, 0x80, None
    )
    if handle in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        kernel32.CloseHandle(handle)


# ======================================================================================
# Week routing
# ======================================================================================

@pytest.mark.parametrize(
    "day, week",
    [(1, 1), (7, 1), (8, 2), (14, 2), (15, 3), (21, 3), (22, 4), (28, 4), (29, 4),
     (30, 4), (31, 4)],
)
def test_week_boundaries(day, week):
    assert week_of_month(day) == week


@pytest.mark.parametrize("when, week", [
    (date(2026, 2, 28), 4),   # non-leap February: week 4 is 22-28, only 7 days
    (date(2028, 2, 29), 4),   # leap day
    (date(2026, 2, 21), 3),
    (date(2026, 2, 22), 4),
])
def test_february_routes_to_the_right_file(tmp_path, when, week):
    store, _ = _store(tmp_path, datetime(2026, 9, 1))
    week_file, month_file = store.targets(when)
    key = f"{when:%Y-%m}"
    assert week_file == store.root / key / f"chat_logs_{key}_week{week}.csv"
    assert month_file == store.root / key / f"chat_logs_{key}_month.csv"


@pytest.mark.parametrize("year, month", [(2026, 2), (2028, 2), (2026, 9), (2026, 12), (2027, 1)])
def test_week_convention_matches_date_windows(year, month):
    """'Week 2' must mean the same days in the audit log as in a '2nd week' question."""
    day = date(year, month, 1)
    while day.month == month:
        start, end = date_windows.week_span(day, week_of_month(day.day))
        assert start <= day < end, (day, start, end)
        day += timedelta(days=1)


def test_file_names_carry_the_month_so_excel_can_open_two_side_by_side(tmp_path):
    store, _ = _store(tmp_path, datetime(2026, 9, 24))
    aug = store.targets(date(2026, 8, 3))[0].name
    sep = store.targets(date(2026, 9, 3))[0].name
    assert aug == "chat_logs_2026-08_week1.csv"
    assert sep == "chat_logs_2026-09_week1.csv"


# ======================================================================================
# Writing
# ======================================================================================

def test_row_goes_to_both_week_and_month_file_with_one_header(tmp_path):
    store, _ = _store(tmp_path, datetime(2026, 9, 10, 14, 0))
    store.append(_row("first"))
    store.append(_row("second"))
    week, month = store.targets(date(2026, 9, 10))
    assert week.name == "chat_logs_2026-09_week2.csv"
    for path in (week, month):
        assert [r["user_question"] for r in _read(path)] == ["first", "second"]
        assert _header_count(path) == 1
    assert list(_read(month)[0]) == FIELDS


def test_rows_route_by_local_date_at_write_time(tmp_path):
    store, clock = _store(tmp_path, datetime(2026, 9, 7, 23, 59))
    store.append(_row("day7"))
    clock.when = datetime(2026, 9, 8, 0, 1)
    store.append(_row("day8"))
    folder = store.root / "2026-09"
    assert [r["user_question"] for r in _read(folder / "chat_logs_2026-09_week1.csv")] == ["day7"]
    assert [r["user_question"] for r in _read(folder / "chat_logs_2026-09_week2.csv")] == ["day8"]
    assert [r["user_question"] for r in _read(folder / "chat_logs_2026-09_month.csv")] == ["day7", "day8"]


def test_ensure_creates_current_week_and_month_files_with_headers(tmp_path):
    store, _ = _store(tmp_path, datetime(2026, 9, 24))
    store.ensure()
    store.ensure()
    for path in store.targets(date(2026, 9, 24)):
        assert path.exists()
        assert _header_count(path) == 1
        assert _read(path) == []


def test_append_after_hand_edit_without_final_newline_does_not_glue_rows(tmp_path):
    store, _ = _store(tmp_path, datetime(2026, 9, 10))
    store.append(_row("first"))
    week, _ = store.targets(date(2026, 9, 10))
    week.write_text(week.read_text(encoding="utf-8").rstrip("\r\n"), encoding="utf-8")
    store.append(_row("second"))
    assert [r["user_question"] for r in _read(week)] == ["first", "second"]


def test_locked_target_spills_to_its_own_pending_file_and_other_target_is_written(tmp_path, monkeypatch):
    store, _ = _store(tmp_path, datetime(2026, 9, 10))
    week, month = store.targets(date(2026, 9, 10))
    real_write = ChatLogStore._append_records

    def excel_holds_week(self, path, records):
        if path == week:
            raise PermissionError(13, "Permission denied", str(path))
        return real_write(self, path, records)

    monkeypatch.setattr(ChatLogStore, "_append_records", excel_holds_week)
    store.append(_row("kept"))

    pending = week.with_name("chat_logs_2026-09_week2.pending.csv")
    assert not week.exists()
    assert [r["user_question"] for r in _read(pending)] == ["kept"]
    assert [r["user_question"] for r in _read(month)] == ["kept"]


@windows_only
def test_file_really_open_in_excel_spills_and_is_merged_back(tmp_path):
    store, _ = _store(tmp_path, datetime(2026, 9, 10))
    store.append(_row("before"))
    week, month = store.targets(date(2026, 9, 10))
    with _held_like_excel(week):
        store.append(_row("during"))
        pending = store.pending_for(week)
        assert [r["user_question"] for r in _read(pending)] == ["during"]
        assert [r["user_question"] for r in _read(month)] == ["before", "during"]
        outcomes = store.merge_pending()
        assert outcomes[0].error and pending.exists()   # still locked: nothing lost
    outcomes = store.merge_pending()
    assert [o.error for o in outcomes] == [""]
    assert not pending.exists()
    assert [r["user_question"] for r in _read(week)] == ["before", "during"]
    assert _header_count(week) == 1


def test_lock_unavailable_spills_both_targets(tmp_path, monkeypatch):
    store, _ = _store(tmp_path, datetime(2026, 9, 10))

    def no_lock(self):
        raise cls_mod.Timeout(str(self.root / ".chat_logs.lock"))

    monkeypatch.setattr(ChatLogStore, "_lock", no_lock)
    store.append(_row("x"))
    week, month = store.targets(date(2026, 9, 10))
    for target in (week, month):
        assert [r["user_question"] for r in _read(store.pending_for(target))] == ["x"]


# ======================================================================================
# Logout back-fill
# ======================================================================================

def test_logout_backfill_across_a_month_boundary(tmp_path, monkeypatch):
    login = "2026-08-31T18:00:00"
    store, clock = _store(tmp_path, datetime(2026, 8, 31, 23, 50))
    store.append(_row("late august", login_time=login))
    store.append(_row("other user", user_id="U009", login_time=login))
    clock.when = datetime(2026, 9, 1, 0, 10)
    store.append(_row("early september", login_time=login))
    store.append(_row("other session", login_time="2026-09-01T00:05:00"))
    # A file with no row of this session must not be rewritten.
    clock.when = datetime(2026, 9, 9)
    store.append(_row("untouched week", login_time="2026-09-09T01:00:00"))

    rewritten = []
    real_rewrite = cls_mod._rewrite
    monkeypatch.setattr(cls_mod, "_rewrite", lambda p, rows: (rewritten.append(p.name), real_rewrite(p, rows)))

    assert store.backfill_logout("U001", login, "2026-09-01T02:00:00") == 4
    assert sorted(rewritten) == sorted([
        "chat_logs_2026-08_week4.csv", "chat_logs_2026-08_month.csv",
        "chat_logs_2026-09_week1.csv", "chat_logs_2026-09_month.csv",
    ])
    for path in store.partition_files():
        for r in _read(path):
            expected = "2026-09-01T02:00:00" if (r["user_id"], r["login_time"]) == ("U001", login) else ""
            assert r["logout_time"] == expected, (path.name, r)


def test_logout_backfill_never_overwrites_an_existing_logout(tmp_path):
    store, _ = _store(tmp_path, datetime(2026, 9, 10))
    store.append(_row(login_time="L", logout="already"))
    assert store.backfill_logout("U001", "L", "new") == 0


@windows_only
def test_logout_backfill_skips_a_locked_file_and_updates_the_rest(tmp_path):
    store, _ = _store(tmp_path, datetime(2026, 9, 10))
    store.append(_row(login_time="L"))
    week, month = store.targets(date(2026, 9, 10))
    with _held_like_excel(week):
        assert store.backfill_logout("U001", "L", "out") == 1
    assert _read(month)[0]["logout_time"] == "out"
    assert _read(week)[0]["logout_time"] == ""
    assert not list(week.parent.glob("*.tmp"))


# ======================================================================================
# Retention
# ======================================================================================

def _make_months(root: Path, *keys):
    for key in keys:
        (root / key).mkdir(parents=True)
        (root / key / f"chat_logs_{key}_month.csv").write_text("user_id\n", encoding="utf-8")


@pytest.mark.parametrize("keep, kept", [
    (1, ["2026-09"]),
    (2, ["2026-08", "2026-09"]),
    (3, ["2026-07", "2026-08", "2026-09"]),
])
def test_retention_keeps_n_months_including_the_current_one(tmp_path, keep, kept):
    store, _ = _store(tmp_path, datetime(2026, 9, 24), keep=keep)
    _make_months(store.root, "2026-05", "2026-06", "2026-07", "2026-08", "2026-09")
    store.ensure()
    assert sorted(p.name for p in store.root.iterdir() if p.is_dir()) == kept


def test_retention_wraps_across_january(tmp_path):
    store, _ = _store(tmp_path, datetime(2026, 1, 15), keep=2)
    _make_months(store.root, "2025-10", "2025-11", "2025-12", "2026-01")
    store.ensure()
    assert sorted(p.name for p in store.root.iterdir() if p.is_dir()) == ["2025-12", "2026-01"]


def test_retention_never_touches_anything_but_expired_yyyy_mm_folders(tmp_path):
    store, _ = _store(tmp_path, datetime(2026, 9, 24), keep=1)
    root = store.root
    _make_months(root, "2020-01")
    for name in ("2026-7", "2025-13", "2025-00", "archive", "2025-01.bak", "x2025-01"):
        (root / name).mkdir(parents=True)
        (root / name / "keep.csv").write_text("x", encoding="utf-8")
    (root / "2024-03").write_text("a FILE named like a month", encoding="utf-8")
    (root / "notes.txt").write_text("x", encoding="utf-8")
    legacy = tmp_path / "chat_logs.csv"
    legacy.write_text("user_id\n", encoding="utf-8")

    store.ensure()

    assert not (root / "2020-01").exists()
    for name in ("2026-7", "2025-13", "2025-00", "archive", "2025-01.bak", "x2025-01"):
        assert (root / name / "keep.csv").exists(), name
    assert (root / "2024-03").is_file()
    assert (root / "notes.txt").exists()
    assert legacy.exists()


def test_retention_leaves_an_expired_folder_that_holds_foreign_files(tmp_path, caplog):
    """CHAT_LOGS_DIR pointed at a folder that already had YYYY-MM subfolders of its own."""
    store, _ = _store(tmp_path, datetime(2026, 9, 24), keep=1)
    _make_months(store.root, "2020-01")
    foreign = store.root / "2020-01" / "budget.xlsx"
    foreign.write_text("not ours", encoding="utf-8")
    leftover_tmp = store.root / "2020-01" / "chat_logs_2020-01_week1.csv.tmp"
    leftover_tmp.write_text("half-written", encoding="utf-8")

    store.ensure()

    assert foreign.exists()
    assert not (store.root / "2020-01" / "chat_logs_2020-01_month.csv").exists()
    assert not leftover_tmp.exists()
    assert "budget.xlsx" in caplog.text


@windows_only
def test_retention_never_deletes_through_a_junction(tmp_path):
    store, _ = _store(tmp_path, datetime(2026, 9, 24))
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "important.csv").write_text("x", encoding="utf-8")
    store.root.mkdir()
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(store.root / "2020-01"), str(outside)],
        capture_output=True, text=True,
    )
    if made.returncode != 0:
        pytest.skip(f"cannot create a junction here: {made.stderr or made.stdout}")
    store.ensure()
    assert (outside / "important.csv").exists()


@windows_only
def test_expired_folder_with_a_file_open_in_excel_is_retried_later(tmp_path, caplog):
    store, clock = _store(tmp_path, datetime(2026, 9, 24))
    _make_months(store.root, "2026-01")
    held = store.root / "2026-01" / "chat_logs_2026-01_month.csv"
    with _held_like_excel(held):
        store.ensure()
        assert held.exists()
    assert "2026-01" in caplog.text and "Close the file in Excel" in caplog.text
    clock.when = datetime(2026, 9, 25)
    store.append(_row())
    assert not (store.root / "2026-01").exists()


def test_retention_from_writes_runs_at_most_once_per_local_day(tmp_path):
    store, clock = _store(tmp_path, datetime(2026, 9, 24, 9, 0))
    _make_months(store.root, "2026-01")
    store.append(_row())
    assert not (store.root / "2026-01").exists()

    _make_months(store.root, "2026-02")
    clock.when = datetime(2026, 9, 24, 23, 59)
    store.append(_row())
    assert (store.root / "2026-02").exists()      # same day: not re-scanned

    clock.when = datetime(2026, 9, 25, 0, 1)
    store.append(_row())
    assert not (store.root / "2026-02").exists()


def test_keep_months_below_one_is_refused(tmp_path):
    with pytest.raises(ValueError, match="CHAT_LOG_KEEP_MONTHS"):
        ChatLogStore(tmp_path, FIELDS, 0)


def test_settings_keep_two_months_by_default_and_reject_below_one():
    from config.settings import Settings

    settings = Settings(_env_file=None)
    assert (settings.CHAT_LOG_KEEP_MONTHS, settings.CHAT_LOGS_DIR) == (2, "./data/chat_logs")
    assert Settings(_env_file=None, CHAT_LOG_KEEP_MONTHS=1).CHAT_LOG_KEEP_MONTHS == 1
    with pytest.raises(Exception, match="CHAT_LOG_KEEP_MONTHS must be 1 or more"):
        Settings(_env_file=None, CHAT_LOG_KEEP_MONTHS=0)


# ======================================================================================
# Maintenance: merge-pending and split-legacy
# ======================================================================================

def test_merge_pending_folds_every_pending_file_into_its_target(tmp_path):
    store, _ = _store(tmp_path, datetime(2026, 9, 10))
    store.append(_row("main"))
    week, month = store.targets(date(2026, 9, 10))
    store._append_records(store.pending_for(week), [_row("spilled")])
    aug_week, _ = store.targets(date(2026, 8, 30))
    aug_week.parent.mkdir(parents=True)
    store._append_records(store.pending_for(aug_week), [_row("august spill")])

    outcomes = store.merge_pending()

    assert sorted((o.target.name, o.rows, o.error) for o in outcomes) == [
        ("chat_logs_2026-08_week4.csv", 1, ""),
        ("chat_logs_2026-09_week2.csv", 1, ""),
    ]
    assert [r["user_question"] for r in _read(week)] == ["main", "spilled"]
    assert [r["user_question"] for r in _read(aug_week)] == ["august spill"]
    assert not list(store.root.rglob("*.pending.csv"))


def _write_legacy(path: Path, rows):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def test_utc_login_time_is_converted_to_local():
    assert utc_iso_to_local("2026-09-07T20:00:00", IST) == datetime(2026, 9, 8, 1, 30)
    assert utc_iso_to_local("not a time", IST) is None


def test_split_legacy_routes_by_local_login_time_and_skips_expired_rows(tmp_path):
    store, _ = _store(tmp_path, datetime(2026, 9, 24), keep=2)
    legacy = tmp_path / "chat_logs.csv"
    _write_legacy(legacy, [
        _row("utc 7th evening = IST 8th", login_time="2026-09-07T20:00:00", logout="x"),
        _row("utc 7th morning", login_time="2026-09-07T04:00:00"),
        _row("previous month", login_time="2026-08-15T06:00:00"),
        _row("too old", login_time="2026-07-31T10:00:00"),
        _row("broken", login_time="garbage"),
    ])

    report = store.split_legacy(legacy, tz=IST)

    assert (report.routed, report.skipped_expired, report.skipped_unparseable) == (3, 1, 1)
    assert report.oldest_kept == "2026-08"
    sep = store.root / "2026-09"
    assert [r["user_question"] for r in _read(sep / "chat_logs_2026-09_week2.csv")] == ["utc 7th evening = IST 8th"]
    assert _read(sep / "chat_logs_2026-09_week2.csv")[0]["logout_time"] == "x"
    assert [r["user_question"] for r in _read(sep / "chat_logs_2026-09_week1.csv")] == ["utc 7th morning"]
    assert len(_read(sep / "chat_logs_2026-09_month.csv")) == 2
    assert [r["user_question"] for r in _read(store.root / "2026-08" / "chat_logs_2026-08_week3.csv")] == ["previous month"]
    assert not (store.root / "2026-07").exists()
    assert not legacy.exists()
    assert len(_read(tmp_path / "chat_logs.migrated.csv")) == 5   # skipped rows kept here

    with pytest.raises(LegacySplitError, match="nothing to split"):
        store.split_legacy(legacy, tz=IST)
    _write_legacy(legacy, [_row(login_time="2026-09-20T04:00:00")])
    with pytest.raises(LegacySplitError, match="split before"):
        store.split_legacy(legacy, tz=IST)


def test_split_legacy_refuses_while_legacy_pending_rows_exist(tmp_path):
    store, _ = _store(tmp_path, datetime(2026, 9, 24))
    legacy = tmp_path / "chat_logs.csv"
    _write_legacy(legacy, [_row()])
    _write_legacy(tmp_path / "chat_logs.pending.csv", [_row()])
    with pytest.raises(LegacySplitError, match="--merge-pending"):
        store.split_legacy(legacy)
    assert legacy.exists() and not store.root.exists()


@windows_only
def test_split_legacy_refuses_cleanly_when_the_legacy_file_is_open_in_excel(tmp_path):
    store, _ = _store(tmp_path, datetime(2026, 9, 24))
    legacy = tmp_path / "chat_logs.csv"
    _write_legacy(legacy, [_row(login_time="2026-09-20T04:00:00")])
    with _held_like_excel(legacy):
        with pytest.raises(LegacySplitError, match="close it in Excel"):
            store.split_legacy(legacy)
    assert legacy.exists()
    assert not (tmp_path / "chat_logs.migrated.csv").exists()
    assert not store.root.exists() or not store.partition_files()


# ======================================================================================
# The public API (logging_service) and the setup script, wired to tmp_path
# ======================================================================================

@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    settings = logging_service.settings
    monkeypatch.setattr(settings, "CHAT_LOGS_DIR", str(tmp_path / "chat_logs"))
    monkeypatch.setattr(settings, "CHAT_LOGS_CSV", str(tmp_path / "chat_logs.csv"))
    monkeypatch.setattr(settings, "USERS_CSV", str(tmp_path / "users.csv"))
    monkeypatch.setattr(settings, "CHAT_LOG_KEEP_MONTHS", 2)
    clock = Clock(datetime(2026, 9, 24, 12, 0))
    monkeypatch.setattr(cls_mod, "_now", clock)
    return tmp_path, clock


def test_logging_service_public_api_writes_partitions(isolated_settings):
    tmp_path, _ = isolated_settings
    logging_service.ensure_log_file()
    logging_service.log_interaction(
        "U002", "harshit", "Top branches?", "SELECT TOP 5\n x", "line1\nline2",
        "2026-09-24T06:00:00", 1,
    )
    logging_service.update_logout_time("U002", "2026-09-24T06:00:00")

    folder = tmp_path / "chat_logs" / "2026-09"
    for name in ("chat_logs_2026-09_week4.csv", "chat_logs_2026-09_month.csv"):
        rows = _read(folder / name)
        assert len(rows) == 1
        assert rows[0]["generated_sql_query"] == "SELECT TOP 5  x"
        assert rows[0]["generated_output"] == "line1 line2"
        assert rows[0]["logout_time"]
    assert not (tmp_path / "chat_logs.csv").exists()


def test_logging_never_breaks_a_response(isolated_settings, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(ChatLogStore, "append", boom)
    monkeypatch.setattr(ChatLogStore, "backfill_logout", boom)
    logging_service.log_interaction("U1", "n", "q", "", "", "t", 1)
    logging_service.update_logout_time("U1", "t")


def _load_init_script():
    spec = importlib.util.spec_from_file_location(
        "init_data_files_under_test", PROJECT_ROOT / "scripts" / "init_data_files.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_setup_script_placeholder_users_file_has_a_role_column(isolated_settings, capsys):
    tmp_path, _ = isolated_settings
    script = _load_init_script()
    script._ensure_users_file(tmp_path / "users.csv")
    assert (tmp_path / "users.csv").read_text(encoding="utf-8") == (
        "user_id,user_name,user_email,role\nU001,Demo User,demo.user@example.com,user\n"
    )


def test_setup_script_merge_pending_handles_partitions_and_the_legacy_pending(isolated_settings, capsys):
    tmp_path, _ = isolated_settings
    script = _load_init_script()
    store = logging_service.get_chat_log_store()
    week, _ = store.targets(date(2026, 9, 24))
    week.parent.mkdir(parents=True)
    store._append_records(store.pending_for(week), [_row("spilled")])
    _write_legacy(tmp_path / "chat_logs.pending.csv", [_row("old spill")])

    assert script.merge_pending() == 0

    out = capsys.readouterr().out
    assert "Merged 1 pending row(s) into chat_logs_2026-09_week4.csv" in out
    assert "Merged 1 pending row(s) into chat_logs.csv" in out
    assert [r["user_question"] for r in _read(week)] == ["spilled"]
    assert [r["user_question"] for r in _read(tmp_path / "chat_logs.csv")] == ["old spill"]
    assert script.merge_pending() == 0
    assert "No pending chat-log rows to merge." in capsys.readouterr().out


def test_setup_script_split_legacy_reports_counts(isolated_settings, capsys):
    tmp_path, _ = isolated_settings
    script = _load_init_script()
    # Midday UTC: the same local date in any time zone from UTC-11 to UTC+12.
    _write_legacy(tmp_path / "chat_logs.csv", [
        _row("kept", login_time="2026-09-10T11:00:00"),
        _row("expired", login_time="2026-06-10T11:00:00"),
    ])
    assert script.split_legacy() == 0
    out = capsys.readouterr().out
    assert "Split 1 row(s)" in out
    assert "Skipped 1 row(s) older than 2026-08" in out
    assert "Renamed chat_logs.csv to chat_logs.migrated.csv" in out
    week = tmp_path / "chat_logs" / "2026-09" / "chat_logs_2026-09_week2.csv"
    assert [r["user_question"] for r in _read(week)] == ["kept"]

    assert script.split_legacy() == 1
    assert "FAILED: No legacy chat log" in capsys.readouterr().out
