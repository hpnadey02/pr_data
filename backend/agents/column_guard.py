"""Validate and repair the column identifiers in model-generated SQL.

This closes the single biggest failure mode in logs/app.log, where every attempt of every
retry produced the same dead query:

    SELECT TOP 1 [BRANCH_NAME], SUM([GROSS PREMIUM]) ...
    -> Invalid column name 'GROSS PREMIUM'. (207)

The model wrote a column name with a space; the real column is GROSS_PREMIUM. Because the
error was only ever fed back as prose at temperature 0, the model re-emitted the identical
SQL three times and the request failed after ~100 seconds.

Now the identifier is resolved through the column registry (which ignores spaces,
underscores and case) and rewritten to the physical name before execution. Only when a
name cannot be resolved unambiguously does the query fail - and then the error names the
exact identifier and the closest real columns, so the retry prompt has something concrete
to act on.
"""
from __future__ import annotations

import re

from backend.core.column_registry import ColumnRegistry, get_registry
from backend.core.logging_config import get_logger

logger = get_logger(__name__)


class ColumnValidationError(Exception):
    """Raised when generated SQL references a column that does not exist."""


_BRACKETED = re.compile(r"\[([^\[\]]+)\]")
_ALIAS_DEF = re.compile(r"(?i)\bAS\s+(?:\[([^\[\]]+)\]|\"([^\"]+)\"|([A-Za-z_][A-Za-z0-9_]*))")
_BARE_WORD = re.compile(r"(?<![\[\"\w.])([A-Za-z_][A-Za-z0-9_]*)(?![\w\]\"(])")
_CTE_DEF = re.compile(r"(?i)\bWITH\s+([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(")

# Reserved words, T-SQL functions and shapes that must never be treated as a column.
_RESERVED = {
    "select", "from", "where", "group", "by", "order", "having", "top", "distinct",
    "as", "and", "or", "not", "in", "is", "null", "like", "between", "case", "when",
    "then", "else", "end", "asc", "desc", "inner", "left", "right", "full", "outer",
    "join", "on", "union", "all", "with", "over", "partition", "rows", "range",
    "unbounded", "preceding", "following", "current", "row", "cast", "convert", "try",
    "sum", "count", "avg", "min", "max", "abs", "round", "floor", "ceiling", "len",
    "length", "upper", "lower", "trim", "ltrim", "rtrim", "substring", "replace",
    "concat", "coalesce", "isnull", "nullif", "iif", "format", "dateadd", "datediff",
    "datepart", "datename", "getdate", "year", "month", "day", "week", "weekday",
    "quarter", "hour", "minute", "second", "date", "datetime", "int", "bigint",
    "decimal", "numeric", "float", "varchar", "nvarchar", "char", "bit", "cte",
    "row_number", "rank", "dense_rank", "ntile", "lag", "lead", "percent", "cross",
    "apply", "exists", "any", "some", "into", "values", "offset", "fetch", "next",
    "only", "first", "last", "limit", "dbo", "strftime", "extract", "interval",
}


def _declared_names(sql: str) -> set[str]:
    """Aliases and CTE names introduced by the query itself - these are not table columns."""
    names: set[str] = set()
    for match in _ALIAS_DEF.finditer(sql):
        name = match.group(1) or match.group(2) or match.group(3)
        if name:
            names.add(name.strip().lower())
    for match in _CTE_DEF.finditer(sql):
        names.add(match.group(1).strip().lower())
    return names


def _string_spans(sql: str) -> list[tuple[int, int]]:
    """Character ranges of single-quoted literals, so values are never rewritten."""
    spans: list[tuple[int, int]] = []
    index, length = 0, len(sql)
    while index < length:
        if sql[index] == "'":
            start = index
            index += 1
            while index < length:
                if sql[index] == "'":
                    if index + 1 < length and sql[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            spans.append((start, index))
            continue
        index += 1
    return spans


def _in_span(position: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in spans)


def validate_and_repair(
    sql: str,
    registry: ColumnRegistry | None = None,
    table_name: str = "",
) -> tuple[str, list[str]]:
    """Return (repaired_sql, repairs). Raises ColumnValidationError if a name is unknown.

    `repairs` is a human-readable list like ["[GROSS PREMIUM] -> [GROSS_PREMIUM]"] which
    is surfaced to the user as a warning so a silent rewrite is never invisible.
    """
    registry = registry or get_registry()
    if not registry.columns:
        # The live schema is unavailable (data source down). Validating against an empty
        # registry would reject every query, so pass the SQL through unchanged.
        return sql, []

    declared = _declared_names(sql)
    table_tokens = {t.lower() for t in re.split(r"[.\[\]]", table_name) if t}
    repairs: list[str] = []
    unresolved: list[tuple[str, list[str]]] = []
    spans = _string_spans(sql)

    # --- bracketed identifiers: [GROSS PREMIUM] -----------------------------------
    def _fix_bracketed(match: re.Match) -> str:
        raw = match.group(1)
        if _in_span(match.start(), spans):
            return match.group(0)
        key = raw.strip().lower()
        if key in declared or key in table_tokens:
            return match.group(0)
        if raw in registry.by_name:
            return match.group(0)
        resolved = registry.resolve(raw)
        if resolved:
            if resolved != raw:
                repairs.append(f"[{raw}] -> [{resolved}]")
            return f"[{resolved}]"
        unresolved.append((raw, registry.suggest(raw)))
        return match.group(0)

    repaired = _BRACKETED.sub(_fix_bracketed, sql)

    # --- bare identifiers that look like column names: GROSS_PREMIUM --------------
    # Only underscore-containing words are considered, so ordinary keywords, aliases and
    # single-word tokens are never touched.
    spans = _string_spans(repaired)

    def _fix_bare(match: re.Match) -> str:
        word = match.group(1)
        if "_" not in word:
            return word
        if _in_span(match.start(), spans):
            return word
        lowered = word.lower()
        if lowered in _RESERVED or lowered in declared or lowered in table_tokens:
            return word
        if word in registry.by_name:
            return f"[{word}]"
        resolved = registry.resolve(word)
        if resolved:
            if resolved != word:
                repairs.append(f"{word} -> [{resolved}]")
            return f"[{resolved}]"
        return word  # unknown bare word: leave it, the database will report it

    repaired = _BARE_WORD.sub(_fix_bare, repaired)

    if unresolved:
        details = "; ".join(
            f"'{name}'" + (f" (did you mean {', '.join(suggestions)}?)" if suggestions else "")
            for name, suggestions in unresolved
        )
        raise ColumnValidationError(
            f"The SQL references {len(unresolved)} column name(s) that do not exist in "
            f"{table_name or 'the table'}: {details}. Use ONLY the exact column names "
            f"listed in the prompt."
        )

    if repairs:
        logger.info("Repaired %s column identifier(s): %s", len(repairs), "; ".join(repairs))
    return repaired, repairs


def columns_referenced(sql: str, registry: ColumnRegistry | None = None) -> list[str]:
    """Physical columns actually referenced by a statement (used for logging/debug)."""
    registry = registry or get_registry()
    found: list[str] = []
    for raw in _BRACKETED.findall(sql):
        resolved = registry.resolve(raw)
        if resolved and resolved not in found:
            found.append(resolved)
    return found
