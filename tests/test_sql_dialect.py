"""Unit tests for the T-SQL -> DuckDB translator (backend/core/sql_dialect.py)."""
import pytest

from backend.core.sql_dialect import SQLDialectError, translate


# --------------------------------------------------------------------------------------
# The four translations named in the requirements
# --------------------------------------------------------------------------------------

def test_top_becomes_limit():
    out = translate("SELECT TOP 5 [BRANCH_NAME] FROM dbo.May_2")
    assert "TOP" not in out.upper()
    assert out.strip().endswith("LIMIT 5")


def test_top_with_parentheses():
    out = translate("SELECT TOP (25) [A] FROM dbo.May_2")
    assert out.strip().endswith("LIMIT 25")


def test_top_with_distinct_is_preserved():
    out = translate("SELECT DISTINCT TOP 3 [STATE] FROM dbo.May_2")
    assert out.upper().startswith("SELECT DISTINCT")
    assert out.strip().endswith("LIMIT 3")


def test_getdate_becomes_current_date():
    assert "CURRENT_DATE" in translate("SELECT TOP 1 GETDATE() FROM dbo.May_2")


def test_isnull_becomes_coalesce():
    out = translate("SELECT TOP 1 ISNULL([A], 0) FROM dbo.May_2")
    assert "COALESCE([A], 0)".replace("[A]", '"A"') in out


def test_brackets_become_double_quotes():
    out = translate("SELECT TOP 1 [GROSS_PREMIUM] FROM dbo.May_2")
    assert '"GROSS_PREMIUM"' in out
    assert "[" not in out


def test_dbo_prefix_removed():
    out = translate("SELECT TOP 1 [A] FROM dbo.May_2")
    assert "dbo." not in out
    assert "May_2" in out


def test_bracketed_dbo_prefix_removed():
    out = translate("SELECT TOP 1 [A] FROM [dbo].[May_2]")
    assert "dbo" not in out.lower()
    assert '"May_2"' in out


# --------------------------------------------------------------------------------------
# Literal safety - values must never be rewritten
# --------------------------------------------------------------------------------------

def test_string_literals_are_untouched():
    sql = "SELECT TOP 1 [A] FROM dbo.May_2 WHERE [B] = 'dbo.May_2 [x] TOP 3'"
    out = translate(sql)
    assert "'dbo.May_2 [x] TOP 3'" in out


def test_escaped_quote_inside_literal():
    sql = "SELECT TOP 1 [A] FROM dbo.May_2 WHERE [B] = 'O''Brien [x]'"
    out = translate(sql)
    assert "'O''Brien [x]'" in out


def test_slash_bearing_identifier_value_survives():
    sql = (
        "SELECT TOP 1 [USGI_SUM_INSURED] FROM dbo.May_2 "
        "WHERE [USGIpos_Policy_Number] = 'AVO/2316/20138002'"
    )
    out = translate(sql)
    assert "'AVO/2316/20138002'" in out


def test_unicode_prefix_stripped():
    out = translate("SELECT TOP 1 [A] FROM dbo.May_2 WHERE [B] = N'text'")
    assert "N'text'" not in out
    assert "'text'" in out


# --------------------------------------------------------------------------------------
# Date functions
# --------------------------------------------------------------------------------------

def test_format_month_bucket():
    out = translate("SELECT TOP 5 FORMAT([POLICY_ISSUE_DATE], 'yyyy-MM') AS month FROM dbo.May_2")
    assert "strftime" in out
    assert "'%Y-%m'" in out


def test_dateadd_and_datepart_week_bucket():
    sql = (
        "SELECT TOP 5 CAST(DATEADD(day, -(DATEPART(weekday, [POLICY_ISSUE_DATE]) - 1), "
        "[POLICY_ISSUE_DATE]) AS date) AS week_start FROM dbo.May_2"
    )
    out = translate(sql)
    assert "INTERVAL 1 DAY" in out
    assert "dayofweek" in out
    assert "DATEADD" not in out.upper()
    assert "DATEPART" not in out.upper()


