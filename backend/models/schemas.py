"""Pydantic request/response contracts shared by FastAPI routes and the Streamlit client."""
from typing import Any, Optional

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr


class SsoLoginRequest(BaseModel):
    # The identity headers the Streamlit server received from nginx, forwarded as they
    # came (backend/core/sso.IDENTITY_HEADERS). The backend decides who they name.
    headers: dict[str, str]


class LoginResponse(BaseModel):
    session_id: str
    user_id: str
    user_name: str
    # "developer" | "user". Only drives what the UI offers - /chat enforces it server-side.
    role: str = "user"


class LogoutRequest(BaseModel):
    session_id: str


class ChatRequest(BaseModel):
    session_id: str
    question: str = Field(min_length=1, max_length=1000)


class ChartPayload(BaseModel):
    chart_type: str
    title: str
    figure_json: str


class SectionPayload(BaseModel):
    """One answered part of the question: its own SQL, insight and chart(s).

    A simple question produces exactly one section. A compound question produces one per
    sub-question, each independently answered - so `error` being set on one section does
    not mean the others failed.
    """

    question: str
    insight: str = ""
    charts: list[ChartPayload] = []
    sql: str = ""
    row_count: int = 0
    answer_mode: str = ""
    data_preview: list[dict[str, Any]] = []
    error: Optional[str] = None


class ChatResponse(BaseModel):
    status: str  # "ok" | "partial" | "error" | "timeout"
    request_id: str
    # One entry per answered sub-question. Clients should render these; the flat
    # `insight`/`charts`/`sql` fields below stay populated as a combined view for any
    # client that does not.
    sections: list[SectionPayload] = []
    insight: str = ""
    charts: list[ChartPayload] = []
    sql: str = ""
    columns_used: list[str] = []
    data_preview: list[dict[str, Any]] = []
    row_count: int = 0
    cache_hit: bool = False
    # How the insight text was produced: "direct" (formatted straight from the query
    # result - no model involved), "narrative" (LLM prose, numerically verified),
    # "template" (deterministic fallback) or "empty".
    answer_mode: str = ""
    # "sqlserver" | "local" - which backend answered this question.
    data_source: str = ""
    warnings: list[str] = []
    error_message: Optional[str] = None
    timings_ms: dict[str, float] = {}
    ques_no: int = 0


class ErrorResponse(BaseModel):
    status: str = "error"
    message: str
    request_id: Optional[str] = None
