"""Regression tests for the SQL Server result shape.

THE BUG THESE LOCK DOWN
-----------------------
With DATA_SOURCE=local the pipeline produced an insight and a chart. With
DATA_SOURCE=sqlserver the same question fetched the same rows but produced NO chart and
an insight with no real figures in it.

Cause: pyodbc returns `decimal.Decimal` for SQL Server DECIMAL/NUMERIC/MONEY columns and
`datetime.date` for date columns. pandas cannot map those to a numpy dtype, so the column
lands as OBJECT. Every downstream decision keys off dtype:

    numeric_columns()  -> []        (Decimal column is not is_numeric_dtype)
    pick_measure()     -> None
    chart_agent        -> no chart
    compute_stats()    -> {"row_count": N} only, so the model had nothing to summarise

DuckDB hands back real numpy dtypes, which is why local mode looked healthy.

These tests build frames the way pyodbc would and assert the whole downstream chain
behaves identically to the local path. No SQL Server, no Ollama and no ChromaDB are
needed, so they can be run on any machine.
"""
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from backend.agents.answer_builder import collect_allowed_numbers, unsupported_numbers
from backend.agents.chart_agent import chart_agent_node
from backend.agents.df_utils import (
    date_like_columns,
    numeric_columns,
    pick_measure,
)
from backend.agents.insight_agent import compute_stats
from backend.core.datasource import normalize_result_frame, repair_column_names


@pytest.fixture(autouse=True)
def _no_live_registry(monkeypatch):
    """Keep these hermetic: never let pick_measure reach for a live schema."""
    monkeypatch.setattr(
        "backend.agents.df_utils._registry_category", lambda name: None
    )


# The same three rows, as each backend would deliver them.
_ROWS = [
    ("MUMBAI", "69205972.68", "2026-05-01"),
    ("DELHI", "41003311.10", "2026-05-08"),
    ("PUNE", "18770145.05", "2026-05-15"),
]


def _sqlserver_frame(measure_name: str = "total_gross_premium") -> pd.DataFrame:
    """What pyodbc actually hands back: Decimal money and datetime.date objects."""
    rows = [
        (branch, Decimal(amount), date.fromisoformat(day))
        for branch, amount, day in _ROWS
    ]
    frame = pd.DataFrame.from_records(
        rows, columns=["BRANCH_NAME", measure_name, "POLICY_ISSUE_DATE"]
    )
    return normalize_result_frame(frame)


def _local_frame(measure_name: str = "total_gross_premium") -> pd.DataFrame:
    """What DuckDB hands back: proper numpy dtypes already."""
    frame = pd.DataFrame(
        {
            "BRANCH_NAME": [r[0] for r in _ROWS],
            measure_name: [float(r[1]) for r in _ROWS],
            "POLICY_ISSUE_DATE": pd.to_datetime([r[2] for r in _ROWS]),
        }
    )
    return normalize_result_frame(frame)


def _state(frame: pd.DataFrame, question: str = "Branch wise business", route: str = "ranking"):
    return {
        "request_id": "test",
        "dataframe": frame,
        "rewritten_question": question,
        "route": route,
        "filters": {},
        "sub_questions": [question],
        "retrieved_columns": [],
        "warnings": [],
        "timings_ms": {},
    }


# --------------------------------------------------------------------------------------
# Normalisation itself
# --------------------------------------------------------------------------------------

def test_decimal_money_becomes_a_real_numeric_column():
    """The single defect that switched charting off on SQL Server."""
    raw = pd.DataFrame.from_records(
        [("MUMBAI", Decimal("69205972.68"))], columns=["BRANCH_NAME", "total"]
    )
    assert raw["total"].dtype == object          # what pyodbc leaves behind
    assert numeric_columns(raw) == []            # ...and why nothing worked

    fixed = normalize_result_frame(raw)
    assert pd.api.types.is_numeric_dtype(fixed["total"])
    assert numeric_columns(fixed) == ["total"]
    assert fixed["total"].iloc[0] == pytest.approx(69205972.68)


