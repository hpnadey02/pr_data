"""Agent 7 - Insight Generation.

Number-safe by construction. Three tiers, in order:

  1. DIRECT - the SQL result is a single row of scalars, so the result IS the answer. It
     is formatted straight from the DataFrame with no model involved. This is the tier
     that fixes the reported defect where a query returning 660000 was narrated as
     "$1,200,000": there is now no step in which a number can be re-typed by a model.

  2. VERIFIED NARRATIVE - for many-row results a narrative genuinely helps, so the model
     writes one from a precomputed statistics dict. Every figure it emits is then checked
     against the values it was given; an unsupported figure, or any invented currency
     symbol, rejects the narrative outright.

  3. TEMPLATE - the deterministic fallback used when the model is unavailable or its
     narrative fails verification.

Temperature follows the same rule as the rest of the app: anything restating a number runs
at LLM_NUMERIC_TEMPERATURE (0.0); only the free-prose tier uses LLM_TEXT_TEMPERATURE.
"""
import json
import time

import pandas as pd

from backend.agents.answer_builder import (
    build_direct_answer,
    collect_allowed_numbers,
    format_value,
    is_direct_answer,
    verify_narrative,
)
from backend.agents.df_utils import categorical_columns, date_like_columns, numeric_columns, pick_measure
from backend.agents.state import AgentState
from backend.core.identifiers import readable_inline
from backend.core.llm_client import LLMUnavailableError, chat
from backend.core.logging_config import get_logger
from config.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()

_SYSTEM_PROMPT = """You are a business analyst summarizing insurance business data for a
non-technical stakeholder. You will be given ONLY a set of precomputed statistics.

ABSOLUTE RULES:
- Use EXCLUSIVELY the numbers given to you. Copy them digit for digit.
- Never estimate, round differently, rescale, or invent any figure.
- Never write a currency symbol or unit: no $, no INR, no Rs, no rupees, no dollars.
  The data carries no currency. Write "69,205,972.68", not "$69,205,972.68".
- If a "target_available: false" note appears, state that target data was not available
  instead of guessing a target.

Write 3 to 6 concise bullet points in plain business language answering the user's
question. Output plain text bullets starting with "- ", no markdown headers, no code."""


def compute_stats(df: pd.DataFrame, question: str) -> dict:
    numeric_cols = numeric_columns(df)
    date_cols = date_like_columns(df)
    cat_cols = categorical_columns(df, exclude=set(date_cols))
    measure = pick_measure(df, numeric_cols)

    stats: dict = {"row_count": len(df), "question": question}
    if measure:
        stats["measure_column"] = measure
        stats["total"] = round(float(df[measure].sum()), 2)
        stats["average"] = round(float(df[measure].mean()), 2)

    if measure and cat_cols:
        grouped = df.groupby(cat_cols[0])[measure].sum().sort_values(ascending=False)
        stats["dimension"] = cat_cols[0]
        stats["top_entities"] = [
            {"name": str(k), "value": round(float(v), 2)} for k, v in grouped.head(5).items()
        ]
        if len(grouped) > 1:
            stats["bottom_entity"] = {
                "name": str(grouped.index[-1]), "value": round(float(grouped.iloc[-1]), 2)
            }

    if measure and date_cols:
        by_date = df.groupby(date_cols[0])[measure].sum().sort_index()
        if len(by_date) >= 2:
            first, last = float(by_date.iloc[0]), float(by_date.iloc[-1])
            pct_change = round(((last - first) / first) * 100, 2) if first else None
            stats["trend"] = {
                "date_column": date_cols[0],
                "periods": len(by_date),
                "first_period_value": round(first, 2),
                "last_period_value": round(last, 2),
                "pct_change": pct_change,
                "direction": "up" if last > first else ("down" if last < first else "flat"),
            }

    return stats


def _measure_label(measure: str) -> str:
    """Label for a measure column, without stuttering.

    The SQL alias is often already "total_gross_premium", so prefixing "Total " produced
    "Total Total gross premium".
    """
    label = readable_inline(measure or "value")
    return label if label.lower().startswith(("total", "sum", "avg", "average", "count")) else f"total {label}"


