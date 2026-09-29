"""LangGraph wiring for the multi-agent pipeline.

    query_understanding -> schema_retrieval -> section_start
                                                    |
        +-------------------------------------------+
        v
    sql_generation <-> sql_execution -> [calculation] -> chart_agent -> insight_agent
        |                    |                                              |
        +-> section_failed <-+                                              v
                  |                                                    section_end
                  +---------------> more sub-questions? -> section_start     |
                                    otherwise -> finalize <------------------+

A compound question ("compare zone contribution AND identify top intermediaries") is
decomposed into sub-questions, and the loop runs the SQL -> chart -> insight pipeline
ONCE PER SUB-QUESTION. Each part therefore gets its own query, its own chart and its own
insight, rather than one query being sliced several ways.

Retrieval runs once for the whole question: it already widens its column search across
every sub-question, so repeating it per section would only add latency.

Failure is per-section. A part that cannot be answered after MAX_SQL_ATTEMPTS is recorded
with its error and the loop moves on, so one bad part never discards the parts that
worked. `finalize` reports "ok" when every part succeeded and "partial" when some did not.

Both section_end and section_failed advance section_index, which is what guarantees the
loop terminates. MAX_SECTIONS bounds total latency on a CPU-only host.

Any node raising an unexpected exception is caught by `_safe` and turned into a graceful
section failure instead of crashing the request.
"""
import traceback
from typing import Callable

from langgraph.graph import END, StateGraph

from backend.agents.calculation_agent import calculation_agent_node
from backend.agents.chart_agent import chart_agent_node
from backend.agents.insight_agent import insight_agent_node
from backend.agents.query_understanding import query_understanding_node
from backend.agents.schema_retrieval import schema_retrieval_node
from backend.agents.sql_execution import sql_execution_node
from backend.agents.sql_generation import sql_generation_node
from backend.agents.state import AgentState
from backend.core.logging_config import get_logger

logger = get_logger(__name__)

MAX_SQL_ATTEMPTS = 3

# Each section is a full SQL-generation round trip, which on a CPU-only host costs
# 30-150s. Three is the most that fits inside REQUEST_TIMEOUT_SECONDS with retries; extra
# sub-questions are dropped with a warning rather than timing the whole answer out.
MAX_SECTIONS = 3


def _safe(name: str, fn: Callable[[AgentState], dict]) -> Callable[[AgentState], dict]:
    def wrapped(state: AgentState) -> dict:
        try:
            return fn(state)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Unhandled exception in node '%s': %s\n%s",
                name, exc, traceback.format_exc(),
                extra={"request_id": state.get("request_id")},
            )
            return {
                "status": "error",
                "error_message": f"Internal error in {name}. Please refresh the page and try again.",
            }
    return wrapped


def _pending_questions(state: AgentState) -> list[str]:
    questions = state.get("sub_questions") or []
    return questions[:MAX_SECTIONS] or [state.get("rewritten_question", "")]


def _section_start(state: AgentState) -> dict:
    """Begin one sub-question with a clean slate.

    `rewritten_question` is repointed at the current sub-question so every downstream
    agent - SQL generation, chart titles, insight statistics - works on that part alone
    and needs no knowledge of the loop.
    """
    questions = _pending_questions(state)
    index = state.get("section_index", 0)
    return {
        "rewritten_question": questions[index],
        # Per-section state must not leak from the previous part.
        "sql_query": "",
        "sql_error": None,
        "sql_attempts": 0,
        "dataframe": None,
        "charts": [],
        "insight": "",
        "answer_mode": "",
        "needs_calculation": False,
        "row_count": 0,
        "truncated": False,
        "status": None,
        "error_message": None,
    }


def _section_end(state: AgentState) -> dict:
    sections = list(state.get("sections", []))
    sections.append(
        {
            "question": state.get("rewritten_question", ""),
            "sql": state.get("sql_query", ""),
            "insight": state.get("insight", ""),
            "charts": state.get("charts", []),
            "row_count": state.get("row_count", 0),
            "answer_mode": state.get("answer_mode", ""),
            "dataframe": state.get("dataframe"),
            "error": None,
        }
    )
    return {"sections": sections, "section_index": state.get("section_index", 0) + 1}


def _section_failed(state: AgentState) -> dict:
    """Record this part as failed and carry on to the next one.

    One unanswerable part of a compound question must not discard the parts that did
    work - that was the whole point of running them separately.
    """
    message = (
        state.get("sql_error")
        or state.get("error_message")
        or "This part of the question could not be answered."
    )
    logger.warning(
        "Section %s failed: %s", state.get("section_index", 0), message,
        extra={"request_id": state.get("request_id")},
    )
    sections = list(state.get("sections", []))
    sections.append(
        {
            "question": state.get("rewritten_question", ""),
            "sql": state.get("sql_query", ""),
            "insight": "",
            "charts": [],
            "row_count": 0,
            "answer_mode": "error",
            "dataframe": None,
            "error": message,
        }
    )
    return {
        "sections": sections,
        "section_index": state.get("section_index", 0) + 1,
        # Cleared so the next section is not judged by this one's failure.
        "sql_error": None,
        "status": None,
        "error_message": None,
    }


