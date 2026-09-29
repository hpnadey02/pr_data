"""Health endpoints for fast local debugging - hit these first when something looks wrong."""
import plotly
from fastapi import APIRouter

from backend.core.cache import get_cache
from backend.core.column_registry import get_registry
from backend.core.datasource import get_datasource
from backend.core.llm_client import check_llm, list_models
from backend.retrieval.chroma_store import collections_ready
from config.settings import get_settings

router = APIRouter(prefix="/health", tags=["health"])
settings = get_settings()


@router.get("")
def health():
    return {"status": "ok"}


def _write_permissions():
    """[] = read-only, a list = what the login could change, None = could not tell."""
    try:
        return get_datasource().write_permissions()
    except Exception:  # noqa: BLE001 - a health check must always answer
        return None


@router.get("/detailed")
def health_detailed():
    try:
        source = get_datasource()
        db_ok, db_msg = source.check_connection()
        source_detail = source.describe()
    except Exception as exc:  # noqa: BLE001 - a health check must always answer
        db_ok, db_msg, source_detail = False, str(exc), settings.describe_source()

    llm_ok, llm_msg = check_llm()
    chroma_ok, chroma_msg = collections_ready()
    cache_status = get_cache().status()

    registry = get_registry()
    registry_info = {
        "columns": len(registry.columns),
        "shortcuts": registry.shortcut_count,
        "ambiguous_shortcuts_ignored": len(registry.collisions),
    }

    overall = "ok" if all([db_ok, llm_ok, chroma_ok]) else "degraded"
    return {
        "status": overall,
        "data_source": {
            "mode": settings.DATA_SOURCE,
            "ok": db_ok,
            "detail": db_msg,
            "target": source_detail,
            # Skipped when down: it would only wait out a second connect timeout.
            "write_permissions": _write_permissions() if db_ok else None,
        },
        # Kept for backwards compatibility with anything reading the old key.
        "sql_server": {"ok": db_ok, "detail": db_msg},
        "llm": {
            "provider": settings.LLM_PROVIDER,
            "ok": llm_ok,
            "detail": llm_msg,
            "target": settings.describe_llm(),
            "models_configured": settings.configured_models,
            "models_available": list_models(),
        },
        "chromadb": {"ok": chroma_ok, "detail": chroma_msg},
        # Never the secret itself - only whether one is set.
        "auth": {
            "mode": settings.AUTH_MODE,
            "detail": settings.describe_auth(),
            "users_csv_found": settings.resolved(settings.USERS_CSV).exists(),
            "sso_secret_set": bool(settings.SSO_SHARED_SECRET),
            # The ENTRA_* values only feed oauth2-proxy's config; listed so a missing one
            # is visible here, by name.
            "entra_tenant_id": settings.ENTRA_TENANT_ID,
            "entra_client_id": settings.ENTRA_CLIENT_ID,
            "entra_redirect_uri": settings.ENTRA_REDIRECT_URI,
            "entra_missing": settings.entra_missing(),
        },
        "column_registry": registry_info,
        "cache": cache_status,
        "charts": {
            "chart_type": settings.CHART_TYPE,
            # Compare with the frontend's `python -c "import plotly; print(plotly.__version__)"`
            # when a chart fails to render but the insight is fine.
            "plotly_version": plotly.__version__,
        },
    }
