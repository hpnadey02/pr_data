"""Agent 3 - SQL Generation (Qwen2.5-Coder via Ollama).

Constrained, retrieval-grounded T-SQL generation: the model only ever sees the retrieved
columns for the configured table (never all 124), plus similar example questions as
few-shot guidance, plus - on retry - its own previous SQL and the validator's error so it
can self-correct.

Three fixes vs. the original implementation, all driven by logs/app.log:

  * The system prompt used to hard-code example column names with SPACES
    (`SUM([GROSS PREMIUM])`, `[POLICY ISSUE DATE]`). The real columns are GROSS_PREMIUM
    and POLICY_ISSUE_DATE, so the model copied the prompt and every query died with
    "Invalid column name 'GROSS PREMIUM'". Examples are now generated from the LIVE
    schema, so the prompt can never disagree with the database.

  * Generated identifiers are repaired against the column registry before execution
    (backend/agents/column_guard.py), so a spelling-format slip is corrected instead of
    burning all three retries.

  * Retries no longer re-ask an identical question at temperature 0, which is why the
    model previously returned byte-identical SQL three times. Each attempt raises the
    temperature slightly and states explicitly which identifiers were rejected.
"""
import time

from backend.agents.column_guard import ColumnValidationError, validate_and_repair
from backend.agents.sql_guard import SQLValidationError, validate_sql
from backend.agents.state import AgentState
from backend.core.column_registry import get_registry
from backend.core.llm_client import LLMUnavailableError, chat
from backend.core.logging_config import get_logger
from config.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()

# Temperature per attempt. Attempt 1 is deterministic (the SQL requirement); later
# attempts add just enough variation to escape a wrong answer the model is locked onto.
_RETRY_TEMPERATURES = (0.0, 0.15, 0.3)

_SYSTEM_PROMPT = """You are a Microsoft SQL Server (T-SQL) expert generating a single \
read-only SELECT query against exactly one table: {table}.

CRITICAL - column names:
- Use ONLY the exact column names listed below. Copy them character for character.
- These names use UNDERSCORES, never spaces. Writing [GROSS PREMIUM] instead of
  [GROSS_PREMIUM] is an error and the query will fail.
- Never invent a column or table name. If the question asks for something that has no
  column in the list, omit it rather than guessing a name.

CRITICAL - result size. This table holds tens of millions of rows:
- NEVER write `SELECT *`. Never select every column.
- Prefer an AGGREGATE: SUM / COUNT / AVG with GROUP BY. Return summary rows, not raw rows.
- If raw records genuinely are the answer (a single-policy lookup), select only the
  columns asked for and cap with TOP 100.
- Every query MUST start with `SELECT TOP n` - the row cap is not optional.

Rules:
- Always wrap column names in square brackets, e.g. [BRANCH_NAME].
- Always use TOP N to cap result size, and place it immediately after SELECT
  (`SELECT TOP 10 ...`). Never write TOP at the end of the query.
- Default to TOP 200 when the question implies no explicit limit.
- When the question gives an identifier value, filter on the column that matches that
  identifier's own name - do not substitute a different, similar-looking column.
- Each column below lists what the business calls it ("also called") and what it means.
  Match the question's words to that list: "coverage amount" is whichever column names it
  as an alias, NOT whichever column name merely looks similar.
{measure_hint}{date_hint}
- For ranking ("top N", "highest", "high-performing") use ORDER BY the measure DESC and TOP N.
- For "contribution"/"share"/"percentage" questions, also compute the share of total using
  a window function, e.g. SUM({measure}) * 100.0 / SUM(SUM({measure})) OVER () AS pct_share
- If the question asks for actual vs target and no target/budget column appears in the
  column list below, do NOT invent one - select only the actual measure.
- Output ONLY the raw SQL. No markdown fences, no explanation, no trailing semicolon.

Available columns (name | category | also called | meaning):
{columns}

Similar example questions for style guidance:
{examples}
"""


