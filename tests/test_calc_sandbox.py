import pandas as pd

from backend.agents.calc_sandbox import run_calculation


def _sample_df():
    return pd.DataFrame({
        "month": ["2026-01", "2026-02", "2026-03"],
        "premium": [100.0, 120.0, 150.0],
    })


def test_simple_calculation_succeeds():
    code = "result = round((df['premium'].iloc[-1] - df['premium'].iloc[0]) / df['premium'].iloc[0] * 100, 2)"
    outcome = run_calculation(code, {"df": _sample_df()})
    assert outcome["ok"] is True
    assert outcome["result"] == 50.0


def test_import_statement_is_rejected():
    code = "import os\nresult = 1"
    outcome = run_calculation(code, {"df": _sample_df()})
    assert outcome["ok"] is False
    assert "Disallowed" in outcome["error"]


def test_dunder_access_is_rejected():
    code = "result = df.__class__"
    outcome = run_calculation(code, {"df": _sample_df()})
    assert outcome["ok"] is False


def test_uses_numpy_and_scipy():
    code = "result = float(np.mean(df['premium']))"
    outcome = run_calculation(code, {"df": _sample_df()})
    assert outcome["ok"] is True
    assert round(outcome["result"], 2) == 123.33
