"""Agent 5 - Calculation (optional/conditional node).

For questions implying a derived metric (growth %, YoY, ratio, average-of-average) that a
single SQL aggregation can't express cleanly, Qwen2.5-Coder writes a short pandas/numpy/
scipy snippet against the already-fetched DataFrame, executed in backend/agents/
calc_sandbox.py. This mirrors the "code generation beats a bigger LLM for arithmetic"
approach: failures here degrade to "no extra calculation", never to a broken pipeline.
"""
import time

from backend.agents.calc_sandbox import run_calculation
from backend.agents.state import AgentState
from backend.core.llm_client import LLMUnavailableError, chat
from backend.core.logging_config import get_logger
from config.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()

_SYSTEM_PROMPT = """You write short Python snippets for a pandas/numpy/scipy sandbox.
A DataFrame `df` is already loaded with the SQL query result (columns and a sample are
given below). Write code that computes what the question asks (e.g. growth %, ratio,
average) and assigns the final answer to a variable named `result` (a plain number,
string, or small dict of numbers - JSON serializable).
Rules: use only pd, np, stats and df. No imports, no file/network access, no printing.
Output ONLY the Python code, no markdown fences, no explanation."""


def calculation_agent_node(state: AgentState) -> dict:
    t0 = time.time()
    warnings = list(state.get("warnings", []))
    df = state.get("dataframe")

    if df is None or df.empty:
        return {"timings_ms": {**state.get("timings_ms", {}), "calculation": 0.0}}

    columns_info = ", ".join(f"{c} ({df[c].dtype})" for c in df.columns)
    sample = df.head(5).to_dict(orient="records")
    prompt = (
        f"Question: {state['rewritten_question']}\n"
        f"DataFrame columns: {columns_info}\n"
        f"Sample rows: {sample}\n"
        "Code:"
    )

    try:
        # Arithmetic must be reproducible, so this always runs fully deterministic.
        code = chat(
            settings.sql_model, _SYSTEM_PROMPT, prompt,
            temperature=settings.LLM_NUMERIC_TEMPERATURE,
        )
    except LLMUnavailableError as exc:
        warnings.append(f"Calculation step skipped (LLM unavailable): {exc}")
        return {
            "needs_calculation": False,
            "warnings": warnings,
            "timings_ms": {**state.get("timings_ms", {}), "calculation": round((time.time() - t0) * 1000, 1)},
        }

    code = code.strip().strip("`")
    if code.lower().startswith("python"):
        code = code[6:].strip()

    outcome = run_calculation(code, {"df": df})
    if not outcome["ok"]:
        warnings.append(f"Calculation step skipped: {outcome['error']}")
        return {
            "needs_calculation": False,
            "warnings": warnings,
            "timings_ms": {**state.get("timings_ms", {}), "calculation": round((time.time() - t0) * 1000, 1)},
        }

    logger.info("calculation_agent result=%s", outcome["result"], extra={"request_id": state.get("request_id")})
    return {
        "calculation_summary": str(outcome["result"]),
        "warnings": warnings,
        "timings_ms": {**state.get("timings_ms", {}), "calculation": round((time.time() - t0) * 1000, 1)},
    }
