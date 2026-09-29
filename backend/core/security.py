"""Lightweight session store: allow-list auth (users.csv) issues a session_id used for
conversational context, per-session question numbering, and idle-timeout enforcement.

Persisted to SESSIONS_FILE on every mutation so a backend restart doesn't silently lose
active sessions (best-effort - a corrupt/missing file just starts empty, never crashes).
"""
import json
import threading
import uuid
from datetime import datetime, timedelta
from typing import Optional

from backend.core.logging_config import get_logger
from backend.core.rbac import ROLE_USER, normalize_role
from backend.services import logging_service
from backend.services.user_service import find_user_by_email
from config.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()

_lock = threading.Lock()
_sessions: dict[str, dict] = {}


def _sessions_path():
    return settings.resolved(settings.SESSIONS_FILE)


def _load() -> None:
    path = _sessions_path()
    if not path.exists():
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for sid, s in raw.items():
            s["login_time"] = datetime.fromisoformat(s["login_time"])
            s["last_activity"] = datetime.fromisoformat(s["last_activity"])
            # Sessions saved before roles existed carry none - they must not gain access.
            s["role"] = normalize_role(s.get("role"))
        _sessions.update(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load sessions file (starting fresh): %s", exc)


def _save() -> None:
    path = _sessions_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        serializable = {
            sid: {**s, "login_time": s["login_time"].isoformat(), "last_activity": s["last_activity"].isoformat()}
            for sid, s in _sessions.items()
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(serializable, f, indent=2)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not persist sessions file (non-fatal): %s", exc)


_load()


def create_session(user_id: str, user_name: str, user_email: str, role: str = ROLE_USER) -> dict:
    session_id = str(uuid.uuid4())
    now = datetime.utcnow()
    with _lock:
        _sessions[session_id] = {
            "session_id": session_id,
            "user_id": user_id,
            "user_name": user_name,
            "user_email": user_email,
            "role": normalize_role(role),
            "login_time": now,
            "last_activity": now,
            "ques_no": 0,
            "chat_history": [],
        }
        _save()
    logger.info("Session created user_id=%s role=%s session_id=%s",
                user_id, _sessions[session_id]["role"], session_id)
    return _sessions[session_id]


def session_role(session: Optional[dict]) -> str:
    """The role /chat enforces, read from users.csv on every call.

    Not the role stored at login: a session lives as long as it keeps asking, so a
    developer demoted in users.csv would otherwise keep seeing SQL indefinitely. Anyone
    no longer found there - or a users.csv that cannot be read - is a user.
    """
    email = str((session or {}).get("user_email") or "").strip()
    user = find_user_by_email(email) if email else None
    return normalize_role(user.get("role")) if user else ROLE_USER


def _pop_expired() -> list[dict]:
    now = datetime.utcnow()
    limit = timedelta(minutes=settings.SESSION_TIMEOUT_MINUTES)
    with _lock:
        expired = [sid for sid, s in _sessions.items() if now - s["last_activity"] > limit]
        if not expired:
            return []
        popped = [_sessions.pop(sid) for sid in expired]
        _save()
    return popped


def _record_expired_logouts(expired: list[dict]) -> None:
    # There is no Log out button, so an idle-out is how nearly every session ends. Its
    # logout_time is when the user was last seen, not when the purge happened to run.
    for session in expired:
        logger.info("Session expired (idle) user_id=%s session_id=%s",
                    session["user_id"], session["session_id"])
        logging_service.update_logout_time(
            session["user_id"], session["login_time"].isoformat(),
            logout_time=session["last_activity"].isoformat(),
        )


def get_session(session_id: str) -> Optional[dict]:
    # Purged here rather than only when the expired id is asked for: a closed tab never
    # asks again, and its session would otherwise sit in sessions.json forever.
    _record_expired_logouts(_pop_expired())
    with _lock:
        return _sessions.get(session_id)


def touch_session(session_id: str) -> Optional[int]:
    """Marks activity, increments and returns the new ques_no for this session."""
    with _lock:
        session = _sessions.get(session_id)
        if not session:
            return None
        session["last_activity"] = datetime.utcnow()
        session["ques_no"] += 1
        _save()
        return session["ques_no"]


def append_history(session_id: str, question: str, rewritten: str) -> None:
    with _lock:
        session = _sessions.get(session_id)
        if not session:
            return
        session["chat_history"].append({"question": question, "rewritten": rewritten})
        session["chat_history"] = session["chat_history"][-5:]
        _save()


def close_session(session_id: str) -> Optional[dict]:
    with _lock:
        session = _sessions.pop(session_id, None)
        _save()
    if session:
        logger.info("Session closed user_id=%s session_id=%s", session["user_id"], session_id)
    return session
