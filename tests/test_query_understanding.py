"""Tests for the deterministic parts of query understanding.

Nothing here calls an LLM: filter binding, routing and decomposition are all rule-based so
they behave identically whether or not Ollama is reachable.
"""
import pytest

from backend.agents import query_understanding as qu
from backend.core.column_registry import ColumnInfo, ColumnRegistry, infer_category

SCHEMA = [
    ("REFERENCE_NUMBER", "int"),
    ("POLICY_NO", "int"),
    ("POLICY_NO_CHAR", "varchar"),
    ("USGIpos_Policy_Number", "varchar"),
    ("USGI_SUM_INSURED", "float"),
    ("GROSS_PREMIUM", "float"),
    ("BRANCH_NAME", "varchar"),
    ("INSURED_NAME", "varchar"),
    ("INSURED_ID", "int"),
    ("BRANCH_OFFICE_CODE", "int"),
    ("Sub_Inward_Number", "varchar"),
]


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    infos = [
        ColumnInfo(name=name, data_type=dtype, category=infer_category(name, dtype))
        for name, dtype in SCHEMA
    ]
    by_name = {i.name: i for i in infos}
    by_name["POLICY_NO"].aliases = ["policy no", "policy number"]
    by_name["BRANCH_NAME"].aliases = ["branch", "branch office"]
    by_name["GROSS_PREMIUM"].aliases = ["gross premium", "premium", "business"]
    by_name["Sub_Inward_Number"].aliases = ["sub inward no", "sub inward number"]
    reg = ColumnRegistry(infos)
    monkeypatch.setattr(qu, "get_registry", lambda: reg)
    return reg


# --------------------------------------------------------------------------------------
# Identifier binding - the reported "wrong column" defect
# --------------------------------------------------------------------------------------

def test_binds_each_identifier_to_its_own_column():
    question = (
        "REFERENCE_NUMBER-2316239805635, POLICY_NO-1029156133, give USGI_SUM_INSURED"
    )
    filters, unresolved = qu.extract_equality_filters(question)
    assert filters == {
        "REFERENCE_NUMBER": "2316239805635",
        "POLICY_NO": "1029156133",
    }
    assert unresolved == []


def test_policy_no_is_not_bound_to_the_pos_policy_column():
    """The logged failure: POLICY_NO's value was filtered on USGIpos_Policy_Number."""
    filters, _ = qu.extract_equality_filters("POLICY_NO-1029156133")
    assert "POLICY_NO" in filters
    assert "USGIpos_Policy_Number" not in filters


def test_slash_bearing_value_is_kept_whole():
    question = "REFERENCE_NUMBER-2316239805635, USGIpos_Policy_Number-AVO/2316/20138002"
    filters, _ = qu.extract_equality_filters(question)
    assert filters["USGIpos_Policy_Number"] == "AVO/2316/20138002"


def test_multiple_identifiers_all_captured():
    question = (
        "REFERENCE_NUMBER-2316239805635, POLICY_NO_CHAR-2316/84507832/00/000, "
        "BRANCH_OFFICE_CODE-10013, INSURED_ID-100461876655, give me INSURED_NAME"
    )
    filters, _ = qu.extract_equality_filters(question)
    assert filters["POLICY_NO_CHAR"] == "2316/84507832/00/000"
    assert filters["BRANCH_OFFICE_CODE"] == "10013"
    assert filters["INSURED_ID"] == "100461876655"


def test_equals_and_colon_separators():
    assert qu.extract_equality_filters("policy no = 1029156133")[0] == {"POLICY_NO": "1029156133"}
    assert qu.extract_equality_filters("policy no: 1029156133")[0] == {"POLICY_NO": "1029156133"}


def test_shortcut_spelling_is_accepted():
    filters, _ = qu.extract_equality_filters("sub inward no - 12345")
    assert filters == {"Sub_Inward_Number": "12345"}


# --------------------------------------------------------------------------------------
# False positives - hyphenated English must not be read as a filter
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "question",
    [
        "High-performing branch region-wise.",
        "Vertical-wise weekly business trend.",
        "Product-wise and branch-wise trend.",
        "Show the top five branches and their weekly trend.",
    ],
)
def test_ordinary_questions_produce_no_filters_and_no_warnings(question):
    filters, unresolved = qu.extract_equality_filters(question)
    assert filters == {}
    assert unresolved == []


def test_uppercase_value_is_accepted_as_a_filter():
    filters, _ = qu.extract_equality_filters("branch = MUMBAI")
    assert filters == {"BRANCH_NAME": "MUMBAI"}


# --------------------------------------------------------------------------------------
# Requested output columns
# --------------------------------------------------------------------------------------