def _template_fallback(stats: dict) -> str:
    lines = []
    if "total" in stats:
        measure = _measure_label(stats.get("measure_column", "value"))
        lines.append(f"- {measure[0].upper()}{measure[1:]}: {format_value(stats['total'])}")
    if stats.get("top_entities"):
        top = stats["top_entities"][0]
        lines.append(
            f"- Highest {readable_inline(stats.get('dimension', 'entity'))}: "
            f"{top['name']} ({format_value(top['value'])})"
        )
    if stats.get("bottom_entity"):
        bottom = stats["bottom_entity"]
        scope = f" of the {stats['row_count']} shown" if stats.get("row_count") else ""
        lines.append(
            f"- Lowest {readable_inline(stats.get('dimension', 'entity'))}{scope}: "
            f"{bottom['name']} ({format_value(bottom['value'])})"
        )
    if "trend" in stats:
        t = stats["trend"]
        lines.append(
            f"- Trend across {t['periods']} periods is {t['direction']}"
            + (f" ({t['pct_change']}% change)" if t["pct_change"] is not None else "")
        )
    if stats.get("target_available") is False:
        lines.append("- Target data was not available in this table, so only actuals are shown.")
    lines.append(f"- Based on {stats.get('row_count', 0)} matching record(s).")
    return "\n".join(lines)


def insight_agent_node(state: AgentState) -> dict:
    t0 = time.time()
    warnings = list(state.get("warnings", []))
    df = state.get("dataframe")

    def _timings() -> dict:
        return {**state.get("timings_ms", {}), "insight_agent": round((time.time() - t0) * 1000, 1)}

    if df is None or df.empty:
        applied = state.get("equality_filters") or {}
        message = "No matching data was found for this question."
        if applied:
            shown = ", ".join(f"{readable_inline(c)} = {v}" for c, v in applied.items())
            message += f" Filters applied: {shown}. Check those values, or broaden the filters."
        else:
            message += " Try broadening the date range or filters."
        return {"insight": message, "answer_mode": "empty", "warnings": warnings, "timings_ms": _timings()}

    # ---- Tier 1: the result IS the answer -------------------------------------------
    if is_direct_answer(df):
        insight = build_direct_answer(df)
        logger.info(
            "insight_agent direct answer (no LLM) from %s row(s) x %s column(s)",
            len(df), len(df.columns), extra={"request_id": state.get("request_id")},
        )
        return {"insight": insight, "answer_mode": "direct", "warnings": warnings, "timings_ms": _timings()}

    # ---- Tier 2/3: statistics, then a verified narrative -----------------------------
    stats = compute_stats(df, state["rewritten_question"])
    if state.get("route") == "actual_vs_target" and "target" not in " ".join(df.columns).lower():
        stats["target_available"] = False
    if state.get("calculation_summary"):
        stats["additional_calculation"] = state["calculation_summary"]

    fallback = _template_fallback(stats)
    allowed = collect_allowed_numbers(stats, df)

    # No measure means compute_stats found nothing to total, rank or trend - the result is
    # all identifiers or text. Asking the model to narrate an (almost) empty statistics
    # dict is exactly how a confident, wrong insight gets produced, so skip the LLM and
    # report what the result actually contains.
    if "total" not in stats:
        warnings.append(
            "No numeric measure was found in the query result, so the summary is "
            "descriptive only - no totals or trends could be computed."
        )
        logger.info(
            "insight_agent skipped the LLM: no measure in columns %s",
            list(df.columns), extra={"request_id": state.get("request_id")},
        )
        return {
            "insight": fallback,
            "answer_mode": "template",
            "warnings": warnings,
            "timings_ms": _timings(),
        }

    try:
        prompt = (
            f"User question: {state['rewritten_question']}\n"
            f"Statistics:\n{json.dumps(stats, indent=2, default=str)}\n\nInsight bullets:"
        )
        narrative = chat(
            settings.insight_model, _SYSTEM_PROMPT, prompt,
            temperature=settings.LLM_TEXT_TEMPERATURE,
        )
    except LLMUnavailableError as exc:
        warnings.append(f"Insight narrative generated from a template (LLM unavailable: {exc}).")
        logger.warning("insight_agent falling back to template: %s", exc,
                       extra={"request_id": state.get("request_id")})
        return {"insight": fallback, "answer_mode": "template", "warnings": warnings, "timings_ms": _timings()}

    trustworthy, reason, cleaned = verify_narrative(narrative, allowed)
    if not trustworthy:
        warnings.append(
            f"The generated narrative was rejected and replaced with figures taken "
            f"directly from the query result, because {reason}."
        )
        logger.warning(
            "insight_agent rejected narrative: %s | text=%s", reason, narrative[:300],
            extra={"request_id": state.get("request_id")},
        )
        return {"insight": fallback, "answer_mode": "template", "warnings": warnings, "timings_ms": _timings()}

    if reason == "currency-stripped":
        # Figures were right, only the unit was invented - keep the prose, drop the symbol.
        warnings.append(
            "A currency symbol was removed from the narrative: the source columns carry "
            "no currency unit."
        )

    logger.info("insight_agent generated verified insight (%s chars, %s)", len(cleaned), reason,
                extra={"request_id": state.get("request_id")})
    return {"insight": cleaned, "answer_mode": "narrative", "warnings": warnings, "timings_ms": _timings()}