def _format_columns(columns: list[dict]) -> str:
    """One line per retrieved column: name, category, what the business calls it, meaning.

    The aliases matter as much as the description. Two columns can be described almost
    identically ("USGI's share of gross premium" vs "gross written premium"); what actually
    separates them is which words the business uses for each.
    """
    if not columns:
        return (
            "(none retrieved - answer with a count only: "
            "SELECT TOP 1 COUNT(*) AS row_count FROM the table)"
        )
    lines = []
    for c in columns:
        aliases = [str(a) for a in (c.get("aliases") or []) if str(a).strip()]
        also = f" | also called: {', '.join(aliases)}" if aliases else ""
        lines.append(
            f"- [{c['column']}] | {c.get('category', 'n/a')}{also} | {c.get('description', '')}"
        )
    return "\n".join(lines)


def _format_examples(examples: list[dict]) -> str:
    if not examples:
        return "(none)"
    lines = []
    for ex in examples:
        meta = ex.get("metadata", {})
        lines.append(
            f"- \"{ex['question']}\" -> route={meta.get('route')}, chart={meta.get('chart_type')}"
        )
    return "\n".join(lines)


def _schema_hints() -> tuple[str, str, str]:
    """Build the measure/date guidance from the LIVE schema.

    Returning the real physical names here is what stops the prompt from teaching the
    model a column name that does not exist.
    """
    registry = get_registry()
    measure = None
    for candidate in ("GROSS_PREMIUM", "USGI_GROSS_PREMIUM", "NET_PREMIUM"):
        resolved = registry.resolve(candidate, allow_fuzzy=False)
        if resolved:
            measure = resolved
            break
    if not measure:
        measures = registry.measures()
        measure = measures[0] if measures else None

    date_column = None
    for candidate in ("POLICY_ISSUE_DATE", "REFERENCE_DATE", "START_DATE"):
        resolved = registry.resolve(candidate, allow_fuzzy=False)
        if resolved:
            date_column = resolved
            break
    if not date_column:
        dates = registry.dates()
        date_column = dates[0] if dates else None

    measure_expr = f"[{measure}]" if measure else "the numeric measure column"
    measure_hint = (
        f"- Default measure for \"business\"/\"sales\"/\"performance\" questions is "
        f"SUM([{measure}]).\n"
        if measure
        else ""
    )
    date_hint = (
        f"- For \"week wise\"/\"weekly\" trends bucket by week start:\n"
        f"  CAST(DATEADD(day, -(DATEPART(weekday, [{date_column}]) - 1), [{date_column}]) AS date) AS week_start\n"
        f"- For \"monthly\" trends bucket with: FORMAT([{date_column}], 'yyyy-MM') AS month\n"
        if date_column
        else ""
    )
    return measure_hint, date_hint, measure_expr


_ROUTE_HINTS = {
    "ranking_then_trend": (
        "First identify the top N entities by total measure (CTE/subquery), then return "
        "their week-bucketed trend, joined back to only those entities, ordered by entity "
        "then week."
    ),
    "top1_then_trend": (
        "First identify the single top entity by total measure, then return its "
        "week-bucketed trend for that entity only, ordered by week."
    ),
    "decomposition": (
        "The question has multiple parts - write ONE query that returns enough grouped "
        "columns (e.g. both the zone/state and the intermediary, or both dimensions asked "
        "about) so the results can be sliced into separate charts downstream."
    ),
    "actual_vs_target": (
        "Select the actual measure grouped by the relevant dimension. Only include a "
        "target/budget column if one exists in the column list above."
    ),
    "lookup": (
        "This is a single-record lookup. Filter on EVERY identifier the question supplies, "
        "combined with AND, and select only the column(s) actually asked for."
    ),
}


