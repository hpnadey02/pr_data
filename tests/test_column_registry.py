"""Unit tests for column resolution and SQL identifier repair.

Built on a fixed miniature schema so the tests do not need a live data source.
"""
import pytest

from backend.agents.column_guard import ColumnValidationError, validate_and_repair
from backend.core.column_registry import ColumnInfo, ColumnRegistry, build_registry, infer_category

SCHEMA = [
    {"column_name": "REFERENCE_NUMBER", "data_type": "int"},
    {"column_name": "POLICY_NO", "data_type": "int"},
    {"column_name": "POLICY_NO_CHAR", "data_type": "varchar"},
    {"column_name": "USGIpos_Policy_Number", "data_type": "varchar"},
    {"column_name": "USGI_SUM_INSURED", "data_type": "float"},
    {"column_name": "TOTAL_SUM_INSURED", "data_type": "float"},
    {"column_name": "GROSS_PREMIUM", "data_type": "float"},
    {"column_name": "BRANCH_NAME", "data_type": "varchar"},
    {"column_name": "Sub_Inward_Number", "data_type": "varchar"},
    {"column_name": "POLICY_ISSUE_DATE", "data_type": "datetime"},
]


@pytest.fixture
def registry() -> ColumnRegistry:
    infos = [
        ColumnInfo(
            name=c["column_name"],
            data_type=c["data_type"],
            category=infer_category(c["column_name"], c["data_type"]),
        )
        for c in SCHEMA
    ]
    # Curated aliases mirroring backend/knowledge/column_aliases.json.
    by_name = {i.name: i for i in infos}
    by_name["GROSS_PREMIUM"].aliases = ["gross premium", "premium", "business"]
    by_name["BRANCH_NAME"].aliases = ["branch", "branch office"]
    by_name["Sub_Inward_Number"].aliases = ["sub inward no", "sub inward number"]
    by_name["POLICY_NO"].aliases = ["policy number", "policy no"]
    # Both sum-insured columns legitimately answer to "sum insured" - that clash is the
    # point of test_ambiguous_shortcut_is_refused_not_guessed below.
    by_name["TOTAL_SUM_INSURED"].aliases = ["sum insured", "total si", "tsi"]
    by_name["USGI_SUM_INSURED"].aliases = ["usgi si", "usgi sum"]
    return ColumnRegistry(infos)


# --------------------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "phrase, expected",
    [
        ("Sub_Inward_Number", "Sub_Inward_Number"),
        ("sub inward number", "Sub_Inward_Number"),
        ("sub inward no", "Sub_Inward_Number"),
        ("SUB-INWARD-NUMBER", "Sub_Inward_Number"),
        ("sb inward no", "Sub_Inward_Number"),          # typo -> fuzzy
        ("gross premium", "GROSS_PREMIUM"),
        ("GROSS PREMIUM", "GROSS_PREMIUM"),             # the exact log failure
        ("grosspremium", "GROSS_PREMIUM"),
        ("branch", "BRANCH_NAME"),
        ("policy no", "POLICY_NO"),
        ("policy number", "POLICY_NO"),
        ("usgi sum insured", "USGI_SUM_INSURED"),
        ("policy issue date", "POLICY_ISSUE_DATE"),
    ],
)
def test_resolves_user_phrasing(registry, phrase, expected):
    assert registry.resolve(phrase) == expected


def test_exact_name_always_wins_over_alias(registry):
    """POLICY_NO_CHAR must resolve to itself, never to POLICY_NO's alias space."""
    assert registry.resolve("POLICY_NO_CHAR") == "POLICY_NO_CHAR"
    assert registry.resolve("policy no char") == "POLICY_NO_CHAR"


def test_distinct_policy_columns_never_collapse(registry):
    """The reported defect: POLICY_NO's value was filtered on USGIpos_Policy_Number."""
    assert registry.resolve("POLICY_NO") == "POLICY_NO"
    assert registry.resolve("USGIpos_Policy_Number") == "USGIpos_Policy_Number"
    assert registry.resolve("POLICY_NO") != registry.resolve("USGIpos_Policy_Number")


