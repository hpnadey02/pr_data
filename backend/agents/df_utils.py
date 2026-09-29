"""Shared DataFrame introspection helpers used by both the chart and insight agents.

These functions decide, from a fetched result alone, what to plot and what to summarise.
They therefore depend on the DataFrame carrying honest dtypes - see
`normalize_result_frame` in backend/core/datasource.py, which is what guarantees that a
SQL Server result and a DuckDB result of the same query look identical here.
"""
from typing import Optional

import pandas as pd

from backend.core.identifiers import (
    IDENTIFIER_NAME_TOKENS,
    MEASURE_NAME_TOKENS,
    RATIO_NAME_TOKENS,
    tokens,
)

MAX_CATEGORY_CARDINALITY = 30
DATE_HINTS = ("date", "week", "month", "week_start", "quarter", "day", "year")


def numeric_columns(df: pd.DataFrame) -> list[str]:
    """Columns safe to aggregate. Booleans are numeric to pandas but never a measure."""
    return [
        c for c in df.columns
        if pd.api.types.is_numeric_dtype(df[c]) and not pd.api.types.is_bool_dtype(df[c])
    ]


def date_like_columns(df: pd.DataFrame) -> list[str]:
    """Columns usable as a time axis.

    A real datetime dtype always qualifies. A NAME hint only qualifies when the column is
    not numeric - otherwise a measure aliased `premium_this_month` would be picked as the
    x-axis and the chart would be nonsense.
    """
    cols = []
    for c in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[c]):
            cols.append(c)
            continue
        if pd.api.types.is_numeric_dtype(df[c]):
            continue
        if any(h in str(c).lower() for h in DATE_HINTS):
            cols.append(c)
    return cols


def categorical_columns(df: pd.DataFrame, exclude: set[str]) -> list[str]:
    cols = []
    for c in df.columns:
        if c in exclude or pd.api.types.is_numeric_dtype(df[c]):
            continue
        if df[c].nunique(dropna=True) <= MAX_CATEGORY_CARDINALITY:
            cols.append(c)
    return cols


def _registry_category(name: str) -> Optional[str]:
    """The live schema's own verdict on a column, when it knows it.

    Never fuzzy-matched: guessing that an alias means some physical column could hand
    back the wrong category and pick the wrong measure.
    """
    try:
        from backend.core.column_registry import get_registry

        registry = get_registry()
        physical = registry.resolve(name, allow_fuzzy=False)
        info = registry.get(physical) if physical else None
        return info.category if info else None
    except Exception:  # noqa: BLE001 - introspection must never break a response
        return None


def _score_measure(name: str) -> int:
    """Rank a numeric column's fitness as THE measure. 0 means never."""
    name_tokens = set(tokens(name))

    if name_tokens & RATIO_NAME_TOKENS:
        return 1
    if name_tokens & IDENTIFIER_NAME_TOKENS:
        return 0
    if _registry_category(name) == "dimension":
        return 0
    if name_tokens & MEASURE_NAME_TOKENS or _registry_category(name) == "measure":
        return 3
    return 2


def pick_measure(df: pd.DataFrame, numeric_cols: list[str]) -> Optional[str]:
    """Choose the column to aggregate, or None when the result has nothing to aggregate.

    Taking the first numeric column was wrong whenever the query also selected a numeric
    identifier: `SELECT BRANCH_OFFICE_CODE, SUM(GROSS_PREMIUM) AS total` would chart the
    branch code. Identifier-like columns now score 0 and are never chosen, and a column
    the live schema calls a measure wins outright.
    """
    if not numeric_cols:
        return None

    ranked = [(_score_measure(name), index, name) for index, name in enumerate(numeric_cols)]
    best_score = max(score for score, _, _ in ranked)
    if best_score == 0:
        # Every numeric column is an identifier - e.g. `SELECT TOP 10 POLICY_NO`.
        # Saying so honestly beats plotting a policy number as if it were a value.
        return None
    # Ties keep the query's own column order, which puts the aggregate where the model
    # intended it.
    return min((r for r in ranked if r[0] == best_score), key=lambda r: r[1])[2]
