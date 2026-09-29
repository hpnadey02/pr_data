"""Role-based access to the pipeline's internals (generated SQL, raw rows, warnings).

Only a developer sees how an answer was produced; a user sees the insight and the chart.
Hiding the SQL expander in Streamlit alone is not security - anyone holding a session_id
can POST /chat directly - so /chat redacts the response itself before returning it.

Every unclear case resolves to the least-privileged role: a missing, blank or misspelt
role in users.csv, or a session persisted before roles existed, is a "user".
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from backend.core.logging_config import get_logger

if TYPE_CHECKING:
    from backend.models.schemas import ChatResponse

logger = get_logger(__name__)

ROLE_DEVELOPER = "developer"
ROLE_USER = "user"
ROLES = (ROLE_DEVELOPER, ROLE_USER)


def normalize_role(value: Any) -> str:
    role = str(value).strip().lower() if value is not None else ""
    if role in ROLES:
        return role
    if role:
        logger.warning(
            "Unknown role %r - treating as %r. Allowed roles: %s. Fix the 'role' column "
            "in users.csv.", value, ROLE_USER, ", ".join(ROLES),
        )
    return ROLE_USER


def can_view_internals(role: Any) -> bool:
    return normalize_role(role) == ROLE_DEVELOPER


# A failed part's error is the raw database or validation message, and DuckDB's quotes the
# SQL itself ("LINE 1: SELECT ..."); a conversion error quotes a cell value. A user gets a
# reference instead - the real message is in logs/app.log under the same request id.
USER_SECTION_ERROR = (
    "the query behind it failed. Try rephrasing it; a developer can look it up under "
    "reference {ref}."
)
USER_ERROR = (
    "This question could not be answered. Try rephrasing it; a developer can look it up "
    "under reference {ref}."
)


def redact_for_role(response: "ChatResponse", role: Any) -> "ChatResponse":
    """A redacted COPY for anyone but a developer - the original is never mutated,
    because the caller caches it and a developer's later cache hit must still carry SQL."""
    if can_view_internals(role):
        return response
    ref = (response.request_id or "")[:8]
    sections = [
        section.model_copy(update={
            "sql": "",
            "data_preview": [],
            "error": USER_SECTION_ERROR.format(ref=ref) if section.error else None,
        })
        for section in response.sections
    ]
    error_message = response.error_message
    # A timeout message is already generic and tells the user what to do.
    if error_message and response.status != "timeout":
        error_message = USER_ERROR.format(ref=ref)
    return response.model_copy(
        update={
            "sections": sections,
            "sql": "",
            "data_preview": [],
            "columns_used": [],
            "warnings": [],
            "error_message": error_message,
        }
    )
