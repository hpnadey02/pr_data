import pytest

from backend.agents.sql_guard import MAX_RAW_ROWS, SQLValidationError, validate_sql
from config.settings import get_settings

settings = get_settings()
TABLE = settings.DB_TABLE


def test_allows_simple_select_and_injects_top():
    sql = f"SELECT [BRANCH NAME], SUM([GROSS PREMIUM]) FROM {TABLE} GROUP BY [BRANCH NAME]"
    result = validate_sql(sql)
    assert result.strip().upper().startswith("SELECT TOP")
    assert TABLE.split(".")[-1].lower() in result.lower()


def test_keeps_existing_top():
    sql = f"SELECT TOP 10 [BRANCH NAME] FROM {TABLE}"
    result = validate_sql(sql)
    assert "TOP 10" in result


def test_strips_markdown_fences():
    sql = f"```sql\nSELECT TOP 5 [BRANCH_NAME] FROM {TABLE}\n```"
    result = validate_sql(sql)
    assert "```" not in result


# --------------------------------------------------------------------------------------
# Result-size enforcement. The table holds tens of millions of rows, so an unbounded or
# all-columns query would stream the whole thing into pandas and exhaust the worker.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM {t}",
        "SELECT TOP 100 * FROM {t}",
        "SELECT DISTINCT * FROM {t}",
        "SELECT m.* FROM {t} m",
    ],
)
def test_select_star_is_rejected(sql):
    with pytest.raises(SQLValidationError, match="SELECT \\* is not allowed"):
        validate_sql(sql.format(t=TABLE))


def test_raw_row_query_is_capped_tighter_than_an_aggregate():
    """A raw row is one source row; an aggregate row already summarises many."""
    raw = validate_sql(f"SELECT [POLICY_NO] FROM {TABLE}")
    assert f"TOP {MAX_RAW_ROWS}" in raw

    agg = validate_sql(
        f"SELECT [BRANCH_NAME], SUM([GROSS_PREMIUM]) AS total FROM {TABLE} "
        "GROUP BY [BRANCH_NAME]"
    )
    assert f"TOP {settings.DB_MAX_ROWS}" in agg


def test_oversized_raw_request_is_lowered_not_rejected():
    """Still answer the question, just with fewer rows."""
    result = validate_sql(f"SELECT TOP 90000 [POLICY_NO] FROM {TABLE}")
    assert f"TOP {MAX_RAW_ROWS}" in result
    assert "90000" not in result


def test_a_reasonable_explicit_top_is_left_alone():
    result = validate_sql(f"SELECT TOP 10 [BRANCH_NAME] FROM {TABLE}")
    assert "TOP 10" in result


@pytest.mark.parametrize("keyword", ["DROP TABLE", "DELETE FROM", "UPDATE", "INSERT INTO", "TRUNCATE TABLE", "EXEC sp_who"])
def test_rejects_forbidden_keywords(keyword):
    sql = f"SELECT * FROM {TABLE}; {keyword} {TABLE}"
    with pytest.raises(SQLValidationError):
        validate_sql(sql)


def test_rejects_non_select():
    with pytest.raises(SQLValidationError):
        validate_sql(f"UPDATE {TABLE} SET [GROSS PREMIUM] = 0")


def test_rejects_wrong_table():
    with pytest.raises(SQLValidationError):
        validate_sql("SELECT * FROM some_other_table")


def test_rejects_multiple_statements():
    with pytest.raises(SQLValidationError):
        validate_sql(f"SELECT * FROM {TABLE}; SELECT * FROM {TABLE}")


# --------------------------------------------------------------------------------------
# Read-only enforcement (backend/core/read_only.py). Each of these passed the old
# keyword list, or passed it without saying what was wrong.
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "sql, keyword",
    [
        (f"SELECT TOP 5 [BRANCH_NAME] INTO dbo.copied FROM {TABLE}", "INTO"),
        (f"SELECT TOP 5 [BRANCH_NAME] INTO #t FROM {TABLE}", "INTO"),
        # T-SQL runs the second statement even without a semicolon.
        (f"SELECT TOP 5 [BRANCH_NAME] FROM {TABLE} DELETE FROM {TABLE}", "DELETE"),
        (f"SELECT TOP 5 [BRANCH_NAME] FROM {TABLE} WITH (UPDLOCK)", "UPDLOCK"),
        (f"SELECT TOP 5 [BRANCH_NAME] FROM {TABLE} WAITFOR DELAY '00:10:00'", "WAITFOR"),
        ("SELECT TOP 5 * FROM OPENROWSET(BULK 'C:/x.csv', SINGLE_CLOB) AS f", "OPENROWSET"),
        (f"SELECT TOP 5 [BRANCH_NAME] FROM {TABLE} DENY SELECT ON {TABLE} TO public", "DENY"),
        (f"SELECT TOP 5 [BRANCH_NAME] FROM {TABLE} DBCC CHECKDB", "DBCC"),
    ],
)
def test_write_constructs_are_rejected_by_name(sql, keyword):
    with pytest.raises(SQLValidationError) as info:
        validate_sql(sql)
    message = str(info.value)
    assert message.startswith("Generated SQL contains a disallowed keyword")
    assert f": {keyword} (" in message
    assert "read-only SELECT" in message


def test_select_into_message_says_why():
    with pytest.raises(SQLValidationError, match=r"INTO \(SELECT \.\.\. INTO creates a table\)"):
        validate_sql(f"SELECT [BRANCH_NAME] INTO dbo.copied FROM {TABLE}")


@pytest.mark.parametrize("value", ["CALL CENTER", "MARINE EXPORT", "BULK CARGO"])
def test_a_filter_value_that_looks_like_a_keyword_is_allowed(value):
    """'CALL CENTER' is a real User_Name value."""
    result = validate_sql(
        f"SELECT TOP 5 [BRANCH_NAME], SUM([GROSS_PREMIUM]) AS total FROM {TABLE} "
        f"WHERE [User_Name] = '{value}' GROUP BY [BRANCH_NAME]"
    )
    assert value in result


def test_columns_containing_keywords_are_allowed():
    result = validate_sql(
        f"SELECT TOP 5 [Business_Type_Fresh_Renewal], SUM([LOADING_ON_PREMIUM]) AS total "
        f"FROM {TABLE} GROUP BY [Business_Type_Fresh_Renewal]"
    )
    assert "LOADING_ON_PREMIUM" in result
