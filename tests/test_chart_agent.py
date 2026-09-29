"""Chart generation rules.

Only three chart types are permitted: bar, pie, donut. Line and scatter were removed
deliberately - every question this app answers compares magnitudes across categories or
periods, and a bar chart reads those correctly in every case.

No LLM is involved: chart-type selection is rule-based, so these run anywhere.
"""
import json

import pandas as pd
import pytest

from backend.agents.chart_agent import ALLOWED_CHART_TYPES, chart_agent_node
from config.settings import Settings


@pytest.fixture(autouse=True)
def _no_live_registry(monkeypatch):
    monkeypatch.setattr("backend.agents.df_utils._registry_category", lambda name: None)


@pytest.fixture(autouse=True)
def _auto_chart_type(monkeypatch):
    """Whatever CHART_TYPE this machine's .env holds, these tests start from auto."""
    monkeypatch.setattr("backend.agents.chart_agent._configured_chart_types", lambda: ())


def _configure(monkeypatch, *types):
    monkeypatch.setattr("backend.agents.chart_agent._configured_chart_types", lambda: types)


def _state(frame, question="Branch wise business", route="ranking", explicit=None):
    return {
        "request_id": "test",
        "dataframe": frame,
        "rewritten_question": question,
        "route": route,
        "filters": {"explicit_chart_type": explicit} if explicit else {},
        "sub_questions": [question],
        "retrieved_columns": [],
        "warnings": [],
        "timings_ms": {},
    }


def _categorical_frame():
    return pd.DataFrame(
        {
            "BRANCH_NAME": ["MUMBAI", "DELHI", "PUNE"],
            "total_gross_premium": [69205972.68, 41003311.10, 18770145.05],
        }
    )


def _period_frame():
    return pd.DataFrame(
        {
            "week_start": pd.to_datetime(["2026-09-15", "2026-09-01", "2026-09-08"]),
            "total_gross_premium": [30.0, 10.0, 20.0],
        }
    )


def _chart(state) -> dict:
    result = chart_agent_node(state)
    assert result["charts"], f"expected a chart, got warnings: {result.get('warnings')}"
    return result["charts"][0]


# --------------------------------------------------------------------------------------
# Only bar / pie / donut may ever be produced
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "frame, route",
    [
        (_categorical_frame(), "ranking"),
        (_categorical_frame(), "aggregation"),
        (_categorical_frame(), "comparison"),
        (_period_frame(), "trend"),
        (_period_frame(), "ranking_then_trend"),
    ],
)
def test_only_permitted_chart_types_are_produced(frame, route):
    assert _chart(_state(frame, route=route))["chart_type"] in ALLOWED_CHART_TYPES


def test_a_time_axis_is_a_bar_chart_not_a_line():
    """Line was removed; a period axis is drawn as ordered bars."""
    assert _chart(_state(_period_frame(), route="trend"))["chart_type"] == "bar"


def test_contribution_uses_a_donut():
    chart = _chart(_state(_categorical_frame(), route="contribution"))
    assert chart["chart_type"] == "donut"


@pytest.mark.parametrize("requested", ["line", "scatter", "area"])
def test_a_removed_type_is_honoured_as_a_bar(requested):
    """Answer the comparison the user asked for rather than refusing the request."""
    chart = _chart(_state(_categorical_frame(), explicit=requested))
    assert chart["chart_type"] == "bar"


@pytest.mark.parametrize("requested", ["bar", "pie", "donut"])
def test_an_explicit_permitted_type_is_respected(requested):
    assert _chart(_state(_categorical_frame(), explicit=requested))["chart_type"] == requested


# --------------------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------------------

def test_a_period_axis_stays_in_chronological_order():
    """Ranking a time axis by magnitude would misrepresent the trend."""
    figure = json.loads(_chart(_state(_period_frame(), route="trend"))["figure_json"])
    x_values = list(figure["data"][0]["x"])
    assert x_values == sorted(x_values)


def test_a_category_axis_is_ranked_by_magnitude():
    figure = json.loads(_chart(_state(_categorical_frame()))["figure_json"])
    y_values = list(figure["data"][0]["y"])
    assert y_values == sorted(y_values, reverse=True)


# --------------------------------------------------------------------------------------
# Honest refusal
# --------------------------------------------------------------------------------------

def test_no_measure_means_no_chart():
    """Every numeric column is an identifier - charting a policy number would be wrong."""
    frame = pd.DataFrame({"POLICY_NO": [1029156133, 1029156134]})
    result = chart_agent_node(_state(frame))
    assert result["charts"] == []
    assert result["chart_type"] == "table"


def test_empty_result_produces_a_warning_not_a_crash():
    result = chart_agent_node(_state(pd.DataFrame()))
    assert result["charts"] == []
    assert any("no chart" in w.lower() for w in result["warnings"])


# --------------------------------------------------------------------------------------
# The payload must load in any plotly version
# --------------------------------------------------------------------------------------

