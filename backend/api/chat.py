"""The single chat endpoint. Internally fans out to the LangGraph multi-agent pipeline
(query understanding -> schema retrieval -> SQL generation/execution -> calculation ->
chart -> insight) behind one unified interface, with caching and a hard request timeout.
"""
import asyncio
import json
import time
import uuid

from fastapi import APIRouter, HTTPException

from backend.agents.graph import get_graph
from backend.core.cache import cache_key, get_cache
from backend.core.logging_config import get_logger
from backend.core import security
from backend.core.rbac import redact_for_role
from backend.models.schemas import ChartPayload, ChatRequest, ChatResponse, SectionPayload
from backend.services import logging_service
from config.settings import get_settings

logger = get_logger(__name__)
router = APIRouter(tags=["chat"])
settings = get_settings()


def _dataframe_preview(df, limit: int = 200) -> list[dict]:
    if df is None or df.empty:
        return []
    return json.loads(df.head(limit).to_json(orient="records", date_format="iso"))


def _run_graph_sync(initial_state: dict) -> dict:
    # The section loop revisits sql_generation once per sub-question, each with its own
    # retries, so the default recursion limit of 25 is not enough headroom.
    return get_graph().invoke(initial_state, {"recursion_limit": 60})


def _build_sections(final_state: dict) -> list[SectionPayload]:
    return [
        SectionPayload(
            question=section.get("question", ""),
            insight=section.get("insight", ""),
            charts=[ChartPayload(**c) for c in section.get("charts", [])],
            sql=section.get("sql", ""),
            row_count=section.get("row_count", 0),
            answer_mode=section.get("answer_mode", ""),
            data_preview=_dataframe_preview(section.get("dataframe")),
            error=section.get("error"),
        )
        for section in final_state.get("sections", [])
    ]


def _build_response(request_id: str, final_state: dict, cache_hit: bool, ques_no: int) -> ChatResponse:
    status = final_state.get("status") or ("ok" if final_state.get("insight") else "error")
    charts = [ChartPayload(**c) for c in final_state.get("charts", [])]
    return ChatResponse(
        status=status,
        request_id=request_id,
        sections=_build_sections(final_state),
        insight=final_state.get("insight", ""),
        charts=charts,
        sql=final_state.get("sql_query", ""),
        columns_used=[c["column"] for c in final_state.get("retrieved_columns", [])],
        data_preview=_dataframe_preview(final_state.get("dataframe")),
        row_count=final_state.get("row_count", 0),
        cache_hit=cache_hit,
        answer_mode=final_state.get("answer_mode", ""),
        data_source=settings.DATA_SOURCE,
        warnings=final_state.get("warnings", []),
        error_message=final_state.get("error_message"),
        timings_ms=final_state.get("timings_ms", {}),
        ques_no=ques_no,
    )


@router.post("/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest):
    request_id = str(uuid.uuid4())
    session = security.get_session(payload.session_id)
    if not session:
        raise HTTPException(
            status_code=401,
            detail="Your session has expired or is invalid. Please refresh the page and log in again.",
        )
    # Redaction is the LAST step on every path below: the cache and the chat log keep the
    # full response, so a developer still gets SQL for a question a user asked first.
    role = security.session_role(session)

    ques_no = security.touch_session(payload.session_id) or 0
    cache = get_cache()
    key = cache_key(payload.question)

    cached = cache.get(key)
    if cached:
        logger.info("Cache hit request_id=%s", request_id, extra={"request_id": request_id})
        response = ChatResponse(**{**cached, "request_id": request_id, "cache_hit": True, "ques_no": ques_no})
        logging_service.log_interaction(
            session["user_id"], session["user_name"], payload.question,
            response.sql, response.insight, session["login_time"].isoformat(), ques_no,
        )
        return redact_for_role(response, role)

    initial_state = {
        "request_id": request_id,
        "user_id": session["user_id"],
        "user_name": session["user_name"],
        "raw_question": payload.question,
        "chat_history": session.get("chat_history", []),
        "sql_attempts": 0,
        "sections": [],
        "section_index": 0,
        "warnings": [],
        "timings_ms": {},
    }

    t0 = time.time()
    try:
        final_state = await asyncio.wait_for(
            asyncio.to_thread(_run_graph_sync, initial_state),
            timeout=settings.REQUEST_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.error("Request timed out request_id=%s question=%s", request_id, payload.question,
                     extra={"request_id": request_id})
        response = ChatResponse(
            status="timeout", request_id=request_id,
            error_message="Request timed out. Please refresh the page.", ques_no=ques_no,
        )
        logging_service.log_interaction(
            session["user_id"], session["user_name"], payload.question,
            "", "TIMEOUT: Please refresh the page.", session["login_time"].isoformat(), ques_no,
        )
        return redact_for_role(response, role)
    except Exception as exc:  # noqa: BLE001
        logger.error("Unhandled pipeline error request_id=%s: %s", request_id, exc,
                      extra={"request_id": request_id})
        response = ChatResponse(
            status="error", request_id=request_id,
            error_message="Something went wrong processing your question. Please refresh the page and try again.",
            ques_no=ques_no,
        )
        logging_service.log_interaction(
            session["user_id"], session["user_name"], payload.question,
            "", response.error_message, session["login_time"].isoformat(), ques_no,
        )
        return redact_for_role(response, role)

    elapsed_ms = round((time.time() - t0) * 1000, 1)
    security.append_history(payload.session_id, payload.question, final_state.get("rewritten_question", payload.question))

    response = _build_response(request_id, final_state, cache_hit=False, ques_no=ques_no)
    response.timings_ms["total"] = elapsed_ms

    if response.status == "ok":
        cache.set(key, response.model_dump(exclude={"request_id", "cache_hit", "ques_no"}))

    logging_service.log_interaction(
        session["user_id"], session["user_name"], payload.question,
        response.sql, response.insight or (response.error_message or ""),
        session["login_time"].isoformat(), ques_no,
    )

    logger.info("chat request_id=%s status=%s elapsed_ms=%s", request_id, response.status, elapsed_ms,
                extra={"request_id": request_id})
    return redact_for_role(response, role)