def test_requested_column_is_extracted():
    question = "REFERENCE_NUMBER-2316239805635, give USGI_SUM_INSURED"
    filters, _ = qu.extract_equality_filters(question)
    assert qu.extract_requested_columns(question, exclude=set(filters)) == ["USGI_SUM_INSURED"]


def test_requested_column_with_give_me():
    question = "INSURED_ID-100461876655, give me INSURED_NAME"
    filters, _ = qu.extract_equality_filters(question)
    assert qu.extract_requested_columns(question, exclude=set(filters)) == ["INSURED_NAME"]


# --------------------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------------------

def test_identifier_filters_route_to_lookup():
    """Routing this as 'aggregation' produced a WHERE-less SELECT over the whole table."""
    assert qu.classify_route("give USGI_SUM_INSURED", {}, {"POLICY_NO": "1"}) == "lookup"


def test_ranking_and_trend_routes():
    assert qu.classify_route("High-performing branch region-wise.", {}, {}) == "ranking"
    assert qu.classify_route("Vertical-wise weekly business trend.", {"date_grain": "week"}, {}) in (
        "trend", "decomposition",
    )
    assert qu.classify_route("Show actual versus target.", {}, {}) == "actual_vs_target"


# --------------------------------------------------------------------------------------
# Conversational rewriting must not hijack a self-contained question
# --------------------------------------------------------------------------------------

HISTORY = [{"question": "give USGI_SUM_INSURED", "rewritten": "give USGI_SUM_INSURED"}]


def test_question_with_an_identifier_is_never_rewritten():
    """The observed bug: this 7-word question was rewritten into the PREVIOUS question,
    so the user got the previous answer."""
    assert not qu._looks_like_followup("sub inward no for policy no 1029156133", HISTORY)


def test_question_naming_a_column_is_not_a_followup():
    assert not qu._looks_like_followup("gross premium", HISTORY)


def test_anaphoric_question_is_a_followup():
    assert qu._looks_like_followup("now show it by product", HISTORY)
    assert qu._looks_like_followup("what about branch?", HISTORY)


def test_no_history_means_no_rewrite():
    assert not qu._looks_like_followup("now show it by product", [])


def test_rewrite_echoing_the_prompt_is_discarded():
    """The model returned prompt scaffolding containing 'interpreted as'."""
    assert not qu._rewrite_is_usable(
        "Q1: give USGI_SUM_INSURED (interpreted as: give USGI_SUM_INSURED)", "sub inward no"
    )
    assert not qu._rewrite_is_usable("", "sub inward no")
    assert not qu._rewrite_is_usable("Standalone question: something", "x")


def test_reasonable_rewrite_is_kept():
    assert qu._rewrite_is_usable(
        "Show gross premium by product for the top branch", "now show it by product"
    )


def test_normalization_expands_abbreviations():
    assert "year over year" in qu.normalize_text("show yoy growth")
    assert qu.normalize_text("  a   b  ") == "a b"


def test_filters_extract_top_n_and_grain():
    filters = qu.extract_filters("Show the top five branches weekly")
    assert filters["top_n"] == 5
    assert filters["date_grain"] == "week"


# --------------------------------------------------------------------------------------
# Default top-N. "Top performing branches" with no number must still mean something.
# --------------------------------------------------------------------------------------

def _node(question: str) -> dict:
    return qu.query_understanding_node(
        {"request_id": "t", "raw_question": question, "chat_history": [],
         "warnings": [], "timings_ms": {}}
    )


@pytest.mark.parametrize(
    "question",
    [
        "High-performing intermediaries.",
        "High-performing branch region-wise.",
        "Show top branches by business",
    ],
)
def test_ranking_without_a_number_defaults_to_five(question):
    result = _node(question)
    assert result["route"] in ("ranking", "ranking_then_trend", "decomposition")
    if result["route"] in ("ranking", "ranking_then_trend"):
        assert result["filters"]["top_n"] == qu.DEFAULT_TOP_N


def test_an_explicit_number_is_never_overridden():
    assert _node("Show the top 3 branches")["filters"]["top_n"] == 3
    assert _node("Show the top ten branches")["filters"]["top_n"] == 10


def test_single_highest_is_not_turned_into_a_shortlist():
    """'Which branch has the highest business, and how did it trend weekly' means ONE."""
    result = _node("Which branch has the highest business, and how did it trend weekly?")
    assert result["route"] == "top1_then_trend"
    assert not result["filters"].get("top_n")


def test_a_non_ranking_question_gets_no_default():
    result = _node("Zone-wise business contribution.")
    assert not result["filters"].get("top_n")
