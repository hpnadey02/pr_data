"""Deterministic date-window resolution.

Date questions are resolved to CONCRETE dates here, in Python, before the SQL model is
asked anything. The model then receives a literal range as a fact:

    [POLICY_ISSUE_DATE] >= '2026-09-08' AND [POLICY_ISSUE_DATE] < '2026-09-15'

rather than being asked to write `MAX()` subqueries and month arithmetic itself. A 7B
local model gets that wrong often, and a wrong date range produces a confidently wrong
answer with no error to catch it. This mirrors how identifier filters are already bound in
query_understanding.py.

The rules, in priority order:

1. A date column named by the user wins outright ("...based on CLAIM_DATE").
2. Otherwise POLICY_ISSUE_DATE is the default.
3. "1st/2nd/3rd/4th week" means that week of the LATEST MONTH PRESENT IN THE DATA -
   found via MAX(date column), never the current calendar month. Asking for "the 1st
   week" when the data ends in August must not return an empty August-of-next-month.
       1st = day 1-7 · 2nd = 8-14 · 3rd = 15-21 · 4th = 22-end of month
4. "Yearly trend" means the CURRENT CALENDAR MONTH compared across the available years -
   not the whole dataset grouped by year. These two rules are different and must not be
   mixed.

Ranges are half-open (`>= start AND < end`) so a column carrying a time component cannot
silently drop the last day, which `BETWEEN` would do.
"""
from __future__ import annotations

import calendar
import re
import threading
import time
from dataclasses import dataclass
from datetime import date

from backend.core.logging_config import get_logger
from config.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()

DEFAULT_DATE_COLUMN = "POLICY_ISSUE_DATE"

# How long a MAX(date) lookup stays valid. Short, because on a live database new policies
# arrive continuously and "the latest month" would otherwise freeze at process start.
_BOUNDS_TTL_SECONDS = 900

_WEEK_ORDINALS = {
    "1st": 1, "first": 1,
    "2nd": 2, "second": 2,
    "3rd": 3, "third": 3,
    "4th": 4, "fourth": 4,
}
_WEEK_RE = re.compile(
    r"\b(" + "|".join(_WEEK_ORDINALS) + r")\s+week\b", re.IGNORECASE
)
_YEARLY_TREND_RE = re.compile(
    r"\byear(?:ly|\s*-?\s*wise|\s*over\s*year)\s+(?:trend|analysis|data|comparison)\b"
    r"|\bannual\s+trend\b|\byearly\s+trend\b",
    re.IGNORECASE,
)
# "based on CLAIM_DATE", "using POLICY_ISSUE_DATE", "by VOUCHER_DATE"
_EXPLICIT_COLUMN_RE = re.compile(
    r"\b(?:based\s+on|using|use|as\s+per|on\s+the\s+basis\s+of|by)\s+"
    r"(?P<column>[A-Za-z][A-Za-z0-9_ ]{2,48})",
    re.IGNORECASE,
)

# Week n -> (first day, first day of the NEXT window). 4 runs to the end of the month.
_WEEK_SPANS = {1: (1, 8), 2: (8, 15), 3: (15, 22), 4: (22, None)}


@dataclass(frozen=True)
class DateWindow:
    """A resolved date instruction, ready to be stated to the SQL model as fact."""

    column: str
    mode: str                     # "week" | "yearly_trend"
    start: date | None = None     # inclusive
    end: date | None = None       # EXCLUSIVE
    month: int | None = None      # yearly_trend: restrict to this calendar month
    label: str = ""               # human-readable, surfaced to the user

    def as_sql_instruction(self) -> str:
        """The exact clause the model must use. Literal, never GETDATE()."""
        if self.mode == "yearly_trend":
            return (
                f"- DATE RULE (yearly trend): compare the same calendar month across "
                f"years. Use EXACTLY:\n"
                f"    WHERE MONTH([{self.column}]) = {self.month}\n"
                f"    GROUP BY YEAR([{self.column}])\n"
                f"    ORDER BY YEAR([{self.column}])\n"
                f"  Select YEAR([{self.column}]) AS year plus the aggregated measure. Do "
                f"NOT group the whole dataset by year, and do NOT call GETDATE().\n"
            )
        return (
            f"- DATE RULE: filter on [{self.column}] using EXACTLY this half-open range:\n"
            f"    [{self.column}] >= '{self.start:%Y-%m-%d}' "
            f"AND [{self.column}] < '{self.end:%Y-%m-%d}'\n"
            f"  These dates are already correct - do NOT call GETDATE(), MAX() or compute "
            f"them yourself.\n"
        )


# ======================================================================================
# Latest date present in the data
# ======================================================================================

