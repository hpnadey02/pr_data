"""FastAPI entrypoint. Run with:
    uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
"""
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.api import auth, chat, health
from backend.core.column_registry import get_registry
from backend.core.datasource import get_datasource
from backend.core.llm_client import check_llm, warm_up
from backend.core.logging_config import configure_logging, get_logger
from backend.retrieval.chroma_store import collections_ready
from backend.services import logging_service
from config.settings import get_settings

configure_logging()
logger = get_logger(__name__)
settings = get_settings()

app = FastAPI(title="USGI Business Insight Chatbot", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())
    start = time.time()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "%s %s -> %s (%.1fms)", request.method, request.url.path, response.status_code,
        (time.time() - start) * 1000, extra={"request_id": request_id},
    )
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    request_id = request.headers.get("X-Request-ID", "-")
    logger.error("Unhandled exception on %s: %s", request.url.path, exc, exc_info=True,
                 extra={"request_id": request_id})
    detail = str(exc) if settings.DEBUG else "Something went wrong. Please refresh the page and try again."
    return JSONResponse(status_code=500, content={"status": "error", "message": detail, "request_id": request_id})


app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(health.router)


@app.on_event("startup")
def on_startup():
    """Verify every dependency and report precisely which one is unhealthy.

    Nothing here is fatal: the app still starts so /health/detailed can be used to
    diagnose the problem, and each failing subsystem degrades on its own terms.
    """
    logger.info("=" * 78)
    logger.info("Starting backend - verifying dependencies (failures are logged, not fatal)")
    logger.info("Active data source: %s", settings.describe_source())
    logger.info("Active LLM provider: %s", settings.describe_llm())
    logger.info("Active login: %s", settings.describe_auth())
    logging_service.ensure_log_file()

    try:
        source = get_datasource()
        db_ok, db_msg = source.check_connection()
    except Exception as exc:  # noqa: BLE001 - a misconfigured backend must not stop startup
        db_ok, db_msg = False, str(exc)
    logger.info("Data source (%s): %s (%s)", settings.DATA_SOURCE,
                "OK" if db_ok else "UNAVAILABLE", db_msg)

    if db_ok:
        # The app only reads. A login that can write turns any guard bypass into data loss.
        try:
            write_perms = source.write_permissions()
        except Exception:  # noqa: BLE001 - e.g. an older datasource.py without the method
            write_perms = None
        if write_perms:
            logger.warning(
                "The data source login can WRITE to %s (%s). The chatbot never needs this - "
                "ask the DBA to run scripts/sqlserver_readonly.sql.",
                settings.DB_TABLE, ", ".join(write_perms),
            )
        elif write_perms is None:
            logger.warning(
                "Could not check whether the data source login can write to %s - run "
                "python scripts/check_readonly_access.py.", settings.DB_TABLE,
            )

    llm_ok, llm_msg = check_llm()
    logger.info(
        "LLM (%s): %s (%s)", settings.LLM_PROVIDER,
        "OK" if llm_ok else "UNAVAILABLE", llm_msg,
    )

    if db_ok:
        registry = get_registry(refresh=True)
        logger.info(
            "Column registry: %s columns, %s shortcuts%s",
            len(registry.columns), registry.shortcut_count,
            f", {len(registry.collisions)} ambiguous ignored" if registry.collisions else "",
        )
    else:
        logger.warning("Column registry not built - the data source is unreachable.")

    chroma_ok, chroma_msg = collections_ready()
    logger.info("ChromaDB: %s (%s)", "OK" if chroma_ok else "NOT READY", chroma_msg)

    if not db_ok:
        if settings.is_local_source:
            logger.warning(
                "Local data file unreadable - check LOCAL_DATA_PATH / LOCAL_SHEET_NAME in .env."
            )
        else:
            logger.warning(
                "SQL Server unreachable at startup - chat requests will fail until this is "
                "fixed. To work offline, set DATA_SOURCE=local in .env."
            )
    if not llm_ok:
        if settings.is_gemini:
            logger.warning(
                "Gemini unreachable at startup - check GEMINI_API_KEY and internet access, "
                "or set LLM_PROVIDER=ollama to run fully locally."
            )
        else:
            logger.warning(
                "Ollama unreachable at startup - run `ollama serve` and pull required models."
            )
    if not chroma_ok:
        logger.warning(
            "Run `python scripts/setup_chromadb.py` once the data source and the LLM "
            "provider are reachable."
        )

    # Warm-up only means anything for a local runtime that has weights to load; the
    # Gemini provider returns an empty dict, so this is skipped there.
    if llm_ok and not settings.is_gemini and settings.OLLAMA_WARMUP_ON_STARTUP:
        # Loading weights now means the first user question does not pay a multi-minute
        # cold start, which is what previously surfaced as a mid-pipeline timeout.
        logger.info("Warming up models (first load can take a minute)...")
        for model, outcome in warm_up().items():
            logger.info("  %s: %s", model, outcome)

    logger.info("Backend ready on http://%s:%s", settings.BACKEND_HOST, settings.BACKEND_PORT)
    logger.info("=" * 78)
