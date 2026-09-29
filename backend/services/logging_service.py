"""Chat audit log writer. Rows are partitioned into weekly + monthly CSV files under
CHAT_LOGS_DIR, and month folders older than CHAT_LOG_KEEP_MONTHS are deleted automatically -
see backend/services/chat_log_store.py for the layout.

Columns exactly as specified: user_id, user_name, user_question, generated_sql_query,
generated_output, login_time, logout_time, ques_no. login_time is written on every row
of a session and doubles as the session key when logout_time is back-filled at logout.
"""
from datetime import datetime

from backend.core.logging_config import get_logger
from backend.services.chat_log_store import ChatLogStore
from config.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()

FIELDNAMES = [
    "user_id",
    "user_name",
    "user_question",
    "generated_sql_query",
    "generated_output",
    "login_time",
    "logout_time",
    "ques_no",
]


def get_chat_log_store() -> ChatLogStore:
    return ChatLogStore(
        settings.resolved(settings.CHAT_LOGS_DIR), FIELDNAMES, settings.CHAT_LOG_KEEP_MONTHS
    )


def ensure_log_file() -> None:
    get_chat_log_store().ensure()


def log_interaction(
    user_id: str,
    user_name: str,
    user_question: str,
    generated_sql_query: str,
    generated_output: str,
    login_time: str,
    ques_no: int,
) -> None:
    row = {
        "user_id": user_id,
        "user_name": user_name,
        "user_question": user_question,
        "generated_sql_query": (generated_sql_query or "").replace("\n", " ").strip(),
        "generated_output": (generated_output or "").replace("\n", " ").strip(),
        "login_time": login_time,
        "logout_time": "",
        "ques_no": ques_no,
    }
    try:
        # A file open in Excel is handled inside: the row goes to that file's
        # .pending.csv sibling, so an audit row is never dropped.
        get_chat_log_store().append(row)
    except Exception as exc:  # noqa: BLE001 - never let logging break a response
        logger.error("Failed to write chat log row (non-fatal, response still returned): %s", exc)


def update_logout_time(user_id: str, login_time: str, logout_time: str | None = None) -> None:
    logout_ts = logout_time or datetime.utcnow().isoformat()
    try:
        get_chat_log_store().backfill_logout(user_id, login_time, logout_ts)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to update logout_time (non-fatal): %s", exc)