def test_figure_json_carries_no_plotly_template():
    """The template lists every trace type the writing plotly knows - including
    `heatmapgl`, which plotly 6 rejects - so a chart written by 5.x failed to render."""
    figure = json.loads(_chart(_state(_categorical_frame()))["figure_json"])
    assert "template" not in figure["layout"]
    assert "heatmapgl" not in _chart(_state(_categorical_frame()))["figure_json"]


def test_a_null_category_stays_on_the_chart():
    """Plotly drops a null-labelled bar, and the NULL group is often the largest."""
    frame = pd.DataFrame({"BRANCH_NAME": [None, "MUMBAI"], "total_gross_premium": [90.0, 10.0]})
    figure = json.loads(_chart(_state(frame))["figure_json"])
    assert None not in figure["data"][0]["x"]
    assert "not available" in figure["data"][0]["x"]


# --------------------------------------------------------------------------------------
# CHART_TYPE in .env
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("configured", ["bar", "pie", "donut"])
def test_a_single_configured_type_is_always_used(monkeypatch, configured):
    _configure(monkeypatch, configured)
    result = chart_agent_node(_state(_categorical_frame(), route="ranking"))
    assert [c["chart_type"] for c in result["charts"]] == [configured]
    assert result["chart_type"] == configured


def test_several_configured_types_draw_one_chart_each_in_order(monkeypatch):
    _configure(monkeypatch, "donut", "bar", "pie")
    result = chart_agent_node(_state(_categorical_frame()))
    assert [c["chart_type"] for c in result["charts"]] == ["donut", "bar", "pie"]


def test_a_type_named_in_the_question_beats_the_configured_default(monkeypatch):
    _configure(monkeypatch, "bar", "pie", "donut")
    result = chart_agent_node(_state(_categorical_frame(), explicit="pie"))
    assert [c["chart_type"] for c in result["charts"]] == ["pie"]


def test_a_configured_pie_on_a_time_axis_becomes_one_bar_with_a_warning(monkeypatch):
    _configure(monkeypatch, "bar", "pie", "donut")
    result = chart_agent_node(_state(_period_frame(), route="trend"))
    assert [c["chart_type"] for c in result["charts"]] == ["bar"]
    assert any("trend over time" in w for w in result["warnings"])


def test_negative_values_are_never_drawn_as_a_pie(monkeypatch):
    """Plotly drops negative slices silently, which would misstate every share."""
    _configure(monkeypatch, "donut")
    frame = pd.DataFrame({"BRANCH_NAME": ["A", "B"], "total_net_premium": [50.0, -5.0]})
    result = chart_agent_node(_state(frame))
    assert [c["chart_type"] for c in result["charts"]] == ["bar"]
    assert any("negative" in w for w in result["warnings"])


def test_auto_never_picks_a_donut_for_negative_values():
    frame = pd.DataFrame({"BRANCH_NAME": ["A", "B"], "total_net_premium": [50.0, -5.0]})
    result = chart_agent_node(_state(frame, route="contribution"))
    assert result["chart_type"] == "bar"
    assert not any("drawn as a bar" in w for w in result["warnings"]), "auto asked for nothing"


def test_a_large_pie_folds_the_tail_into_others_and_keeps_the_total(monkeypatch):
    _configure(monkeypatch, "pie")
    frame = pd.DataFrame({
        "BRANCH_NAME": [f"B{i:02d}" for i in range(20)],
        "total_gross_premium": [float(100 - i) for i in range(20)],
    })
    trace = json.loads(_chart(_state(frame))["figure_json"])["data"][0]
    assert len(trace["labels"]) == 8
    assert trace["labels"][-1] == "Others (13)"
    assert sum(trace["values"]) == pytest.approx(frame["total_gross_premium"].sum())


def test_decomposed_questions_use_the_configured_types(monkeypatch):
    _configure(monkeypatch, "bar", "donut")
    frame = pd.DataFrame({
        "BRANCH": ["MUMBAI", "DELHI"], "ZONE": ["WEST", "NORTH"],
        "total_gross_premium": [70.0, 30.0],
    })
    state = _state(frame, route="decomposition")
    state["sub_questions"] = ["branch performance", "zone contribution"]
    result = chart_agent_node(state)
    assert [c["chart_type"] for c in result["charts"]] == ["bar", "donut", "bar", "donut"]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", ()),
        ("auto", ()),
        ("Pie", ("pie",)),
        ("bar,pie,donut", ("bar", "pie", "donut")),
        ("all", ("bar", "pie", "donut")),
        ("bar, pie chart and donut", ("bar", "pie", "donut")),
        ("doughnut, bar, bar", ("donut", "bar")),
    ],
)
def test_chart_type_setting_is_parsed(raw, expected):
    settings = Settings(_env_file=None, DATA_SOURCE="local", LOCAL_DATA_PATH="x.csv", CHART_TYPE=raw)
    assert settings.chart_types == expected


@pytest.mark.parametrize("raw", ["line", "bar,scatter", "heatmap"])
def test_an_unsupported_chart_type_fails_fast_naming_the_value(raw):
    with pytest.raises(ValueError, match="CHART_TYPE"):
        Settings(_env_file=None, DATA_SOURCE="local", LOCAL_DATA_PATH="x.csv", CHART_TYPE=raw)