_bounds_cache: dict[str, tuple[float, date | None]] = {}
_bounds_lock = threading.Lock()


def latest_date(column: str) -> date | None:
    """MAX(column) as actually stored, or None if unavailable.

    Never raises: if the data source is unreachable the caller simply gets no window and
    the model falls back to whatever the question said.
    """
    now = time.time()
    with _bounds_lock:
        cached = _bounds_cache.get(column)
        if cached and now - cached[0] < _BOUNDS_TTL_SECONDS:
            return cached[1]

    value: date | None = None
    try:
        from backend.core.datasource import get_datasource

        result = get_datasource().run_select(
            f"SELECT MAX([{column}]) AS max_date FROM {settings.DB_TABLE}"
        )
        if result.row_count:
            raw = result.dataframe.iloc[0, 0]
            if raw is not None and not _is_nat(raw):
                value = _to_date(raw)
    except Exception as exc:  # noqa: BLE001 - a missing window must not fail the request
        logger.warning("Could not read MAX([%s]): %s", column, exc)

    with _bounds_lock:
        _bounds_cache[column] = (now, value)
    return value


def _is_nat(value) -> bool:
    return value != value  # NaT/NaN are the only values not equal to themselves


def _to_date(value) -> date | None:
    if isinstance(value, date):
        return value if not hasattr(value, "date") else value.date()
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime().date()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def reset_date_bounds() -> None:
    """Drop the cache. Used by tests and after a data refresh."""
    with _bounds_lock:
        _bounds_cache.clear()


# ======================================================================================
# Resolution
# ======================================================================================

def _resolve_explicit_column(question: str) -> str | None:
    """A date column the user named themselves. Verified against the live schema."""
    try:
        from backend.core.column_registry import get_registry

        registry = get_registry()
    except Exception:  # noqa: BLE001
        return None
    if not registry.columns:
        return None

    for match in _EXPLICIT_COLUMN_RE.finditer(question):
        phrase = match.group("column").strip(" ,.")
        words = phrase.split()
        # Longest phrase first: "the basis of CLAIM DATE" should find "CLAIM DATE".
        for size in range(len(words), 0, -1):
            candidate = " ".join(words[:size])
            resolved = registry.resolve(candidate)
            info = registry.get(resolved) if resolved else None
            # Only a real DATE column counts - "based on GROSS_PREMIUM" is not a date rule.
            if info and info.category == "date":
                return info.name
    return None


def _default_column() -> str | None:
    try:
        from backend.core.column_registry import get_registry

        registry = get_registry()
        resolved = registry.resolve(DEFAULT_DATE_COLUMN, allow_fuzzy=False)
        if resolved:
            return resolved
        dates = registry.dates()
        return dates[0] if dates else None
    except Exception:  # noqa: BLE001
        return None


def week_span(anchor: date, week: int) -> tuple[date, date]:
    """(inclusive start, exclusive end) of `week` within `anchor`'s month."""
    first_day, next_first = _WEEK_SPANS[week]
    start = anchor.replace(day=first_day)
    if next_first is None:
        # Week 4 runs to the end of the month, so the exclusive end is the 1st of next.
        if anchor.month == 12:
            end = date(anchor.year + 1, 1, 1)
        else:
            end = date(anchor.year, anchor.month + 1, 1)
    else:
        end = anchor.replace(day=next_first)
    return start, end


def resolve_date_window(question: str, today: date | None = None) -> DateWindow | None:
    """The date instruction for this question, or None if it needs no date rule."""
    week_match = _WEEK_RE.search(question)
    is_yearly = bool(_YEARLY_TREND_RE.search(question))
    if not week_match and not is_yearly:
        return None

    column = _resolve_explicit_column(question) or _default_column()
    if not column:
        logger.warning("A date rule was requested but no date column could be resolved.")
        return None

    if is_yearly:
        # The CURRENT calendar month, compared across whatever years the data holds.
        month = (today or date.today()).month
        return DateWindow(
            column=column,
            mode="yearly_trend",
            month=month,
            label=(
                f"{calendar.month_name[month]} compared across available years, "
                f"on {column}"
            ),
        )

    week = _WEEK_ORDINALS[week_match.group(1).lower()]
    anchor = latest_date(column)
    if anchor is None:
        logger.warning(
            "Week %s requested but MAX([%s]) is unavailable - no date rule applied.",
            week, column,
        )
        return None

    start, end = week_span(anchor, week)
    return DateWindow(
        column=column,
        mode="week",
        start=start,
        end=end,
        label=(
            f"week {week} of {calendar.month_name[anchor.month]} {anchor.year} "
            f"({start:%Y-%m-%d} to {end:%Y-%m-%d}, exclusive) on {column} - the latest "
            f"month present in the data"
        ),
    )
