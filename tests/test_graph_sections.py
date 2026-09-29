"""The per-sub-question section loop.

A compound question ("compare zone contribution AND identify top intermediaries") now runs
the SQL -> chart -> insight pipeline ONCE PER PART, so each part gets its own query, its
own chart and its own insight. Previously one query was sliced several ways.

Two properties matter and are locked down here:
  * every part is answered independently, in order
  * a part that fails is recorded and the loop CONTINUES - one bad part must never discard
    the parts that worked

The agent nodes are replaced with deterministic fakes, so no LLM, database or ChromaDB is
involved and the graph's own wiring is what gets tested.
"""
import pandas as pd
import pytest

from backend.agents import graph as graph_module


def _frame(value: float) -> pd.DataFrame:
    return pd.DataFrame(
        {"BRANCH_NAME": ["MUMBAI", "DELHI"], "total_gross_premium": [value, value / 2]}
    )


@pytest.fixture
def pipeline(monkeypatch):
    """Build a graph whose agents are fakes, so only the wiring is under test."""

    def build(sub_questions, failing: set[str] = frozenset()):
        monkeypatch.setattr(
            graph_module, "query_understanding_node",
            lambda state: {
                "rewritten_question": state["raw_question"],
                "sub_questions": sub_questions,
                "route": "decomposition" if len(sub_questions) > 1 else "aggregation",
                "filters": {},
            },
        )
        monkeypatch.setattr(
            graph_module, "schema_retrieval_node",
            lambda state: {"retrieved_columns": [], "retrieved_examples": []},
        )

        def fake_sql_generation(state):
            question = state["rewritten_question"]
            if question in failing:
                return {
                    "sql_error": f"no column matched '{question}'",
                    "sql_attempts": state.get("sql_attempts", 0) + 1,
                }
            return {
                "sql_query": f"SELECT TOP 5 [BRANCH_NAME] FROM t -- {question}",
                "sql_error": None,
                "sql_attempts": state.get("sql_attempts", 0) + 1,
            }

        def fake_sql_execution(state):
            index = state.get("section_index", 0)
            return {
                "dataframe": _frame(100.0 * (index + 1)),
                "row_count": 2,
                "sql_error": None,
                "needs_calculation": False,
            }

        monkeypatch.setattr(graph_module, "sql_generation_node", fake_sql_generation)
        monkeypatch.setattr(graph_module, "sql_execution_node", fake_sql_execution)
        monkeypatch.setattr(
            graph_module, "chart_agent_node",
            lambda state: {
                "charts": [{
                    "chart_type": "bar",
                    "title": state["rewritten_question"],
                    "figure_json": "{}",
                }],
                "chart_type": "bar",
            },
        )
        monkeypatch.setattr(
            graph_module, "insight_agent_node",
            lambda state: {
                "insight": f"insight for {state['rewritten_question']}",
                "answer_mode": "template",
            },
        )
        return graph_module.build_graph()

    return build


def _run(compiled, question="compound question"):
    return compiled.invoke(
        {
            "request_id": "test",
            "raw_question": question,
            "chat_history": [],
            "sql_attempts": 0,
            "sections": [],
            "section_index": 0,
            "warnings": [],
            "timings_ms": {},
        },
        {"recursion_limit": 60},
    )


# --------------------------------------------------------------------------------------
# One section per sub-question
# --------------------------------------------------------------------------------------

def test_a_simple_question_produces_exactly_one_section(pipeline):
    final = _run(pipeline(["branch wise business"]))
    assert final["status"] == "ok"
    assert len(final["sections"]) == 1
    assert final["sections"][0]["question"] == "branch wise business"


def test_each_sub_question_gets_its_own_sql_insight_and_chart(pipeline):
    parts = ["zone contribution", "top intermediaries"]
    final = _run(pipeline(parts))

    assert final["status"] == "ok"
    assert [s["question"] for s in final["sections"]] == parts
    for part, section in zip(parts, final["sections"]):
        assert part in section["sql"], "each part must run its OWN query"
        assert section["insight"] == f"insight for {part}"
        assert len(section["charts"]) == 1
        assert section["charts"][0]["title"] == part


def test_sections_are_answered_in_order(pipeline):
    parts = ["first part", "second part", "third part"]
    final = _run(pipeline(parts))
    assert [s["question"] for s in final["sections"]] == parts


def test_more_parts_than_the_cap_are_dropped_not_run(pipeline):
    """Each section is a full LLM round trip; MAX_SECTIONS bounds total latency."""
    parts = [f"part {i}" for i in range(graph_module.MAX_SECTIONS + 3)]
    final = _run(pipeline(parts))
    assert len(final["sections"]) == graph_module.MAX_SECTIONS


# --------------------------------------------------------------------------------------
# Per-section failure must not discard the parts that worked
# --------------------------------------------------------------------------------------

def test_one_failing_part_leaves_the_others_intact(pipeline):
    parts = ["good part", "bad part", "another good part"]
    final = _run(pipeline(parts, failing={"bad part"}))

    assert final["status"] == "partial"
    assert len(final["sections"]) == 3

    answered = [s for s in final["sections"] if not s["error"]]
    assert [s["question"] for s in answered] == ["good part", "another good part"]

    failed = [s for s in final["sections"] if s["error"]][0]
    assert failed["question"] == "bad part"
    assert "no column matched" in failed["error"]


def test_a_failure_is_explained_in_the_warnings(pipeline):
    final = _run(pipeline(["good part", "bad part"], failing={"bad part"}))
    assert any("bad part" in w for w in final["warnings"])


def test_every_part_failing_is_reported_as_an_error(pipeline):
    final = _run(pipeline(["a", "b"], failing={"a", "b"}))
    assert final["status"] == "error"
    assert final["error_message"]


def test_a_failure_does_not_leak_into_the_next_section(pipeline):
    """section_start must reset sql_error, or the next part inherits the verdict."""
    final = _run(pipeline(["bad part", "good part"], failing={"bad part"}))
    good = [s for s in final["sections"] if s["question"] == "good part"][0]
    assert good["error"] is None
    assert good["insight"] == "insight for good part"


def test_retries_are_counted_per_section_not_across_the_whole_question(pipeline):
    """A part that needed retries must not exhaust the budget for later parts."""
    final = _run(pipeline(["bad part", "good part", "another good"], failing={"bad part"}))
    answered = [s for s in final["sections"] if not s["error"]]
    assert len(answered) == 2


# --------------------------------------------------------------------------------------
# The combined view older clients still read
# --------------------------------------------------------------------------------------

def test_flat_fields_combine_every_answered_section(pipeline):
    parts = ["zone contribution", "top intermediaries"]
    final = _run(pipeline(parts))

    assert len(final["charts"]) == 2, "flat charts must include every section's chart"
    for part in parts:
        assert part in final["insight"]
        assert part in final["sql_query"]


def test_a_single_section_is_not_given_a_heading(pipeline):
    """One part needs no '**question**' prefix - that only helps when there are several."""
    final = _run(pipeline(["branch wise business"]))
    assert final["insight"] == "insight for branch wise business"
