"""Date-window resolution rules.

Dates are resolved to concrete values in Python before the SQL model sees the question, so
all of this is testable without a database or an LLM. The two rules that are easiest to
get wrong, and that these lock down:

  * "1st/2nd/3rd/4th week" = that week of the LATEST MONTH IN THE DATA, never the current
    calendar month. If the data ends in August, "the 1st week" means August 1-7.
  * "Yearly trend" = the CURRENT CALENDAR MONTH compared across the available years, not
    the whole dataset grouped by year. These two are different rules.
"""
from datetime import date

import pytest

from backend.core import date_windows as dw
from backend.core.column_registry import ColumnInfo, ColumnRegistry

SCHEMA = [
    ColumnInfo(name="POLICY_ISSUE_DATE", data_type="date", category="date"),
    ColumnInfo(name="CLAIM_DATE", data_type="date", category="date"),
    ColumnInfo(name="VOUCHER_DATE", data_type="datetime", category="date"),
    ColumnInfo(name="GROSS_PREMIUM", data_type="decimal", category="measure"),
    ColumnInfo(name="BRANCH_NAME", data_type="varchar", category="dimension"),
]


@pytest.fixture(autouse=True)
def _registry(monkeypatch):
    registry = ColumnRegistry(SCHEMA)
    monkeypatch.setattr(
        "backend.core.column_registry.get_registry", lambda refresh=False: registry
    )
    dw.reset_date_bounds()
    return registry


@pytest.fixture
def data_ends(monkeypatch):
    """Pin MAX(date_column) so 'latest month in the data' is deterministic."""
    def _set(when: date):
        monkeypatch.setattr(dw, "latest_date", lambda column: when)
    return _set


# --------------------------------------------------------------------------------------
# Week spans
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "week, start_day, end_day",
    [(1, 1, 8), (2, 8, 15), (3, 15, 22)],
)
def test_fixed_week_spans(week, start_day, end_day):
    start, end = dw.week_span(date(2026, 9, 20), week)
    assert (start.day, end.day) == (start_day, end_day)
    assert start.month == end.month == 9


def test_fourth_week_runs_to_the_end_of_the_month():
    """September has 30 days, so the exclusive end is 1 October."""
    start, end = dw.week_span(date(2026, 9, 20), 4)
    assert start == date(2026, 9, 22)
    assert end == date(2026, 10, 1)


def test_fourth_week_of_a_31_day_month():
    start, end = dw.week_span(date(2026, 8, 5), 4)
    assert (start, end) == (date(2026, 8, 22), date(2026, 9, 1))


def test_fourth_week_of_december_rolls_the_year():
    _, end = dw.week_span(date(2026, 12, 3), 4)
    assert end == date(2027, 1, 1)


def test_fourth_week_of_february_in_a_leap_year():
    start, end = dw.week_span(date(2028, 2, 10), 4)
    assert (start, end) == (date(2028, 2, 22), date(2028, 3, 1))


# --------------------------------------------------------------------------------------
# Week requests use the latest month IN THE DATA, not the calendar
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "phrase, week", [("1st week", 1), ("2nd week", 2), ("3rd week", 3), ("4th week", 4)]
)
def test_week_request_uses_the_latest_month_in_the_data(data_ends, phrase, week):
    data_ends(date(2026, 9, 18))
    window = dw.resolve_date_window(f"Show me the {phrase}.")
    assert window.mode == "week"
    assert window.column == "POLICY_ISSUE_DATE"
    assert window.start.month == 9 and window.start.year == 2026
    assert window.start == dw.week_span(date(2026, 9, 18), week)[0]


def test_week_request_ignores_the_current_calendar_month(data_ends):
    """Data ends in August; 'the 1st week' must mean August, not today's month."""
    data_ends(date(2026, 8, 14))
    window = dw.resolve_date_window("Give me the 1st week.")
    assert (window.start, window.end) == (date(2026, 8, 1), date(2026, 8, 8))


def test_word_ordinals_are_understood(data_ends):
    data_ends(date(2026, 9, 18))
    window = dw.resolve_date_window("Show the second week of business")
    assert window.start == date(2026, 9, 8)