def test_datediff_argument_order():
    out = translate("SELECT TOP 1 DATEDIFF(day, [START_DATE], [EXPIRY_DATE]) FROM dbo.May_2")
    assert "date_diff('day'" in out


def test_datepart_year_uses_extract():
    out = translate("SELECT TOP 1 DATEPART(year, [START_DATE]) FROM dbo.May_2")
    assert "EXTRACT(YEAR FROM" in out


def test_unknown_date_part_is_rejected():
    with pytest.raises(SQLDialectError, match="Unsupported date part"):
        translate("SELECT TOP 1 DATEPART(fortnight, [START_DATE]) FROM dbo.May_2")


# --------------------------------------------------------------------------------------
# Other scalar rewrites
# --------------------------------------------------------------------------------------

def test_len_becomes_length():
    out = translate("SELECT TOP 1 LEN([INSURED_NAME]) FROM dbo.May_2")
    assert "LENGTH(" in out


def test_charindex_swaps_arguments():
    out = translate("SELECT TOP 1 CHARINDEX('x', [INSURED_NAME]) FROM dbo.May_2")
    assert 'strpos("INSURED_NAME", \'x\')' in out


def test_window_function_passes_through():
    sql = (
        "SELECT TOP 5 [STATE], SUM([GROSS_PREMIUM]) * 100.0 / "
        "SUM(SUM([GROSS_PREMIUM])) OVER () AS pct_share FROM dbo.May_2 GROUP BY [STATE]"
    )
    out = translate(sql)
    assert "OVER ()" in out
    assert out.strip().endswith("LIMIT 5")


def test_cte_is_supported():
    sql = (
        "WITH top_branches AS (SELECT TOP 5 [BRANCH_NAME] FROM dbo.May_2) "
        "SELECT [BRANCH_NAME] FROM top_branches"
    )
    out = translate(sql)
    assert "LIMIT 5" in out


# --------------------------------------------------------------------------------------
# Unsupported constructs must raise, never silently return different data
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "sql, label",
    [
        ("SELECT TOP 1 [A] FROM dbo.May_2 PIVOT (SUM([B]) FOR [C] IN ([X]))", "PIVOT"),
        ("SELECT TOP 10 PERCENT [A] FROM dbo.May_2", "TOP n PERCENT"),
        ("SELECT TOP 1 [A] FROM dbo.May_2 ORDER BY [A] WITH TIES", "WITH TIES"),
        ("SELECT TOP 1 CONVERT(varchar, [A]) FROM dbo.May_2", "CONVERT()"),
        ("SELECT TOP 1 IIF([A] > 1, 'y', 'n') FROM dbo.May_2", "IIF()"),
        ("SELECT TOP 1 [A] FROM dbo.May_2 CROSS APPLY dbo.f([A])", "CROSS/OUTER APPLY"),
        ("SELECT TOP 1 @total FROM dbo.May_2", "T-SQL variable"),
        ("SELECT TOP 1 [A] FROM #temp", "temporary table"),
    ],
)
def test_unsupported_constructs_raise_with_a_name(sql, label):
    with pytest.raises(SQLDialectError) as info:
        translate(sql)
    assert label.split("(")[0].strip() in str(info.value)


def test_empty_sql_raises():
    with pytest.raises(SQLDialectError):
        translate("   ")


def test_literal_containing_unsupported_keyword_is_not_rejected():
    """A VALUE that happens to read like a banned construct must not fail the query."""
    out = translate("SELECT TOP 1 [A] FROM dbo.May_2 WHERE [B] = 'PIVOT'")
    assert "'PIVOT'" in out


def test_translation_is_idempotent_for_plain_sql():
    sql = "SELECT TOP 1 [A] FROM dbo.May_2"
    assert translate(translate(sql)) == translate(sql)
