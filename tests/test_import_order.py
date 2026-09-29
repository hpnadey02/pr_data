"""Guards the arrangement that keeps ChromaDB alive on Windows.

The conflict, verified directly on this host:

    import pandas; import onnxruntime   ->  ImportError: DLL load failed while importing
                                            onnxruntime_pybind11_state
                                        ->  or, non-deterministically, a hard crash:
                                            "Windows fatal exception: access violation"
    import onnxruntime; import pandas   ->  both import fine

pyarrow - which pandas imports whenever it is installed - loads native runtime DLLs that
shadow the ones onnxruntime needs. onnxruntime is what chromadb pulls in, so losing that
race silently disables semantic schema retrieval, and the access-violation variant kills
the process outright (it cannot be caught).

Fixing this by import ORDER was tried and rejected: it only holds if `backend` is imported
before pandas, which nothing guarantees across tests, scripts and plugins. So pyarrow is
simply not installed, and the one feature that wanted it - Streamlit's `st.dataframe` -
renders as HTML instead.

These tests fail the build if either half of that arrangement is undone.
"""
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_pyarrow_is_not_installed():
    """Installing pyarrow disables ChromaDB and can crash the uvicorn worker."""
    result = _run("import pyarrow")
    assert result.returncode != 0, (
        "pyarrow is installed. It shadows the native DLLs onnxruntime needs, which "
        "disables ChromaDB and can hard-crash the backend. Run: pip uninstall pyarrow"
    )


def test_onnxruntime_loads_after_pandas():
    """The ordering that actually matters at runtime, in a fresh interpreter."""
    result = _run("import pandas, onnxruntime; print('OK')")
    assert result.returncode == 0, (
        "onnxruntime failed to load after pandas - something reintroduced an Arrow-style "
        f"native dependency.\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "OK" in result.stdout


def test_chromadb_survives_the_backend_import_chain():
    """backend.main imports pandas transitively; ChromaDB must still be available."""
    result = _run(
        "import backend.main; "
        "from backend.retrieval import chroma_store; "
        "print('CHROMA=' + str(chroma_store.is_available())); "
        "print('ERR=' + repr(chroma_store.CHROMA_IMPORT_ERROR))"
    )
    assert result.returncode == 0, result.stderr
    assert "CHROMA=True" in result.stdout, (
        "ChromaDB was disabled by a native import failure:\n" + result.stdout + result.stderr
    )


def test_backend_init_does_not_import_native_libraries():
    """backend/__init__.py must stay import-free - see its docstring."""
    source = (PROJECT_ROOT / "backend" / "__init__.py").read_text(encoding="utf-8")
    for forbidden in ("import onnxruntime", "import pandas", "import chromadb"):
        assert forbidden not in source, (
            f"backend/__init__.py must not run `{forbidden}`: a native import here risks "
            "an uncatchable access violation on every backend process."
        )


def test_frontend_avoids_arrow_backed_widgets():
    """st.dataframe/st.table serialise through Arrow and would need pyarrow."""
    source = (PROJECT_ROOT / "frontend" / "streamlit_app.py").read_text(encoding="utf-8")
    assert "st.dataframe(" not in source, (
        "st.dataframe requires pyarrow. Use _render_table() instead."
    )
    assert "st.table(" not in source, (
        "st.table requires pyarrow. Use _render_table() instead."
    )
    assert "_render_table" in source
