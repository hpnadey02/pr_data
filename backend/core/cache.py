"""Question-result cache. Redis when reachable, transparent in-memory TTL fallback otherwise.

Fail-safe by design: a Redis outage must never break the app, only make caching
process-local instead of shared. The active backend is exposed via `cache.backend_name`
so the /health endpoint and Streamlit debug panel can surface it.
"""
import hashlib
import json
import re
from typing import Any, Optional

from cachetools import TTLCache

from backend.core.logging_config import get_logger
from config.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()

_WHITESPACE_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s%]")


def normalize_for_cache(question: str) -> str:
    """Deterministic, non-LLM normalization for cache keys - hyphens are treated as word
    separators (not preserved) so "region-wise" and "region wise" hash identically."""
    q = question.strip().lower().replace("-", " ")
    q = _PUNCT_RE.sub(" ", q)
    q = _WHITESPACE_RE.sub(" ", q).strip()
    return q


def cache_key(question: str) -> str:
    """The active data source, LLM provider and chart setting are part of the key.

    Switching DATA_SOURCE must never serve an answer computed against the other backend,
    and switching LLM_PROVIDER must not serve one written by the other model - the SQL
    and the narrative both differ. Changing CHART_TYPE must show the new charts at once,
    not after CACHE_TTL_SECONDS.
    """
    normalized = normalize_for_cache(question)
    scope = (
        f"{settings.DATA_SOURCE}|{settings.DB_TABLE}|"
        f"{settings.LLM_PROVIDER}|{settings.sql_model}|{settings.CHART_TYPE}|{normalized}"
    )
    return "chat:" + hashlib.sha256(scope.encode("utf-8")).hexdigest()


class _InMemoryCache:
    name = "in-memory (process-local fallback)"

    def __init__(self, ttl_seconds: int):
        self._store: TTLCache = TTLCache(maxsize=2048, ttl=ttl_seconds)

    def get(self, key: str) -> Optional[dict]:
        return self._store.get(key)

    def set(self, key: str, value: dict, ttl_seconds: int | None = None) -> None:
        self._store[key] = value

    def ping(self) -> bool:
        return True


class _RedisCache:
    name = "redis"

    def __init__(self, client):
        self._client = client

    def get(self, key: str) -> Optional[dict]:
        raw = self._client.get(key)
        return json.loads(raw) if raw else None

    def set(self, key: str, value: dict, ttl_seconds: int | None = None) -> None:
        self._client.setex(key, ttl_seconds or settings.CACHE_TTL_SECONDS, json.dumps(value, default=str))

    def ping(self) -> bool:
        return bool(self._client.ping())


class Cache:
    def __init__(self):
        self.backend_name = "disabled"
        self._impl = None
        if settings.CACHE_ENABLED:
            self._impl = self._build_backend()

    def _build_backend(self):
        try:
            import redis

            client = redis.Redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                db=settings.REDIS_DB,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            client.ping()
            self.backend_name = "redis"
            logger.info("Cache backend: Redis (%s:%s)", settings.REDIS_HOST, settings.REDIS_PORT)
            return _RedisCache(client)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Redis unavailable (%s). Falling back to in-memory cache - not shared across "
                "processes/restarts. Start Redis to enable shared caching.",
                exc,
            )
            self.backend_name = "in-memory"
            return _InMemoryCache(settings.CACHE_TTL_SECONDS)

    def get(self, key: str) -> Optional[dict]:
        if not self._impl:
            return None
        try:
            return self._impl.get(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cache GET failed, treating as miss: %s", exc)
            return None

    def set(self, key: str, value: dict, ttl_seconds: int | None = None) -> None:
        if not self._impl:
            return
        try:
            self._impl.set(key, value, ttl_seconds)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cache SET failed (non-fatal): %s", exc)

    def status(self) -> dict[str, Any]:
        healthy = False
        if self._impl:
            try:
                healthy = self._impl.ping()
            except Exception:  # noqa: BLE001
                healthy = False
        return {"enabled": settings.CACHE_ENABLED, "backend": self.backend_name, "healthy": healthy}


_cache_singleton: Cache | None = None


def get_cache() -> Cache:
    global _cache_singleton
    if _cache_singleton is None:
        _cache_singleton = Cache()
    return _cache_singleton
