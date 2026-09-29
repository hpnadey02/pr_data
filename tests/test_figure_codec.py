"""The chart payload must survive a plotly version mismatch between backend and frontend.

Regression for: insight and SQL rendered fine but the chart failed with
"Invalid property specified for object of type plotly.graph_objs.layout.template.Data:
'heatmapgl'" - the backend wrote the figure with plotly 5.x, the Streamlit process read
it with plotly 6.x, and 6.x no longer knows `heatmapgl`.

Only one plotly is installed when these run, so the other version's output is written
out by hand below.
"""
import base64
import json

import numpy as np
import plotly.express as px
import pytest

from backend.core.figure_codec import figure_from_json, figure_to_json, portable_figure_dict


def _typed_array(values, dtype="f8") -> dict:
    """What plotly 6 writes for a numeric array."""
    return {"dtype": dtype, "bdata": base64.b64encode(np.asarray(values, dtype="<" + dtype).tobytes()).decode()}


def _bar_figure():
    return px.bar(x=["MUMBAI", "DELHI"], y=[69205972.68, 41003311.10], title="Top branches")


def test_written_payload_has_no_template_and_plain_lists():
    payload = json.loads(figure_to_json(_bar_figure()))
    assert "template" not in payload["layout"]
    assert payload["data"][0]["y"] == [69205972.68, 41003311.10]


def test_a_template_naming_a_trace_type_this_plotly_lacks_still_loads():
    """Stands in for the real failure: a template key the reading plotly does not know."""
    payload = json.loads(_bar_figure().to_json())
    payload["layout"]["template"]["data"]["heatmapgl_from_another_version"] = [{"type": "x"}]
    fig = figure_from_json(json.dumps(payload))
    assert list(fig.data[0].y) == [69205972.68, 41003311.10]


def test_an_unknown_layout_property_is_dropped_not_fatal():
    payload = json.loads(figure_to_json(_bar_figure()))
    payload["layout"]["property_from_a_future_plotly"] = 1
    fig = figure_from_json(json.dumps(payload))
    assert fig.layout.title.text == "Top branches"


@pytest.mark.parametrize("dtype", ["f8", "f4", "i4", "u1", "i2"])
def test_plotly6_typed_arrays_decode_to_plain_lists(dtype):
    figure = {"data": [{"type": "bar", "x": ["A", "B", "C"], "y": _typed_array([1, 2, 3], dtype)}],
              "layout": {}}
    assert portable_figure_dict(figure)["data"][0]["y"] == [1, 2, 3]


def test_a_2d_typed_array_keeps_its_shape():
    node = {**_typed_array([1, 2, 3, 4, 5, 6], "i4"), "shape": "2, 3"}
    assert portable_figure_dict({"data": [{"z": node}]})["data"][0]["z"] == [[1, 2, 3], [4, 5, 6]]


def test_a_plotly6_payload_renders_in_this_plotly():
    payload = {"data": [{"type": "pie", "labels": ["A", "B"], "values": _typed_array([70.0, 30.0])}],
               "layout": {"template": {"data": {"heatmapgl": [{"type": "heatmapgl"}]}}}}
    fig = figure_from_json(json.dumps(payload))
    assert list(fig.data[0].values) == [70.0, 30.0]


def test_an_ordinary_dict_that_merely_has_a_dtype_key_is_left_alone():
    figure = {"data": [{"meta": {"dtype": "f8", "note": "not an array"}}]}
    assert portable_figure_dict(figure) == figure
