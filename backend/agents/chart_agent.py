"""Agent 6 - Chart Generation.

Only three chart types are permitted: **bar, pie, donut**. Line and scatter were removed
deliberately - every question this app answers compares magnitudes across categories or
periods, and a bar chart reads those correctly in every case. A period axis is drawn as an
ordered bar chart. An explicit "line chart" request is honoured as a bar rather than
refused.

Chart-TYPE selection is rule-based (not LLM-guessed) for reliability. In precedence order:
  1. a chart type named in the question (filters.explicit_chart_type), coerced onto a
     permitted type
  2. CHART_TYPE in .env - one type, or a list that draws one chart of each
  3. "auto", the per-question rule:
       "contribution"/"share" route with <=8 slices -> donut  ("market share")
       anything else with a measure + an axis       -> bar
  No usable measure or axis -> table view only, whatever was asked for.

A pie/donut the data cannot honestly be drawn as becomes a bar, with a warning saying why:
a time axis (a pie has no order, so it cannot show a trend) or negative values (plotly
silently drops negative slices, misstating every share). Beyond 8 slices the smallest are
summed into one "Others" slice, so the pie stays readable and its total unchanged.
"""
import re
import time
from typing import Optional

import pandas as pd
import plotly.express as px

from backend.agents.df_utils import categorical_columns, date_like_columns, numeric_columns, pick_measure
from backend.agents.state import AgentState
from backend.core.figure_codec import figure_to_json
from backend.core.logging_config import get_logger
from config.settings import ALLOWED_CHART_TYPES, get_settings

logger = get_logger(__name__)

_MAX_PIE_SLICES = 8
# The insight words a missing value the same way (answer_builder.format_value).
_MISSING_LABEL = "not available"


def _configured_chart_types() -> tuple[str, ...]:
    """CHART_TYPE from .env; empty means choose per question."""
    return get_settings().chart_types