def test_no_data_means_no_date_rule(monkeypatch):
    """Better to apply no filter than to invent a range over an empty column."""
    monkeypatch.setattr(dw, "latest_date", lambda column: None)
    assert dw.resolve_date_window("Show me the 1st week.") is None


# --------------------------------------------------------------------------------------
# Yearly trend = current month across years
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "question",
    ["Give me the yearly trend.", "Show yearly analysis", "annual trend please",
     "year-wise trend of business"],
)
def test_yearly_trend_is_recognised(question):
    window = dw.resolve_date_window(question, today=date(2026, 9, 16))
    assert window is not None and window.mode == "yearly_trend"


def test_yearly_trend_uses_the_current_calendar_month():
    window = dw.resolve_date_window("Give me the yearly trend.", today=date(2026, 9, 16))
    assert window.month == 9
    assert window.start is None and window.end is None


def test_yearly_trend_sql_groups_by_year_not_the_whole_dataset():
    window = dw.resolve_date_window("yearly trend", today=date(2026, 3, 2))
    instruction = window.as_sql_instruction()
    assert "MONTH([POLICY_ISSUE_DATE]) = 3" in instruction   # the literal month, resolved
    assert "GROUP BY YEAR([POLICY_ISSUE_DATE])" in instruction
    assert "NOT group the whole dataset by year" in instruction
    assert "NOT call GETDATE()" in instruction               # the model is told not to


def test_a_week_request_and_a_yearly_trend_are_not_confused(data_ends):
    data_ends(date(2026, 9, 18))
    assert dw.resolve_date_window("Give me the 2nd week.").mode == "week"
    assert dw.resolve_date_window("Give me the yearly trend.").mode == "yearly_trend"


# --------------------------------------------------------------------------------------
# Date column priority
# --------------------------------------------------------------------------------------

def test_an_explicitly_named_date_column_wins(data_ends):
    data_ends(date(2026, 9, 18))
    window = dw.resolve_date_window("Give me the 4th week based on CLAIM_DATE.")
    assert window.column == "CLAIM_DATE"


def test_explicit_column_also_applies_to_a_yearly_trend():
    window = dw.resolve_date_window(
        "Show yearly trend based on CLAIM_DATE.", today=date(2026, 9, 16)
    )
    assert window.column == "CLAIM_DATE"
    assert window.mode == "yearly_trend"


def test_the_default_column_is_used_when_none_is_named(data_ends):
    data_ends(date(2026, 9, 18))
    assert dw.resolve_date_window("Show the 3rd week.").column == "POLICY_ISSUE_DATE"


def test_a_non_date_column_never_becomes_the_date_rule(data_ends):
    """'based on GROSS_PREMIUM' is not a date instruction."""
    data_ends(date(2026, 9, 18))
    window = dw.resolve_date_window("Show the 1st week based on GROSS_PREMIUM.")
    assert window.column == "POLICY_ISSUE_DATE"


# --------------------------------------------------------------------------------------
# Questions that need no date rule
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "question",
    [
        "High-performing branch region-wise.",
        "Zone-wise business contribution.",
        "Vertical-wise weekly business trend.",   # a grain, not a specific week
        "Show the top five branches",
    ],
)
def test_questions_without_a_date_rule_get_none(question):
    assert dw.resolve_date_window(question) is None


# --------------------------------------------------------------------------------------
# The instruction handed to the model
# --------------------------------------------------------------------------------------

def test_week_instruction_is_a_half_open_literal_range(data_ends):
    """Half-open, so a column carrying a time component cannot drop the last day."""
    data_ends(date(2026, 9, 18))
    instruction = dw.resolve_date_window("2nd week").as_sql_instruction()
    assert ">= '2026-09-08'" in instruction
    assert "< '2026-09-15'" in instruction
    assert "BETWEEN" not in instruction
    # The dates are already resolved, so the model is told explicitly not to derive them.
    assert "do NOT call GETDATE(), MAX()" in instruction


def test_the_label_states_which_month_was_used(data_ends):
    data_ends(date(2026, 8, 14))
    assert "August 2026" in dw.resolve_date_window("1st week").label
