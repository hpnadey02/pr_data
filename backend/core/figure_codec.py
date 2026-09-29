"""Plotly figures on the wire, readable whatever plotly version each side has loaded.

The backend builds a figure and Streamlit rebuilds it in a different process, and nothing
guarantees the two import the same plotly. Plotly's own JSON is not portable across
major versions:

- `layout.template` is a full copy of the writer's default theme, keyed by every trace
  type the WRITER knows. plotly 6 removed `heatmapgl`, so a figure written by 5.x fails
  to load in 6.x with "Invalid property ... 'heatmapgl'" - the insight and SQL arrive
  fine and only the chart is lost.
- plotly 6 writes numeric arrays as base64 `{"dtype", "bdata"}` objects, which 5.x
  rejects.

So the template is dropped (Streamlit applies its own theme regardless, and the template
is ~90% of the payload) and typed arrays are decoded back to plain lists. What remains -
bar/pie traces holding lists, plus a title and margins - means the same thing to every
plotly version. The reader is also lenient, so a response cached before this existed
still renders.

Both sides import from here so the writer and the reader can never drift apart.
"""
import base64
import json

import numpy as np

# plotly.js typed-array dtype names that numpy does not spell the same way.
_NUMPY_DTYPES = {"u1c": "u1"}


def _decode_typed_array(node: dict) -> list:
    dtype = np.dtype("<" + _NUMPY_DTYPES.get(node["dtype"], node["dtype"]))
    values = np.frombuffer(base64.b64decode(node["bdata"]), dtype=dtype)
    shape = node.get("shape")
    if shape:
        values = values.reshape([int(dim) for dim in str(shape).split(",")])
    return values.tolist()


def _is_typed_array(node) -> bool:
    return (
        isinstance(node, dict)
        and "dtype" in node
        and "bdata" in node
        and set(node) <= {"dtype", "bdata", "shape"}
    )


def _plain(node):
    if _is_typed_array(node):
        return _decode_typed_array(node)
    if isinstance(node, dict):
        return {key: _plain(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_plain(value) for value in node]
    return node


def portable_figure_dict(figure: dict) -> dict:
    """A figure dict with nothing in it that another plotly version could reject."""
    figure = _plain(figure)
    layout = figure.get("layout")
    if isinstance(layout, dict):
        layout.pop("template", None)
    return figure


def figure_to_json(fig) -> str:
    """Serialise a plotly Figure for the API response."""
    # Round-trip through plotly's own encoder first: it knows numpy, pandas and datetime.
    return json.dumps(portable_figure_dict(json.loads(fig.to_json())))


def figure_from_json(text: str):
    """Rebuild a Figure from `figure_to_json` output - or from any older payload."""
    import plotly.graph_objects as go

    # skip_invalid drops a property this plotly does not know instead of losing the
    # whole chart over it.
    return go.Figure(portable_figure_dict(json.loads(text)), skip_invalid=True)