def _build_prompt(state: AgentState, attempt: int) -> str:
    prompt = f"Question: {state['rewritten_question']}\n"

    filters = state.get("filters") or {}
    if filters.get("top_n"):
        prompt += f"Limit results to top {filters['top_n']}.\n"
    if filters.get("date_grain"):
        prompt += f"Trend/date grain requested: {filters['date_grain']}.\n"

    # Identifier filters resolved deterministically from the question text. Stating the
    # exact column keeps the model from filtering POLICY_NO on USGIpos_Policy_Number.
    equality = state.get("equality_filters") or {}
    if equality:
        pairs = "\n".join(f"  [{column}] = '{value}'" for column, value in equality.items())
        prompt += (
            "The question supplies these exact filters. Use every one of them, on exactly "
            f"these columns, combined with AND:\n{pairs}\n"
        )

    requested = state.get("requested_columns") or []
    if requested:
        prompt += (
            "The question asks for these column(s) specifically - select them and nothing "
            f"else: {', '.join(f'[{c}]' for c in requested)}\n"
        )

    # Already resolved to literal dates by backend/core/date_windows.py. Stating the range
    # beats asking a 7B model to derive "the 2nd week of the latest month in the data".
    date_window = state.get("date_window")
    if date_window is not None:
        prompt += date_window.as_sql_instruction()

    hint = _ROUTE_HINTS.get(state.get("route", ""))
    if hint:
        prompt += f"Routing hint: {hint}\n"

    if state.get("sql_error"):
        prompt += (
            f"\nAttempt {attempt - 1} FAILED. Your previous SQL was:\n"
            f"{state.get('sql_query', '')}\n"
            f"The error was: {state['sql_error']}\n"
            "Write DIFFERENT SQL that fixes this. Re-check every column name against the "
            "list above, character for character.\n"
        )

    prompt += "\nSQL:"
    return prompt


def sql_generation_node(state: AgentState) -> dict:
    t0 = time.time()
    attempts = state.get("sql_attempts", 0) + 1
    warnings = list(state.get("warnings", []))

    measure_hint, date_hint, measure_expr = _schema_hints()
    system = _SYSTEM_PROMPT.format(
        table=settings.DB_TABLE,
        columns=_format_columns(state.get("retrieved_columns", [])),
        examples=_format_examples(state.get("retrieved_examples", [])),
        measure_hint=measure_hint,
        date_hint=date_hint,
        measure=measure_expr,
    )
    prompt = _build_prompt(state, attempts)

    temperature = _RETRY_TEMPERATURES[min(attempts - 1, len(_RETRY_TEMPERATURES) - 1)]
    if temperature == 0.0:
        temperature = settings.LLM_SQL_TEMPERATURE

    def _timings() -> dict:
        return {
            **state.get("timings_ms", {}),
            "sql_generation": round((time.time() - t0) * 1000, 1),
        }

    try:
        # A different seed per attempt guarantees the retry is not byte-identical.
        raw_sql = chat(
            settings.sql_model, system, prompt,
            temperature=temperature, seed=42 + attempts,
        )
    except LLMUnavailableError as exc:
        logger.error(
            "sql_generation LLM failure (attempt %s): %s", attempts, exc,
            extra={"request_id": state.get("request_id")},
        )
        return {
            "status": "error",
            "error_message": str(exc),
            "sql_attempts": attempts,
            "warnings": warnings,
            "timings_ms": _timings(),
        }

    try:
        sanitized = validate_sql(raw_sql)
    except SQLValidationError as exc:
        logger.warning(
            "SQL validation failed (attempt %s): %s | raw=%s", attempts, exc, raw_sql,
            extra={"request_id": state.get("request_id")},
        )
        return {
            "sql_query": raw_sql,
            "sql_error": str(exc),
            "sql_attempts": attempts,
            "warnings": warnings,
            "timings_ms": _timings(),
        }

    try:
        repaired, repairs = validate_and_repair(
            sanitized, table_name=settings.DB_TABLE
        )
    except ColumnValidationError as exc:
        logger.warning(
            "Column validation failed (attempt %s): %s | sql=%s", attempts, exc, sanitized,
            extra={"request_id": state.get("request_id")},
        )
        return {
            "sql_query": sanitized,
            "sql_error": str(exc),
            "sql_attempts": attempts,
            "warnings": warnings,
            "timings_ms": _timings(),
        }

    if repairs:
        warnings.append(
            "Corrected column name(s) in the generated SQL: " + "; ".join(repairs)
        )

    logger.info(
        "sql_generation success attempt=%s temp=%s sql=%s", attempts, temperature, repaired,
        extra={"request_id": state.get("request_id")},
    )
    return {
        "sql_query": repaired,
        "sql_error": None,
        "sql_attempts": attempts,
        "warnings": warnings,
        "timings_ms": _timings(),
    }
