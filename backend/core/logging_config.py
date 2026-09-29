"""Rotating file + console logging. Every module gets a logger via get_logger(__name__)."""
import logging
import logging.handlers
import sys
from pathlib import Path

from config.settings import get_settings

_CONFIGURED = False


def configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    settings = get_settings()
    log_dir = settings.resolved(settings.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | req=%(request_id)s | %(message)s"
    )

    root = logging.getLogger()
    root.setLevel(settings.LOG_LEVEL)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "app.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(_DefaultRequestId())

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    console_handler.addFilter(_DefaultRequestId())

    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    error_handler = logging.handlers.RotatingFileHandler(
        log_dir / "errors.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(fmt)
    error_handler.addFilter(_DefaultRequestId())
    root.addHandler(error_handler)

    _quieten_noisy_libraries()
    _CONFIGURED = True


def _quieten_noisy_libraries() -> None:
    """Stop third-party noise from burying real failures in logs/errors.log.

    ChromaDB 0.5.x ships a telemetry client that is incompatible with current posthog
    releases and logs at ERROR on every collection access:

        Failed to send telemetry event ClientCreateCollectionEvent:
        capture() takes 1 positional argument but 3 were given

    It is cosmetic - retrieval works fine - but it produced thousands of ERROR lines that
    made the log unusable for diagnosing actual problems. Telemetry is disabled in
    backend/retrieval/chroma_store.py; this silences the client that still initialises.
    """
    for name in (
        "chromadb.telemetry",
        "chromadb.telemetry.product",
        "chromadb.telemetry.product.posthog",
        "posthog",
        "backoff",
    ):
        logging.getLogger(name).setLevel(logging.CRITICAL)
        logging.getLogger(name).propagate = False

    # httpx logs one INFO line per Ollama call; keep it, but drop urllib3's duplicate.
    logging.getLogger("urllib3.connectionpool").setLevel(logging.WARNING)


class _DefaultRequestId(logging.Filter):
    """Ensures %(request_id)s never crashes formatting when a caller forgets to pass it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)
