"""Guarded execution environment for LLM-generated numeric calculations.

Per the "use code generation for arithmetic, not a bigger LLM" strategy: the SQL
Generation/Insight agents can ask Qwen2.5-Coder to write a short pandas/numpy/scipy
snippet operating on the already-fetched DataFrame(s) (e.g. growth %, YoY, ratios) rather
than trusting the LLM to compute numbers itself. The snippet never touches the network,
filesystem, or process - only whitelisted builtins/modules and the given DataFrame(s).
"""
import ast
import concurrent.futures

import numpy as np
import pandas as pd
from scipy import stats

from backend.core.logging_config import get_logger

logger = get_logger(__name__)

_ALLOWED_BUILTINS = {
    "abs": abs, "round": round, "min": min, "max": max, "sum": sum, "len": len,
    "range": range, "sorted": sorted, "list": list, "dict": dict, "tuple": tuple,
    "set": set, "float": float, "int": int, "str": str, "bool": bool, "zip": zip,
    "enumerate": enumerate, "map": map, "filter": filter, "reversed": reversed,
}

_FORBIDDEN_NODES = (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.Lambda)


class CalcSandboxError(Exception):
    pass


def _validate_ast(code: str) -> None:
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise CalcSandboxError(f"Generated calculation code has a syntax error: {exc}") from exc

    for node in ast.walk(tree):
        if isinstance(node, _FORBIDDEN_NODES):
            raise CalcSandboxError(f"Disallowed construct in calculation code: {type(node).__name__}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise CalcSandboxError("Dunder attribute access is not allowed in calculation code.")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise CalcSandboxError("Dunder names are not allowed in calculation code.")


def _run(code: str, dataframes: dict[str, pd.DataFrame]) -> dict:
    _validate_ast(code)
    safe_globals = {
        "__builtins__": _ALLOWED_BUILTINS,
        "pd": pd,
        "np": np,
        "stats": stats,
    }
    safe_locals = {**dataframes, "result": None}
    exec(code, safe_globals, safe_locals)  # noqa: S102 - AST-validated, restricted builtins
    return {"result": safe_locals.get("result")}


def run_calculation(code: str, dataframes: dict[str, pd.DataFrame], timeout_seconds: int = 10) -> dict:
    """Executes `code` which must assign a JSON-serializable value to a `result` variable.
    Returns {"ok": True, "result": ...} or {"ok": False, "error": "..."} - never raises,
    so a bad snippet degrades to "no calculation" instead of failing the whole pipeline."""
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_run, code, dataframes)
            outcome = future.result(timeout=timeout_seconds)
        return {"ok": True, "result": outcome["result"]}
    except concurrent.futures.TimeoutError:
        logger.warning("Calculation sandbox timed out after %ss", timeout_seconds)
        return {"ok": False, "error": "Calculation timed out."}
    except CalcSandboxError as exc:
        logger.warning("Calculation sandbox rejected code: %s", exc)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Calculation sandbox raised: %s", exc)
        return {"ok": False, "error": str(exc)}
