"""Shared LangGraph state passed between agent nodes. A TypedDict (not a class with
methods) because LangGraph merges partial-state dict returns from each node."""
from typing import Any, Optional, TypedDict

import pandas as pd


class AgentState(TypedDict, total=False):
    request_id: str
    user_id: str
    user_name: str
    raw_question: str
    chat_history: list[dict]

    normalized_question: str
    rewritten_question: str
    filters: dict[str, Any]
    # {physical_column: value} parsed deterministically from the question ("POLICY_NO-123")
    equality_filters: dict[str, str]
    # Columns the question explicitly asks to see ("give USGI_SUM_INSURED")
    requested_columns: list[str]
    # Concrete date range resolved in Python (backend/core/date_windows.py), stated to the
    # SQL model as fact so it never computes MAX()/GETDATE() arithmetic itself.
    date_window: Optional[Any]
    route: str
    sub_questions: list[str]

    retrieved_columns: list[dict]
    retrieved_examples: list[dict]

    sql_attempts: int
    sql_query: str
    sql_error: Optional[str]

    dataframe: Optional[pd.DataFrame]
    sub_dataframes: dict[str, pd.DataFrame]
    row_count: int
    truncated: bool

    needs_calculation: bool
    calculation_summary: Optional[str]

    chart_type: str
    charts: list[dict]

    insight: str
    # "direct" (formatted from the DataFrame), "narrative" (LLM, numerically verified),
    # "template" (deterministic fallback) or "empty".
    answer_mode: str

    # One entry per sub-question, each with its OWN sql / insight / charts. A compound
    # question runs the pipeline once per part instead of slicing a single result, so a
    # failure in one part leaves the others intact.
    sections: list[dict]
    section_index: int

    status: str
    error_message: Optional[str]
    warnings: list[str]
    timings_ms: dict[str, float]