def _finalize(state: AgentState) -> dict:
    """Collapse the sections into the single-answer shape the API already returns."""
    sections = state.get("sections", [])
    answered = [s for s in sections if not s.get("error")]
    warnings = list(state.get("warnings", []))

    if not answered:
        first_error = sections[0]["error"] if sections else None
        return {
            "status": "error",
            "error_message": first_error or "Something went wrong. Please refresh the page.",
            "sections": sections,
        }

    for failed in (s for s in sections if s.get("error")):
        warnings.append(f"Could not answer \"{failed['question']}\": {failed['error']}")

    # Top-level fields stay populated so any client that does not understand sections
    # still gets a complete answer.
    combined_insight = "\n\n".join(
        (f"**{s['question']}**\n{s['insight']}" if len(answered) > 1 else s["insight"])
        for s in answered
    )
    return {
        "status": "ok" if len(answered) == len(sections) else "partial",
        "sections": sections,
        "insight": combined_insight,
        "charts": [chart for s in answered for chart in s["charts"]],
        "sql_query": "\n\n".join(s["sql"] for s in answered if s["sql"]),
        "dataframe": answered[0]["dataframe"],
        "row_count": answered[0]["row_count"],
        "answer_mode": answered[0]["answer_mode"],
        "warnings": warnings,
    }


def _route_after_sql_generation(state: AgentState) -> str:
    if state.get("status") == "error":
        return "section_failed"
    if state.get("sql_error"):
        if state.get("sql_attempts", 0) >= MAX_SQL_ATTEMPTS:
            return "section_failed"
        return "sql_generation"
    return "sql_execution"


def _route_after_sql_execution(state: AgentState) -> str:
    if state.get("status") == "error":
        return "section_failed"
    if state.get("sql_error"):
        if state.get("sql_attempts", 0) >= MAX_SQL_ATTEMPTS:
            return "section_failed"
        return "sql_generation"
    if state.get("needs_calculation"):
        return "calculation_agent"
    return "chart_agent"


def _route_next_section(state: AgentState) -> str:
    if state.get("section_index", 0) < len(_pending_questions(state)):
        return "section_start"
    return "finalize"


def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("query_understanding", _safe("query_understanding", query_understanding_node))
    graph.add_node("schema_retrieval", _safe("schema_retrieval", schema_retrieval_node))
    graph.add_node("section_start", _section_start)
    graph.add_node("sql_generation", _safe("sql_generation", sql_generation_node))
    graph.add_node("sql_execution", _safe("sql_execution", sql_execution_node))
    graph.add_node("calculation_agent", _safe("calculation_agent", calculation_agent_node))
    graph.add_node("chart_agent", _safe("chart_agent", chart_agent_node))
    graph.add_node("insight_agent", _safe("insight_agent", insight_agent_node))
    graph.add_node("section_end", _section_end)
    graph.add_node("section_failed", _section_failed)
    graph.add_node("finalize", _finalize)

    graph.set_entry_point("query_understanding")
    graph.add_edge("query_understanding", "schema_retrieval")
    # Retrieval runs ONCE for the whole question - it already widens its search across
    # every sub-question, so re-running it per section would only cost time.
    graph.add_edge("schema_retrieval", "section_start")
    graph.add_edge("section_start", "sql_generation")

    graph.add_conditional_edges(
        "sql_generation", _route_after_sql_generation,
        {"sql_generation": "sql_generation", "sql_execution": "sql_execution",
         "section_failed": "section_failed"},
    )
    graph.add_conditional_edges(
        "sql_execution", _route_after_sql_execution,
        {"sql_generation": "sql_generation", "calculation_agent": "calculation_agent",
         "chart_agent": "chart_agent", "section_failed": "section_failed"},
    )

    graph.add_edge("calculation_agent", "chart_agent")
    graph.add_edge("chart_agent", "insight_agent")
    graph.add_edge("insight_agent", "section_end")

    # Both outcomes advance section_index, so the loop always terminates.
    for node in ("section_end", "section_failed"):
        graph.add_conditional_edges(
            node, _route_next_section,
            {"section_start": "section_start", "finalize": "finalize"},
        )

    graph.add_edge("finalize", END)

    return graph.compile()


_compiled_graph = None


def get_graph():
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_graph()
    return _compiled_graph
