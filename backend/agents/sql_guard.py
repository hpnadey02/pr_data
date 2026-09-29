"""Validates and hardens LLM-generated SQL before it ever reaches the data source.

Defense in depth for a single-table, read-only app:
  1. Must be exactly one SELECT statement (no DDL/DML, no stacked statements).
  2. Must reference only the configured table (dbo.May_2) - blocks table-name
     hallucination and any attempt to pivot to a different object.
  3. Anything that could write is blocked outright regardless of position. The rule lives
     in backend/core/read_only.py, shared with the data sources' second check.
  4. A row cap (TOP N) is injected if the model omitted one.
  5. Misplaced clauses the model emits when it runs out of context - most often a stray
     `TOP 1` appended after ORDER BY - are repaired rather than sent to the database,
     where they only produce "Incorrect syntax near the keyword 'TOP'".
"""
import re

from backend.core.read_only import describe_write_operation, find_write_operation
from config.settings import get_settings

settings = get_settings()

_SELECT_START = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
_TOP_PRESENT = re.compile(r"^\s*SELECT\s+(DISTINCT\s+)?TOP\s*\(?\s*\d+", re.IGNORECASE)
_FENCE = re.compile(r"^```(sql)?|```$", re.IGNORECASE | re.MULTILINE)
_TABLE_REF = re.compile(r"\b(?:FROM|JOIN)\s+\[?([\w\.\[\]]+)\]?", re.IGNORECASE)
# A `TOP n` that the model appended to the END of the statement instead of after SELECT.
_TRAILING_TOP = re.compile(r"\s*\bTOP\s*\(?\s*(\d+)\s*\)?\s*;?\s*$", re.IGNORECASE)
_LEADING_LABEL = re.compile(r"^\s*(?:sql|query|answer|output)\s*[:\-]\s*", re.IGNORECASE)
# `SELECT *` or `SELECT t.*` anywhere in the statement.
_STAR_SELECT = re.compile(r"(?i)\bSELECT\s+(?:DISTINCT\s+)?(?:TOP\s*\(?\s*\d+\s*\)?\s+)?(?:\w+\s*\.\s*)?\*")
_AGGREGATE = re.compile(r"(?i)\b(SUM|COUNT|AVG|MIN|MAX)\s*\(")

# A raw-row query is legitimate (single-policy lookup) but must stay small. Aggregates may
# return up to DB_MAX_ROWS because each row is already a summary of many.
MAX_RAW_ROWS = 100


class SQLValidationError(Exception):
    pass


def _strip_fences(sql: str) -> str:
    sql = _FENCE.sub("", str(sql or "")).strip()
    sql = _LEADING_LABEL.sub("", sql)
    # Some models emit a prose sentence before the statement; keep from the first SELECT/WITH.
    match = re.search(r"(?is)\b(SELECT|WITH)\b", sql)
    if match and match.start() > 0:
        prefix = sql[: match.start()].strip()
        # Only drop the prefix when it is prose, not a legitimate leading comment/paren.
        if prefix and not prefix.endswith(("(", ",")):
            sql = sql[match.start():]
    sql = sql.strip()
    while sql.endswith(";"):
        sql = sql[:-1].strip()
    return sql


def _normalize_table_name(name: str) -> str:
    return name.strip().strip("[]").lower()


def _relocate_trailing_top(sql: str) -> str:
    """Move a stray trailing `TOP n` back to where T-SQL expects it.

    Observed in logs/app.log: the model emitted `... ORDER BY Total_Premium DESC\\nTOP 1`,
    which SQL Server rejects with error 156. The intent is unambiguous, so it is repaired
    instead of burning a retry.
    """
    match = _TRAILING_TOP.search(sql)
    if not match:
        return sql
    limit = match.group(1)
    body = sql[: match.start()].rstrip()
    if _TOP_PRESENT.match(body):
        # A TOP already exists at the front; the trailing one is redundant - drop it.
        return body
    return re.sub(
        r"^\s*SELECT\s+(DISTINCT\s+)?",
        lambda m: f"SELECT {m.group(1) or ''}TOP {limit} ",
        body,
        count=1,
        flags=re.IGNORECASE,
    )


def validate_sql(sql: str) -> str:
    """Returns a sanitized, safe-to-execute SQL string or raises SQLValidationError."""
    sql = _strip_fences(sql)

    if not sql:
        raise SQLValidationError("The model returned no SQL statement.")

    if ";" in sql:
        raise SQLValidationError("Multiple SQL statements are not allowed.")

    if not _SELECT_START.match(sql):
        raise SQLValidationError("Only SELECT statements are allowed.")

    offending = find_write_operation(sql)
    if offending:
        raise SQLValidationError(
            "Generated SQL contains a disallowed keyword: "
            f"{describe_write_operation(offending)}. Only read-only SELECT queries are allowed."
        )

    sql = _relocate_trailing_top(sql)

    allowed_table = _normalize_table_name(settings.DB_TABLE)
    referenced = [_normalize_table_name(t) for t in _TABLE_REF.findall(sql)]
    if not referenced:
        raise SQLValidationError("Could not determine the table referenced by the SQL.")

    # Names introduced by the statement itself (CTEs, derived tables) are legitimate.
    local_names = {
        name.lower()
        for name in re.findall(r"(?i)\b(?:WITH|,)\s*([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(", sql)
    }
    allowed_names = {allowed_table, allowed_table.split(".")[-1], *local_names}
    for table in referenced:
        if table not in allowed_names:
            raise SQLValidationError(
                f"Generated SQL references an unexpected table '{table}'. "
                f"Only {settings.DB_TABLE} is permitted."
            )

    # `SELECT *` on a table of tens of millions of rows x 124 columns would stream the
    # whole thing into pandas and exhaust the worker's memory. The prompt forbids it, but
    # a prompt is a request - this is the enforcement.
    if _STAR_SELECT.search(sql):
        raise SQLValidationError(
            "SELECT * is not allowed on this table - it would return every one of its "
            "124 columns. Name the columns you need, or aggregate with SUM/COUNT/AVG "
            "and GROUP BY."
        )

    sql = _enforce_row_cap(sql)
    return sql


def _enforce_row_cap(sql: str) -> str:
    """Guarantee a TOP n, and keep raw-row queries far smaller than aggregates.

    An aggregate row is already a summary of many source rows, so DB_MAX_ROWS is a
    sensible cap there. A raw-row query returns source rows one-for-one, so it gets the
    much tighter MAX_RAW_ROWS - this is what stops an accidental full-table fetch.
    """
    is_aggregate = bool(_AGGREGATE.search(sql))
    ceiling = settings.DB_MAX_ROWS if is_aggregate else MAX_RAW_ROWS

    match = _TOP_PRESENT.match(sql)
    if not match:
        return re.sub(
            r"^\s*SELECT\s+(DISTINCT\s+)?",
            lambda m: f"SELECT {m.group(1) or ''}TOP {ceiling} ",
            sql,
            count=1,
            flags=re.IGNORECASE,
        )

    requested = int(re.search(r"\d+", match.group(0)).group())
    if requested <= ceiling:
        return sql
    # The model asked for more than the cap allows - lower it rather than reject, so the
    # user still gets an answer.
    return re.sub(
        r"^(\s*SELECT\s+(?:DISTINCT\s+)?TOP\s*\(?\s*)\d+",
        lambda m: f"{m.group(1)}{ceiling}",
        sql,
        count=1,
        flags=re.IGNORECASE,
    )
