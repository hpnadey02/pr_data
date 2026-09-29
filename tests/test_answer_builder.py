"""Unit tests for deterministic answers and hallucinated-number rejection.

The headline case is the real defect: a query returning 660000 was narrated as
"$1,200,000". Both halves of the fix are covered - the direct answer path that removes the
model from the loop, and the verifier that rejects an unsupported figure if a narrative is
generated.
"""
import pandas as pd
import pytest

from backend.agents.answer_builder import (
    build_direct_answer,
    collect_allowed_numbers,
    format_value,
    has_invented_currency,
    is_direct_answer,
    strip_currency,
    unsupported_numbers,
    verify_narrative,
)


# --------------------------------------------------------------------------------------
# Direct answers - no model involved
# --------------------------------------------------------------------------------------

def test_single_scalar_result_is_answered_verbatim():
    frame = pd.DataFrame({"USGI_SUM_INSURED": [660000.0]})
    assert is_direct_answer(frame)
    answer = build_direct_answer(frame)
    assert "660,000" in answer
    assert "USGI sum insured" in answer
    assert "$" not in answer


def test_direct_answer_of_several_columns():
    frame = pd.DataFrame({"BRANCH_NAME": ["MUMBAI"], "GROSS_PREMIUM": [10233.0]})
    answer = build_direct_answer(frame)
    assert "MUMBAI" in answer
    assert "10,233" in answer


def test_many_rows_is_not_a_direct_answer():
    frame = pd.DataFrame({"A": [1, 2, 3]})
    assert not is_direct_answer(frame)


def test_empty_frame_is_not_a_direct_answer():
    assert not is_direct_answer(pd.DataFrame())
    assert not is_direct_answer(None)


def test_null_value_is_reported_as_missing():
    frame = pd.DataFrame({"USGI_SUM_INSURED": [None]})
    assert "not available" in build_direct_answer(frame)


# --------------------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------------------

def test_format_value_never_adds_currency():
    assert format_value(660000.0) == "660,000"
    assert format_value(1234.5) == "1,234.50"
    assert format_value(0) == "0"
    assert format_value(True) == "Yes"
    assert format_value(None) == "not available"
    assert format_value("MUMBAI") == "MUMBAI"


def test_float_that_is_whole_prints_without_decimals():
    assert format_value(660000.00) == "660,000"


# --------------------------------------------------------------------------------------
# Narrative verification
# --------------------------------------------------------------------------------------

def test_the_reported_hallucination_is_rejected():
    """660000 narrated as $1,200,000: the FIGURE is wrong, so the narrative is discarded."""
    stats = {"measure_column": "USGI_SUM_INSURED", "total": 660000.0, "row_count": 1}
    allowed = collect_allowed_numbers(stats)
    narrative = "- The policy has a USGI sum insured of $1,200,000."
    ok, reason, _ = verify_narrative(narrative, allowed)
    assert not ok
    assert "not present" in reason


def test_correct_narrative_is_accepted():
    stats = {"measure_column": "GROSS_PREMIUM", "total": 660000.0, "row_count": 12}
    allowed = collect_allowed_numbers(stats)
    ok, reason, cleaned = verify_narrative(
        "- Total gross premium is 660,000 across 12 records.", allowed
    )
    assert ok, reason
    assert reason == "ok"
    assert "660,000" in cleaned


def test_right_figure_with_invented_currency_is_kept_but_cleaned():
    """Discarding correct prose over a stray '$' loses good output; strip the unit instead."""
    allowed = collect_allowed_numbers({"total": 69205972.68})
    ok, reason, cleaned = verify_narrative(
        "- MAHARASHTRA leads with $69,205,972.68.", allowed
    )
    assert ok
    assert reason == "currency-stripped"
    assert "$" not in cleaned
    assert "69,205,972.68" in cleaned


@pytest.mark.parametrize(
    "text", ["$1,000,000", "1,000,000 INR", "Rs. 1,000,000", "₹1,000,000", "1,000,000 rupees"]
)
def test_currency_forms_are_stripped(text):
    cleaned = strip_currency(text)
    assert "1,000,000" in cleaned
    for token in ("$", "INR", "Rs", "₹", "rupees"):
        assert token not in cleaned


def test_invented_figure_is_caught():
    allowed = collect_allowed_numbers({"total": 660000.0})
    assert unsupported_numbers("Total was 1,200,000.", allowed) == ["1,200,000"]


def test_small_integers_are_treated_as_prose():
    allowed = collect_allowed_numbers({"total": 660000.0})
    assert unsupported_numbers("The top 5 branches across 3 zones.", allowed) == []


def test_rescaled_figures_are_permitted():
    """1234567 legitimately restated as '1.23 million' must not be rejected."""
    allowed = collect_allowed_numbers({"total": 1234567.0})
    assert unsupported_numbers("Total is 1.23 million.", allowed) == []


def test_numbers_from_a_dataframe_count_as_allowed():
    frame = pd.DataFrame({"GROSS_PREMIUM": [10233.0, 9785.0]})
    allowed = collect_allowed_numbers(frame)
    assert unsupported_numbers("Top value 10,233 and 9,785.", allowed) == []


def test_currency_detection():
    assert has_invented_currency("$1,200,000")
    assert has_invented_currency("1200000 USD")
    assert has_invented_currency("1.2 million dollars")
    assert not has_invented_currency("1,200,000")


def test_empty_narrative_is_rejected():
    ok, reason, _ = verify_narrative("   ", set())
    assert not ok
    assert "empty" in reason
