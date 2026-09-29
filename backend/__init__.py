"""Backend package.

Deliberately empty of imports.

An earlier version imported onnxruntime here to win a DLL-loading race against
pandas/pyarrow on Windows. That was abandoned: when onnxruntime loses the race the failure
is not always a catchable ImportError - it can be a hard interpreter crash
("Windows fatal exception: access violation"), which no try/except can contain. Making
every backend process take that risk at import time was worse than the problem it solved.

The conflict is avoided at its source instead: pyarrow is not installed at all (see
requirements.txt), so nothing shadows the DLLs onnxruntime needs. Streamlit's data preview
renders as HTML rather than through `st.dataframe`, which is the only thing that wanted
pyarrow. tests/test_import_order.py enforces both halves of that arrangement.
"""

NATIVE_RUNTIME_ERROR: str | None = None