def test_date_objects_become_a_real_datetime_column():
    raw = pd.DataFrame.from_records(
        [(date(2026, 5, 1),), (date(2026, 5, 8),)], columns=["POLICY_ISSUE_DATE"]
    )
    assert raw["POLICY_ISSUE_DATE"].dtype == object

    fixed = normalize_result_frame(raw)
    assert pd.api.types.is_datetime64_any_dtype(fixed["POLICY_ISSUE_DATE"])
    assert date_like_columns(fixed) == ["POLICY_ISSUE_DATE"]


def test_unaliased_aggregate_gets_a_usable_column_name():
    """SQL Server names `SELECT SUM(x)` with no alias '' - Plotly cannot chart that."""
    assert repair_column_names(["BRANCH_NAME", ""]) == ["BRANCH_NAME", "column_2"]


def test_duplicate_column_names_are_made_unique():
    assert repair_column_names(["total", "total", "total"]) == ["total", "total_1", "total_2"]


def test_column_names_are_trimmed():
    assert repair_column_names(["  BRANCH_NAME  "]) == ["BRANCH_NAME"]


@pytest.mark.parametrize(
    "column", ["POLICY_NO", "POLICY_NO_CHAR", "REFERENCE_NUMBER", "YEAR_OF_MANUFACTURING"]
)
def test_identifier_text_is_never_coerced_to_numbers(column):
    """A digits-only identifier must stay a string, not become a float.

    POLICY_NO holds 1029156133 - parseable, but a reference, not a quantity.
    """
    raw = pd.DataFrame({column: ["1029156133", "1029156134"]})
    fixed = normalize_result_frame(raw)
    assert not pd.api.types.is_numeric_dtype(fixed[column])
    assert fixed[column].iloc[0] == "1029156133"


# --------------------------------------------------------------------------------------
# The Excel side of the same problem
#
# In may_2.xlsx all 47 measure columns are stored as TEXT even though SQL Server declares
# them decimal(38,2). Read with pandas they arrive as strings, which hides the measure
# exactly as the Decimal objects did on SQL Server.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "column",
    [
        "GROSS_PREMIUM", "TOTAL_SUM_INSURED", "NET_PREMIUM", "COMMISSION_AMOUNT",
        "Sgst_Net_Amount", "Total_Gst", "Balance_Amount", "Gvw", "PML",
        "SHARE_PERCENTAGE", "Num_Cess_Percentage", "COMMISSION_PER",
    ],
)
def test_measure_stored_as_text_becomes_numeric(column):
    frame = normalize_result_frame(pd.DataFrame({column: ["69205972.68", "41003311.10"]}))
    assert pd.api.types.is_numeric_dtype(frame[column])
    assert frame[column].iloc[0] == pytest.approx(69205972.68)


def test_thousands_separators_are_handled():
    frame = normalize_result_frame(pd.DataFrame({"TOTAL_SUM_INSURED": ["1,250,000.00"]}))
    assert frame["TOTAL_SUM_INSURED"].iloc[0] == pytest.approx(1250000.0)


def test_a_measure_column_with_any_non_number_is_left_alone():
    """One 'N/A' and the whole column stays text - never guess past a bad value."""
    frame = normalize_result_frame(
        pd.DataFrame({"GROSS_PREMIUM": ["100.5", "N/A", "200.0"]})
    )
    assert not pd.api.types.is_numeric_dtype(frame["GROSS_PREMIUM"])


def test_text_measures_produce_a_chart_and_stats():
    """The Excel path must reach the same place as the SQL Server path."""
    frame = normalize_result_frame(
        pd.DataFrame(
            {
                "BRANCH_NAME": ["MUMBAI", "DELHI", "PUNE"],
                "GROSS_PREMIUM": ["69205972.68", "41003311.10", "18770145.05"],
            }
        )
    )
    assert pick_measure(frame, numeric_columns(frame)) == "GROSS_PREMIUM"
    assert compute_stats(frame, "q")["total"] == pytest.approx(128979428.83)
    assert chart_agent_node(_state(frame))["charts"]


def test_all_null_column_survives_normalisation():
    raw = pd.DataFrame({"TARGET_PREMIUM": [None, None]})
    assert len(normalize_result_frame(raw)) == 2


def test_empty_result_keeps_its_columns():
    empty = normalize_result_frame(pd.DataFrame(columns=["BRANCH_NAME", "total"]))
    assert list(empty.columns) == ["BRANCH_NAME", "total"]


# --------------------------------------------------------------------------------------
# The chart that used not to appear
# --------------------------------------------------------------------------------------

