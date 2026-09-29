"""Deterministic answers, and a guard that stops the LLM inventing figures.

The bug this exists to prevent, straight from a real session:

    SQL   : SELECT TOP 1 [USGI_SUM_INSURED] FROM dbo.May_2 WHERE ...
    Result: 660000
    Shown : "The policy ... has a USGI sum insured of $1,200,000."

The model was handed correct statistics and still restated the number wrongly, in a
currency the data does not use. Two rules follow from that:

  1. When the SQL result IS the answer - a single row, one or a few scalar values - the
     answer is formatted directly from the DataFrame. No model is involved at all, so
     there is nothing to hallucinate.

  2. When a narrative is genuinely useful (many rows, rankings, trends), the model may
     phrase it, but every number it emits is then checked against the set of figures it
     was given. A figure that was not in that set means the narrative is rejected and the
     deterministic template is shown instead.

Formatting never invents a currency symbol; values are rendered exactly as stored, with
thousands separators only.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal
from numbers import Number

import pandas as pd

from backend.core.identifiers import readable

# Up to this many rows/columns, the result itself is the answer and no LLM is used.
MAX_DIRECT_ROWS = 1
MAX_DIRECT_COLUMNS = 6


def format_value(value) -> str:
    """Render one cell exactly as stored - separators only, never a currency symbol."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "not available"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.strftime("%Y-%m-%d") if value.time() == datetime.min.time() else value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, Number):
        number = float(value)
        if number.is_integer():
            return f"{int(number):,}"
        return f"{number:,.2f}"
    text = str(value).strip()
    return text if text else "not available"


def _cell(frame: pd.DataFrame, row: int, column: str):
    value = frame.iloc[row][column]
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        try:
            return value.item()
        except (AttributeError, ValueError):
            return value
    return value


def is_direct_answer(frame: pd.DataFrame | None) -> bool:
    """True when the result set IS the answer and no narration is needed."""
    if frame is None or frame.empty:
        return False
    return len(frame) <= MAX_DIRECT_ROWS and len(frame.columns) <= MAX_DIRECT_COLUMNS


def build_direct_answer(frame: pd.DataFrame) -> str:
    """Format a single-row result straight from the DataFrame. No model involved."""
    row_index = 0

    if len(frame.columns) == 1:
        column = frame.columns[0]
        value = _cell(frame, row_index, column)
        return f"The {readable(column)} is {format_value(value)}."

    parts = [
        f"{readable(column)}: {format_value(_cell(frame, row_index, column))}"
        for column in frame.columns
    ]
    return "The matching record has " + ", ".join(parts) + "."


# --------------------------------------------------------------------------------------
# Numeric verification of model-written prose
# --------------------------------------------------------------------------------------

_NUMBER_IN_TEXT = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
# Small integers are ordinary English ("3 branches", "top 5") and are not treated as
# data figures that must be justified.
_TRIVIAL_MAX = 100


def collect_allowed_numbers(*sources) -> set[float]:
    """Every numeric value the model is permitted to state, from stats dicts / frames."""
    allowed: set[float] = set()

    def visit(node) -> None:
        if node is None or isinstance(node, bool):
            return
        if isinstance(node, Number):
            value = float(node)
            allowed.add(round(value, 2))
            allowed.add(float(round(value)))
            # A total of 1234567.89 may legitimately be stated as 1234568 or 1.23 million.
            allowed.add(round(value / 1000, 2))
            allowed.add(round(value / 100000, 2))
            allowed.add(round(value / 1000000, 2))
            return
        if isinstance(node, str):
            for token in _NUMBER_IN_TEXT.findall(node):
                try:
                    visit(float(token.replace(",", "")))
                except ValueError:
                    continue
            return
        if isinstance(node, dict):
            for value in node.values():
                visit(value)
            return
        if isinstance(node, (list, tuple, set)):
            for value in node:
                visit(value)
            return
        if isinstance(node, pd.DataFrame):
            for column in node.columns:
                if pd.api.types.is_numeric_dtype(node[column]):
                    for value in node[column].dropna().tolist():
                        visit(value)
            return

    for source in sources:
        visit(source)
    return allowed


def unsupported_numbers(text: str, allowed: set[float]) -> list[str]:
    """Figures in `text` that do not trace back to the data it was given."""
    offenders: list[str] = []
    for token in _NUMBER_IN_TEXT.findall(text or ""):
        try:
            value = float(token.replace(",", ""))
        except ValueError:
            continue
        if abs(value) <= _TRIVIAL_MAX and float(value).is_integer():
            continue
        candidates = {round(value, 2), float(round(value))}
        if candidates & allowed:
            continue
        offenders.append(token)
    return offenders


# The source columns carry no currency unit, so any symbol the model adds is fabricated.
# Indian and Western forms are both listed because the model reaches for either.
_CURRENCY = re.compile(
    r"[$£€₹]\s*|\b(?:USD|EUR|GBP|INR|Rs\.?|dollars?|rupees?|euros?|pounds?)\b\s*",
    re.IGNORECASE,
)


def has_invented_currency(text: str) -> bool:
    return bool(_CURRENCY.search(text or ""))


def strip_currency(text: str) -> str:
    """Remove fabricated currency markers, leaving the figures themselves untouched.

    Discarding an otherwise-correct narrative just because the model prefixed a '$' throws
    away good prose. The symbol is removed and the numbers are then verified as usual; a
    narrative whose FIGURES are wrong is still rejected outright.
    """
    cleaned = _CURRENCY.sub("", str(text or ""))
    return re.sub(r"[ \t]{2,}", " ", cleaned)


def verify_narrative(text: str, allowed: set[float]) -> tuple[bool, str, str]:
    """(is_trustworthy, reason, cleaned_text).

    `cleaned_text` is the narrative with any invented currency stripped; use it in place of
    the original when the verdict is True.
    """
    raw = str(text or "")
    if not raw.strip():
        return False, "the model returned an empty narrative", ""

    cleaned = strip_currency(raw)
    offenders = unsupported_numbers(cleaned, allowed)
    if offenders:
        return False, (
            "the narrative contained figure(s) not present in the query result: "
            + ", ".join(offenders[:5])
        ), cleaned

    if cleaned != raw:
        return True, "currency-stripped", cleaned
    return True, "ok", cleaned
