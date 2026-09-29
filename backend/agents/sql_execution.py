"""Agent 4 - SQL Execution.

Runs the validated SQL against whichever data source is active (SQL Server via pyodbc, or
the local .xlsx via DuckDB - see backend/core/datasource.py) and loads the result into a
pandas DataFrame for the downstream Calculation/Chart/Insight agents. The agent itself is
dialect-agnostic: it always hands over T-SQL, and the local backend translates.
"""
import re
import time

from backend.agents.state import AgentState
from backend.core.datasource import DatabaseError, get_datasource
from backend.core.logging_config import get_logger

logger = get_logger(__name__)

_CALC_KEYWORDS = re.compile(
    r"\bgrowth\b|\bratio\b|% change|\bchange\b|\bincrease\b|\bdecrease\b|\baverage\b|"
    r"\byoy\b|\byear over year\b|compared to|\bdelta\b",
    re.IGNORECASE,
)


def sql_execution_node(state: AgentState) -> dict:
    t0 = time.time()
    warnings = list(state.get("warnings", []))

    def _timings() -> dict:
        return {**state.get("timings_ms", {}), "sql_execution": round((time.time() - t0) * 1000, 1)}

    try:
        result = get_datasource().run_select(state["sql_query"])
    except DatabaseError as exc:
        logger.warning("SQL execution failed: %s", exc, extra={"request_id": state.get("request_id")})
        return {"sql_error": str(exc), "dataframe": None, "timings_ms": _timings()}
    except Exception as exc:  # noqa: BLE001 - an unexpected driver error must not crash the graph
        logger.error(
            "Unexpected data-source error: %s", exc, exc_info=True,
            extra={"request_id": state.get("request_id")},
        )
        return {
            "sql_error": f"The data source raised an unexpected error: {exc}",
            "dataframe": None,
            "timings_ms": _timings(),
        }

    if result.truncated:
        warnings.append(f"Result set exceeded the row cap and was truncated to {result.row_count} rows.")

    if result.row_count == 0:
        warnings.append("The query returned no rows for the given filters/date range.")

    needs_calculation = bool(_CALC_KEYWORDS.search(state["rewritten_question"]))
    # A single-row scalar result is already the answer; running a code-generating
    # calculation step over it only adds latency and a failure mode.
    if result.row_count <= 1:
        needs_calculation = False

    return {
        "dataframe": result.dataframe,
        "row_count": result.row_count,
        "truncated": result.truncated,
        "sql_error": None,
        "needs_calculation": needs_calculation,
        "warnings": warnings,
        "timings_ms": _timings(),
    }