def _label_missing(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Plotly silently drops a bar or slice whose label is null, and the NULL group is
    often the largest one - so it would vanish from the chart while the insight counts it."""
    to_fill = [
        c for c in columns
        if not pd.api.types.is_datetime64_any_dtype(df[c]) and df[c].isna().any()
    ]
    if not to_fill:
        return df
    df = df.copy()
    for c in to_fill:
        df[c] = df[c].astype(object).where(df[c].notna(), _MISSING_LABEL)
    return df


def _pie_frame(df: pd.DataFrame, x: str, y: str) -> pd.DataFrame:
    totals = df.groupby(x, sort=False)[y].sum().sort_values(ascending=False)
    if len(totals) > _MAX_PIE_SLICES:
        kept = totals.iloc[: _MAX_PIE_SLICES - 1]
        rest = totals.iloc[_MAX_PIE_SLICES - 1:]
        totals = pd.concat([kept, pd.Series({f"Others ({len(rest)})": rest.sum()})])
    return totals.rename_axis(x).reset_index(name=y)


def _build_figure(df: pd.DataFrame, chart_type: str, x: str, y: str, color: Optional[str],
                  title: str, sort_by_x: bool = False):
    df = _label_missing(df, [x] + ([color] if color else []))
    if chart_type in ("pie", "donut"):
        fig = px.pie(_pie_frame(df, x, y), names=x, values=y,
                     hole=0.45 if chart_type == "donut" else 0.0, title=title)
    else:
        # A period axis must stay in chronological order; everything else reads best
        # ranked by magnitude.
        df_plot = df.sort_values(by=x) if sort_by_x else df.sort_values(by=y, ascending=False)
        fig = px.bar(df_plot, x=x, y=y, color=color, title=title)
    fig.update_layout(margin=dict(l=10, r=10, t=50, b=10), height=420)
    return fig


def _chart_payload(df: pd.DataFrame, chart_type: str, x: str, y: str, color: Optional[str],
                   title: str, sort_by_x: bool = False) -> dict:
    fig = _build_figure(df, chart_type, x, y, color, title, sort_by_x=sort_by_x)
    # Never fig.to_json() directly: its output only loads in the same plotly major
    # version, and the frontend process may not have the same one (figure_codec.py).
    return {"chart_type": chart_type, "title": title, "figure_json": figure_to_json(fig)}


def _coerce_chart_type(requested: Optional[str]) -> Optional[str]:
    """Map anything the user or router asks for onto a permitted type."""
    if not requested:
        return None
    requested = requested.lower()
    if requested in ALLOWED_CHART_TYPES:
        return requested
    # An explicit "line chart"/"scatter" request is honoured as a bar chart rather than
    # refused - the user still gets the comparison they asked for.
    if requested in ("line", "scatter", "area"):
        return "bar"
    return None


def _requested_chart_types(explicit: Optional[str]) -> list[str]:
    """The question's own wording beats the .env default; empty means choose per question."""
    coerced = _coerce_chart_type(explicit)
    if coerced:
        return [coerced]
    return list(_configured_chart_types())


def _pie_blocker(df: pd.DataFrame, measure: str, time_axis: bool) -> Optional[str]:
    """Why this result cannot honestly be drawn as a pie/donut, or None if it can."""
    if time_axis:
        return "a pie has no order, so it cannot show a trend over time"
    if (df[measure] < 0).any():
        return "the result has negative values, which a pie silently drops"
    return None


def _chart_types(df: pd.DataFrame, route: str, explicit: Optional[str], x: str, measure: str,
                 time_axis: bool) -> tuple[list[str], list[str]]:
    """The chart types to draw for this result, plus a warning for each one swapped."""
    blocker = _pie_blocker(df, measure, time_axis)
    requested = _requested_chart_types(explicit)
    if not requested:
        # Share-of-total questions with few slices are the only case a pie/donut beats a bar.
        few_slices = df[x].nunique(dropna=False) <= _MAX_PIE_SLICES
        return (["donut"] if route == "contribution" and few_slices and not blocker else ["bar"]), []

    types, notes = [], []
    for chart_type in requested:
        if chart_type in ("pie", "donut") and blocker:
            notes.append(f"{chart_type.capitalize()} chart drawn as a bar instead: {blocker}.")
            chart_type = "bar"
        if chart_type not in types:
            types.append(chart_type)
    return types, notes


def _single_charts(df: pd.DataFrame, state: AgentState) -> tuple[list[dict], list[str]]:
    date_cols = date_like_columns(df)
    numeric_cols = numeric_columns(df)
    cat_cols = categorical_columns(df, exclude=set(date_cols))

    measure = pick_measure(df, numeric_cols)
    if not measure:
        return [], []

    # A period axis wins over a category axis: "premium by month" should be bars over
    # time, not bars over whatever category happens to also be in the result.
    if date_cols:
        x, time_axis = date_cols[0], True
        bar_color = cat_cols[0] if cat_cols else None
    elif cat_cols:
        x, time_axis = cat_cols[0], False
        bar_color = cat_cols[1] if len(cat_cols) > 1 else None
    else:
        return [], []

    explicit = (state.get("filters") or {}).get("explicit_chart_type")
    types, notes = _chart_types(df, state.get("route", ""), explicit, x, measure, time_axis)
    title = state["rewritten_question"][:90]
    charts = [
        _chart_payload(df, chart_type, x, measure, bar_color if chart_type == "bar" else None,
                       title, sort_by_x=time_axis)
        for chart_type in types
    ]
    return charts, notes


_STOPWORDS = {"show", "with", "their", "that", "this", "have", "high", "identify", "generate",
              "compare", "separate", "charts", "chart", "business", "performing", "performance"}


def _match_column_for_subquestion(sub_q: str, cat_cols: list[str], retrieved_columns: list[dict]) -> Optional[str]:
    """Business terms in a sub-question ("zone") rarely match the real column name
    ("STATE") by substring - so this also checks the retrieval agent's column
    descriptions/aliases (e.g. STATE's indexed doc includes "zone, region") before
    falling back to a plain name match."""
    sub_q_lower = sub_q.lower()
    cat_cols_lower = {c.lower(): c for c in cat_cols}

    for c in cat_cols:
        if c.lower() in sub_q_lower or any(tok in sub_q_lower for tok in c.lower().split()):
            return c

    words = [w for w in re.findall(r"[a-z]+", sub_q_lower) if len(w) > 3 and w not in _STOPWORDS]
    for rc in retrieved_columns:
        col = (rc.get("column") or "")
        if col.lower() not in cat_cols_lower:
            continue
        desc = (rc.get("description") or "").lower()
        if any(w in desc for w in words):
            return cat_cols_lower[col.lower()]
    return None


def _decomposed_charts(df: pd.DataFrame, state: AgentState) -> tuple[list[dict], list[str]]:
    charts = []
    date_cols = date_like_columns(df)
    numeric_cols = numeric_columns(df)
    measure = pick_measure(df, numeric_cols)
    cat_cols = categorical_columns(df, exclude=set(date_cols))
    retrieved_columns = state.get("retrieved_columns", [])
    explicit = (state.get("filters") or {}).get("explicit_chart_type")
    warnings = []

    for sub_q in state.get("sub_questions", []):
        sub_q_lower = sub_q.lower()
        match = _match_column_for_subquestion(sub_q, cat_cols, retrieved_columns)
        if not match or not measure:
            warnings.append(f"Could not build a dedicated chart for: \"{sub_q}\"")
            continue
        route = "contribution" if ("contribution" in sub_q_lower or "share" in sub_q_lower) else ""
        types, notes = _chart_types(df, route, explicit, match, measure, time_axis=False)
        warnings.extend(notes)
        title = sub_q[:90]
        charts.extend(_chart_payload(df, chart_type, match, measure, None, title) for chart_type in types)

    if not charts and measure and cat_cols:
        title = state["rewritten_question"][:90]
        types, notes = _chart_types(df, "", explicit, cat_cols[0], measure, time_axis=False)
        warnings.extend(notes)
        charts.extend(_chart_payload(df, chart_type, cat_cols[0], measure, None, title) for chart_type in types)

    return charts, warnings


def chart_agent_node(state: AgentState) -> dict:
    t0 = time.time()
    warnings = list(state.get("warnings", []))
    df = state.get("dataframe")

    if df is None or df.empty:
        warnings.append("No data was returned, so no chart could be generated.")
        return {
            "charts": [],
            "warnings": warnings,
            "timings_ms": {**state.get("timings_ms", {}), "chart_agent": round((time.time() - t0) * 1000, 1)},
        }

    try:
        if state.get("route") == "decomposition" and len(state.get("sub_questions", [])) > 1:
            charts, chart_notes = _decomposed_charts(df, state)
        else:
            charts, chart_notes = _single_charts(df, state)
            if not charts:
                chart_notes.append("Data did not fit a standard chart shape; showing table view only.")
        warnings.extend(chart_notes)
    except Exception as exc:  # noqa: BLE001
        logger.error("Chart generation failed: %s", exc, extra={"request_id": state.get("request_id")})
        charts = []
        warnings.append("Chart generation failed; showing insight and table only.")

    return {
        "charts": charts,
        "chart_type": charts[0]["chart_type"] if charts else "table",
        "warnings": warnings,
        "timings_ms": {**state.get("timings_ms", {}), "chart_agent": round((time.time() - t0) * 1000, 1)},
    }