def test_sqlserver_result_produces_a_chart():
    result = chart_agent_node(_state(_sqlserver_frame()))
    assert result["charts"], "SQL Server data must produce a chart, not a table-only fallback"
    assert result["chart_type"] != "table"
    assert result["charts"][0]["figure_json"]


def test_chart_output_matches_the_local_backend():
    """Parity is the real contract: same query, same chart, whichever backend answered."""
    sqlserver = chart_agent_node(_state(_sqlserver_frame()))
    local = chart_agent_node(_state(_local_frame()))
    assert sqlserver["chart_type"] == local["chart_type"]
    assert len(sqlserver["charts"]) == len(local["charts"])


def test_unaliased_aggregate_still_charts():
    frame = _sqlserver_frame(measure_name="")
    assert "column_2" in frame.columns
    assert chart_agent_node(_state(frame))["charts"]


# --------------------------------------------------------------------------------------
# The insight that used to be wrong
# --------------------------------------------------------------------------------------

def test_stats_are_computed_from_sqlserver_data():
    stats = compute_stats(_sqlserver_frame(), "Branch wise business")
    assert stats["measure_column"] == "total_gross_premium"
    assert stats["total"] == pytest.approx(128979428.83, rel=1e-9)
    assert stats["dimension"] == "BRANCH_NAME"
    assert stats["top_entities"][0]["name"] == "MUMBAI"
    assert stats["top_entities"][0]["value"] == pytest.approx(69205972.68)


def test_stats_match_the_local_backend():
    assert compute_stats(_sqlserver_frame(), "q") == compute_stats(_local_frame(), "q")


def test_real_figures_are_accepted_by_the_hallucination_guard():
    """The verifier must recognise the fetched numbers, or every narrative is rejected."""
    frame = _sqlserver_frame()
    allowed = collect_allowed_numbers(compute_stats(frame, "q"), frame)
    assert unsupported_numbers("MUMBAI leads with 69,205,972.68.", allowed) == []


def test_an_invented_figure_is_still_caught():
    frame = _sqlserver_frame()
    allowed = collect_allowed_numbers(compute_stats(frame, "q"), frame)
    assert unsupported_numbers("MUMBAI leads with 1,200,000.", allowed) == ["1,200,000"]


# --------------------------------------------------------------------------------------
# Identifier columns must never be charted as if they were values
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "identifier", ["POLICY_NO", "BRANCH_OFFICE_CODE", "INSURED_ID", "REFERENCE_NUMBER"]
)
def test_numeric_identifier_is_never_chosen_as_the_measure(identifier):
    """SQL Server keeps these as real integers, so a dtype-only check would pick them."""
    frame = pd.DataFrame(
        {"BRANCH_NAME": ["MUMBAI", "DELHI"], identifier: [10013, 10014],
         "total_gross_premium": [69205972.68, 41003311.10]}
    )
    assert pick_measure(frame, numeric_columns(frame)) == "total_gross_premium"


def test_a_result_of_only_identifiers_has_no_measure():
    frame = pd.DataFrame({"POLICY_NO": [1029156133, 1029156134]})
    assert pick_measure(frame, numeric_columns(frame)) is None


def test_absolute_measure_wins_over_a_percentage_share():
    frame = pd.DataFrame(
        {"STATE": ["MH", "DL"], "pct_share": [53.6, 31.8],
         "total_gross_premium": [69205972.68, 41003311.10]}
    )
    assert pick_measure(frame, numeric_columns(frame)) == "total_gross_premium"


def test_a_percentage_is_used_when_it_is_the_only_number():
    frame = pd.DataFrame({"STATE": ["MH", "DL"], "pct_share": [53.6, 31.8]})
    assert pick_measure(frame, numeric_columns(frame)) == "pct_share"


def test_boolean_column_is_not_a_measure():
    frame = pd.DataFrame({"STATE": ["MH", "DL"], "is_renewal": [True, False]})
    assert numeric_columns(frame) == []


def test_measure_named_like_a_date_is_not_used_as_the_time_axis():
    """`premium_this_month` is a value, not an axis."""
    frame = pd.DataFrame({"STATE": ["MH", "DL"], "premium_this_month": [10.0, 20.0]})
    assert date_like_columns(frame) == []