def test_ambiguous_shortcut_is_refused_not_guessed(registry):
    """'sum insured' fits both USGI_SUM_INSURED and TOTAL_SUM_INSURED."""
    assert registry.resolve("sum insured") is None
    assert "suminsured" in registry.collisions
    assert set(registry.collisions["suminsured"]) == {"USGI_SUM_INSURED", "TOTAL_SUM_INSURED"}


def test_unknown_phrase_returns_none_with_suggestions(registry):
    assert registry.resolve("total claims paid") is None
    assert registry.suggest("policy nummber")


def test_category_inference():
    assert infer_category("GROSS_PREMIUM", "float") == "measure"
    assert infer_category("POLICY_ISSUE_DATE", "datetime") == "date"
    # A numeric identifier is a dimension, not something to SUM.
    assert infer_category("POLICY_NO", "int") == "dimension"
    assert infer_category("BRANCH_OFFICE_CODE", "int") == "dimension"


def test_build_registry_ignores_columns_not_in_live_schema():
    reg = build_registry([{"column_name": "BRANCH_NAME", "data_type": "varchar"}])
    assert reg.names == ["BRANCH_NAME"]
    assert reg.resolve("GROSS_PREMIUM") is None


# --------------------------------------------------------------------------------------
# SQL identifier repair
# --------------------------------------------------------------------------------------

def test_repairs_the_exact_logged_failure(registry):
    """SUM([GROSS PREMIUM]) killed every query in logs/app.log."""
    sql = (
        "SELECT TOP 1 [BRANCH_NAME], SUM([GROSS PREMIUM]) AS [PERFORMANCE] "
        "FROM dbo.May_2 GROUP BY [BRANCH_NAME] ORDER BY [PERFORMANCE] DESC"
    )
    repaired, repairs = validate_and_repair(sql, registry, "dbo.May_2")
    assert "[GROSS_PREMIUM]" in repaired
    assert "GROSS PREMIUM" not in repaired
    assert repairs == ["[GROSS PREMIUM] -> [GROSS_PREMIUM]"]


def test_query_alias_is_not_treated_as_a_column(registry):
    sql = (
        "SELECT TOP 5 [BRANCH_NAME], SUM([GROSS_PREMIUM]) AS Total_Premium "
        "FROM dbo.May_2 GROUP BY [BRANCH_NAME] ORDER BY Total_Premium DESC"
    )
    repaired, repairs = validate_and_repair(sql, registry, "dbo.May_2")
    assert repairs == []
    assert "Total_Premium" in repaired


def test_unknown_column_raises_with_suggestions(registry):
    sql = "SELECT TOP 1 [TOTAL_CLAIMS_PAID] FROM dbo.May_2"
    with pytest.raises(ColumnValidationError) as info:
        validate_and_repair(sql, registry, "dbo.May_2")
    assert "TOTAL_CLAIMS_PAID" in str(info.value)


def test_string_literals_are_never_repaired(registry):
    sql = (
        "SELECT TOP 1 [USGI_SUM_INSURED] FROM dbo.May_2 "
        "WHERE [USGIpos_Policy_Number] = 'AVO/2316/20138002'"
    )
    repaired, repairs = validate_and_repair(sql, registry, "dbo.May_2")
    assert "'AVO/2316/20138002'" in repaired
    assert repairs == []


def test_table_name_is_not_treated_as_a_column(registry):
    sql = "SELECT TOP 1 [BRANCH_NAME] FROM dbo.May_2"
    repaired, repairs = validate_and_repair(sql, registry, "dbo.May_2")
    assert "dbo.May_2" in repaired
    assert repairs == []


def test_bare_underscore_identifier_is_bracketed(registry):
    sql = "SELECT TOP 1 GROSS_PREMIUM FROM dbo.May_2"
    repaired, _ = validate_and_repair(sql, registry, "dbo.May_2")
    assert "[GROSS_PREMIUM]" in repaired


def test_empty_registry_passes_sql_through_unchanged():
    """When the data source is down, validation must not reject every query."""
    empty = ColumnRegistry([])
    sql = "SELECT TOP 1 [ANYTHING] FROM dbo.May_2"
    repaired, repairs = validate_and_repair(sql, empty, "dbo.May_2")
    assert repaired == sql
    assert repairs == []
