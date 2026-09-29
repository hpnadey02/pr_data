"""Pluggable LLM provider layer.

The same agent pipeline runs against either a local Ollama runtime or the Google Gemini
API, selected by LLM_PROVIDER in .env. Exactly one provider is ever constructed.

    LLM_PROVIDER=ollama  ->  OllamaProvider  (100% local, nothing leaves the machine)
    LLM_PROVIDER=gemini  ->  GeminiProvider  (HTTPS to Google, needs GEMINI_API_KEY)

Both expose the identical interface the agents use:

    chat(model, system, prompt, temperature, ...) -> str
    embed(model, text)                            -> list[float]
    check()                                       -> (ok: bool, detail: str)
    list_models()                                 -> [str, ...]
    warm_up(models)                               -> {model: outcome}

Agents never name a provider. They ask settings for a ROLE - settings.sql_model,
settings.insight_model, settings.router_model, settings.embed_model - so switching
providers requires no change anywhere in the pipeline.

Failures are classified rather than lumped together, because "the model is loading
slowly" and "nothing is listening" need completely different fixes. Every provider maps
its transport errors onto the same four exception types below, so the agents' existing
`except LLMUnavailableError` handlers degrade identically whichever provider is active.

PRIVACY: Ollama keeps every byte on the host. Gemini sends the question, the retrieved
column names, and the computed statistics (which include real figures from your data) to
Google. Only enable it for data that has been cleared for third-party processing.
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod

import requests

from backend.core.logging_config import get_logger
from config.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()


# ======================================================================================
# Error taxonomy - shared by every provider
# ======================================================================================

class LLMUnavailableError(Exception):
    """Base class - agents catch this to fall back to deterministic behaviour."""


class LLMConnectionError(LLMUnavailableError):
    """Nothing is listening / the host is unreachable."""


class LLMTimeoutError(LLMUnavailableError):
    """The service is up but did not answer in time."""


class LLMModelMissingError(LLMUnavailableError):
    """The service is up but the requested model does not exist there."""


class LLMResponseError(LLMUnavailableError):
    """The service answered with an error or an unparsable payload."""


class LLMAuthError(LLMUnavailableError):
    """Credentials are missing, invalid, or lack permission (remote providers only)."""


def _strip_fences(text: str) -> str:
    """Remove a ```lang ... ``` wrapper some models add around code/SQL answers."""
    cleaned = str(text or "").strip()
    if not cleaned.startswith("```"):
        return cleaned
    lines = cleaned.splitlines()
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    cleaned = "\n".join(lines).strip()
    for prefix in ("sql", "json", "python"):
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix):].lstrip()
            break
    return cleaned


class LLMProvider(ABC):
    """Interface every backend must implement. Kept deliberately narrow."""

    name: str = "abstract"

    @abstractmethod
    def chat(
        self,
        model: str,
        system: str,
        prompt: str,
        temperature: float = 0.0,
        *,
        num_predict: int | None = None,
        read_timeout: int | None = None,
        seed: int = 42,
    ) -> str:
        """One deterministic-by-default completion. Returns the text, never None."""

    @abstractmethod
    def embed(self, model: str, text: str) -> list[float]:
        """Embedding vector for one document."""

    @abstractmethod
    def check(self) -> tuple[bool, str]:
        """Never raises - health checks must always return a verdict."""

    @abstractmethod
    def list_models(self) -> list[str]:
        """Model names actually available, or [] when that cannot be determined."""

    def warm_up(self, models: list[str] | None = None) -> dict[str, str]:
        """Preload weights. Meaningless for hosted APIs, so the default is a no-op."""
        return {}

    def describe(self) -> str:
        return self.name


# ======================================================================================
# Ollama - local, on-prem
# ======================================================================================

class OllamaProvider(LLMProvider):
    """Local Ollama runtime, called over its HTTP API.

    The HTTP API is used directly rather than the `ollama` package so connect and read
    timeouts can be separated: an unreachable host then fails in seconds instead of
    blocking for the full read timeout. `keep_alive` holds weights in memory between
    questions, which removes the multi-minute cold load that used to surface as a
    mid-pipeline timeout.
    """

    name = "ollama"

    def __init__(self) -> None:
        self._session = requests.Session()
        self._warmed: set[str] = set()
        self._warm_lock = threading.Lock()

    def describe(self) -> str:
        return f"Ollama at {self._base_url()}"

    # -- transport --------------------------------------------------------------------

    def _base_url(self) -> str:
        return settings.OLLAMA_HOST.rstrip("/")

    def _timeouts(self, read_timeout: int | None = None) -> tuple[int, int]:
        return (
            settings.OLLAMA_CONNECT_TIMEOUT,
            int(read_timeout or settings.OLLAMA_REQUEST_TIMEOUT),
        )

    def _classify(self, exc: Exception, model: str) -> LLMUnavailableError:
        if isinstance(exc, requests.exceptions.ConnectTimeout):
            return LLMConnectionError(
                f"Could not connect to Ollama at {self._base_url()} within "
                f"{settings.OLLAMA_CONNECT_TIMEOUT}s. Start it with `ollama serve`."
            )
        if isinstance(exc, requests.exceptions.ReadTimeout):
            return LLMTimeoutError(
                f"Ollama is running, but model '{model}' did not respond within "
                f"{settings.OLLAMA_REQUEST_TIMEOUT}s. This is usually a cold model load or "
                f"a long prompt - raise OLLAMA_REQUEST_TIMEOUT in .env, or use a smaller "
                f"model."
            )
        if isinstance(exc, requests.exceptions.ConnectionError):
            return LLMConnectionError(
                f"Ollama is not reachable at {self._base_url()}. Start it with "
                f"`ollama serve` and confirm OLLAMA_HOST in .env matches its port."
            )
        return LLMResponseError(f"Ollama request failed for model '{model}': {exc}")

    def _error_text(self, response: requests.Response) -> str:
        try:
            payload = response.json()
            if isinstance(payload, dict) and payload.get("error"):
                return str(payload["error"])
        except ValueError:
            pass
        return " ".join(str(response.text or "").split())[:500]

    def _post(
        self, path: str, payload: dict, model: str, read_timeout: int | None = None
    ) -> dict:
        url = f"{self._base_url()}{path}"
        attempts = max(1, settings.OLLAMA_RETRY_COUNT + 1)
        last: LLMUnavailableError | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = self._session.post(
                    url, json=payload, timeout=self._timeouts(read_timeout)
                )
            except Exception as exc:  # noqa: BLE001 - classified immediately below
                last = self._classify(exc, model)
                logger.warning(
                    "Ollama %s attempt %s/%s failed: %s", path, attempt, attempts, last
                )
                # A cold connection failure will not fix itself inside one request; a
                # timeout might (the model is now loaded), so only timeouts are retried.
                if isinstance(last, LLMConnectionError) or attempt >= attempts:
                    break
                time.sleep(1)
                continue

            if response.status_code == 404:
                raise LLMModelMissingError(
                    f"Ollama is running, but model '{model}' is not available. "
                    f"Pull it once with: ollama pull {model}"
                )
            if response.status_code >= 400:
                raise LLMResponseError(
                    f"Ollama returned HTTP {response.status_code} for '{model}': "
                    f"{self._error_text(response)}"
                )

            try:
                data = response.json()
            except ValueError as exc:
                raise LLMResponseError(
                    f"Ollama returned a non-JSON response for '{model}'."
                ) from exc
            if not isinstance(data, dict):
                raise LLMResponseError(
                    f"Ollama returned an unexpected payload shape for '{model}'."
                )
            if data.get("error"):
                raise LLMResponseError(f"Ollama error for '{model}': {data['error']}")
            return data

        raise last or LLMResponseError(f"Ollama request failed for model '{model}'.")

    # -- interface --------------------------------------------------------------------

    def chat(
        self,
        model: str,
        system: str,
        prompt: str,
        temperature: float = 0.0,
        *,
        num_predict: int | None = None,
        read_timeout: int | None = None,
        seed: int = 42,
    ) -> str:
        payload = {
            "model": model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "keep_alive": settings.OLLAMA_KEEP_ALIVE,
            "options": {
                "temperature": float(temperature),
                "num_ctx": settings.OLLAMA_NUM_CTX,
                "num_predict": int(num_predict or settings.OLLAMA_NUM_PREDICT),
                "seed": seed,
                "repeat_penalty": 1.05,
            },
        }
        data = self._post("/api/generate", payload, model, read_timeout=read_timeout)
        content = _strip_fences(str(data.get("response", "")))
        if not content:
            raise LLMResponseError(f"Model '{model}' returned an empty response.")
        return content

    def embed(self, model: str, text: str) -> list[float]:
        payload = {
            "model": model,
            "prompt": text,
            "keep_alive": settings.OLLAMA_KEEP_ALIVE,
        }
        data = self._post(
            "/api/embeddings",
            payload,
            model,
            read_timeout=min(60, settings.OLLAMA_REQUEST_TIMEOUT),
        )
        vector = data.get("embedding")
        if not isinstance(vector, list) or not vector:
            raise LLMResponseError(f"Embedding model '{model}' returned no vector.")
        return [float(v) for v in vector]

    def list_models(self) -> list[str]:
        try:
            response = self._session.get(
                f"{self._base_url()}/api/tags", timeout=self._timeouts(15)
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:  # noqa: BLE001 - callers treat [] as "unknown"
            return []
        return [str(m.get("name", "")) for m in payload.get("models", []) if m.get("name")]

    def check(self) -> tuple[bool, str]:
        try:
            response = self._session.get(
                f"{self._base_url()}/api/tags", timeout=self._timeouts(15)
            )
        except Exception as exc:  # noqa: BLE001
            return False, str(self._classify(exc, "-"))
        if response.status_code >= 400:
            return False, f"Ollama returned HTTP {response.status_code} at {self._base_url()}."

        available = self.list_models()
        bare = {name.split(":")[0] for name in available}
        missing = [
            f"{role}='{name}'"
            for role, name in settings.configured_models.items()
            if name not in available and name.split(":")[0] not in bare
        ]
        if missing:
            return False, (
                "Ollama is running but these models are not pulled: "
                + ", ".join(missing)
                + ". Pull them with `ollama pull <model>`."
            )
        return True, "ok"

    def warm_up(self, models: list[str] | None = None) -> dict[str, str]:
        """Load weights now so the first real question does not pay the cold start.

        Only the models needed to answer the FIRST question are warmed. Warming every
        configured model would load several GB at once, which on a memory-constrained
        host causes more problems than the cold start it avoids. dict.fromkeys also
        collapses the common case where SQL and insight share one model.
        """
        targets = models or [settings.sql_model, settings.embed_model]
        results: dict[str, str] = {}
        for model in dict.fromkeys(t for t in targets if t):
            with self._warm_lock:
                if model in self._warmed:
                    results[model] = "already loaded"
                    continue
            started = time.time()
            try:
                if model == settings.embed_model:
                    self.embed(model, "warm up")
                else:
                    self.chat(model, "You are a health probe.", "Reply with: ok", num_predict=4)
                with self._warm_lock:
                    self._warmed.add(model)
                results[model] = f"loaded in {time.time() - started:.1f}s"
            except LLMUnavailableError as exc:
                results[model] = f"NOT loaded: {exc}"
                logger.warning("Warm-up failed for %s: %s", model, exc)
        return results


# ======================================================================================
# Gemini - Google Generative Language API
# ======================================================================================

class GeminiProvider(LLMProvider):
    """Google Gemini over its REST API.

    Called with `requests` rather than the google-generativeai SDK on purpose: the SDK
    pulls in grpc/protobuf, and this project already pins protobuf transitively through
    chromadb. Adding a second constraint on it risks the kind of native/dependency
    conflict that the pyarrow note in requirements.txt documents. REST needs nothing
    beyond `requests`, which is already a dependency.

    The API key travels in the `x-goog-api-key` header, never the URL, so it cannot leak
    into request logs or proxy access logs.
    """

    name = "gemini"

    # Errors worth a second attempt: transient server-side or rate-limit conditions.
    _RETRYABLE_STATUS = {429, 500, 502, 503, 504}

    def __init__(self) -> None:
        self._session = requests.Session()
        if not str(settings.GEMINI_API_KEY or "").strip():
            # settings validation already enforces this; belt and braces so a
            # mis-constructed provider fails loudly instead of 401-ing per request.
            raise LLMAuthError(
                "GEMINI_API_KEY is not set. Add it to .env, or set LLM_PROVIDER=ollama."
            )

    def describe(self) -> str:
        return f"Gemini at {self._base_url()}"

    # -- transport --------------------------------------------------------------------

    def _base_url(self) -> str:
        return settings.GEMINI_API_BASE.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "x-goog-api-key": settings.GEMINI_API_KEY,
            "Content-Type": "application/json",
        }

    def _timeouts(self, read_timeout: int | None = None) -> tuple[int, int]:
        return (
            settings.GEMINI_CONNECT_TIMEOUT,
            int(read_timeout or settings.GEMINI_REQUEST_TIMEOUT),
        )

    def _classify(self, exc: Exception, model: str) -> LLMUnavailableError:
        if isinstance(exc, requests.exceptions.ConnectTimeout):
            return LLMConnectionError(
                f"Could not connect to the Gemini API at {self._base_url()} within "
                f"{settings.GEMINI_CONNECT_TIMEOUT}s. Check internet access/proxy, or set "
                f"LLM_PROVIDER=ollama to run offline."
            )
        if isinstance(exc, requests.exceptions.ReadTimeout):
            return LLMTimeoutError(
                f"Gemini did not respond for model '{model}' within "
                f"{settings.GEMINI_REQUEST_TIMEOUT}s. Raise GEMINI_REQUEST_TIMEOUT in .env."
            )
        if isinstance(exc, requests.exceptions.SSLError):
            return LLMConnectionError(
                "TLS handshake with the Gemini API failed - this is usually a corporate "
                "proxy intercepting HTTPS. Set LLM_PROVIDER=ollama to run offline."
            )
        if isinstance(exc, requests.exceptions.ConnectionError):
            return LLMConnectionError(
                f"The Gemini API is not reachable at {self._base_url()}. Check internet "
                f"access, or set LLM_PROVIDER=ollama to run offline."
            )
        return LLMResponseError(f"Gemini request failed for model '{model}': {exc}")

    def _api_error(self, response: requests.Response, model: str) -> LLMUnavailableError:
        """Map an HTTP error onto the right exception, using Google's own message."""
        detail = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                detail = str(payload.get("error", {}).get("message") or "")
        except ValueError:
            pass
        detail = " ".join(detail.split())[:500] or response.reason or ""

        status = response.status_code
        if status in (401, 403) or "API key" in detail or "API_KEY" in detail:
            return LLMAuthError(
                f"Gemini rejected the API key (HTTP {status}): {detail} "
                "Check GEMINI_API_KEY in .env - see https://aistudio.google.com/apikey"
            )
        if status == 404:
            return LLMModelMissingError(
                f"Gemini has no model '{model}' (HTTP 404): {detail} "
                "Check the GEMINI_*_MODEL values in .env against the available models."
            )
        if status == 429:
            return LLMResponseError(
                f"Gemini rate limit / quota exceeded for '{model}': {detail} "
                "Wait and retry, or switch to LLM_PROVIDER=ollama."
            )
        return LLMResponseError(f"Gemini returned HTTP {status} for '{model}': {detail}")

    def _post(
        self, path: str, payload: dict, model: str, read_timeout: int | None = None
    ) -> dict:
        url = f"{self._base_url()}{path}"
        attempts = max(1, settings.GEMINI_RETRY_COUNT + 1)
        last: LLMUnavailableError | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = self._session.post(
                    url,
                    json=payload,
                    headers=self._headers(),
                    timeout=self._timeouts(read_timeout),
                )
            except Exception as exc:  # noqa: BLE001 - classified immediately below
                last = self._classify(exc, model)
                logger.warning(
                    "Gemini attempt %s/%s failed for '%s': %s", attempt, attempts, model, last
                )
                if isinstance(last, LLMConnectionError) or attempt >= attempts:
                    break
                time.sleep(1)
                continue

            if response.status_code >= 400:
                error = self._api_error(response, model)
                # An auth or missing-model failure will not fix itself on a retry.
                if response.status_code in self._RETRYABLE_STATUS and attempt < attempts:
                    last = error
                    logger.warning(
                        "Gemini attempt %s/%s retryable: %s", attempt, attempts, error
                    )
                    time.sleep(2)
                    continue
                raise error

            try:
                data = response.json()
            except ValueError as exc:
                raise LLMResponseError(
                    f"Gemini returned a non-JSON response for '{model}'."
                ) from exc
            if not isinstance(data, dict):
                raise LLMResponseError(
                    f"Gemini returned an unexpected payload shape for '{model}'."
                )
            return data

        raise last or LLMResponseError(f"Gemini request failed for model '{model}'.")

    # -- interface --------------------------------------------------------------------

    def chat(
        self,
        model: str,
        system: str,
        prompt: str,
        temperature: float = 0.0,
        *,
        num_predict: int | None = None,
        read_timeout: int | None = None,
        seed: int = 42,
    ) -> str:
        generation: dict = {
            "temperature": float(temperature),
            "maxOutputTokens": int(num_predict or settings.GEMINI_MAX_OUTPUT_TOKENS),
            "seed": seed,
        }
        # 1.5/2.0 models reject thinkingConfig outright, so it is only sent when the
        # operator has explicitly opted in for a 2.5-series model.
        if settings.GEMINI_THINKING_BUDGET >= 0:
            generation["thinkingConfig"] = {
                "thinkingBudget": settings.GEMINI_THINKING_BUDGET
            }

        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation,
        }
        data = self._post(
            f"/models/{model}:generateContent", payload, model, read_timeout=read_timeout
        )
        return _strip_fences(self._extract_text(data, model))

    def _extract_text(self, data: dict, model: str) -> str:
        """Pull the answer out, turning every "no text" case into a specific message."""
        blocked = (data.get("promptFeedback") or {}).get("blockReason")
        if blocked:
            raise LLMResponseError(
                f"Gemini blocked the prompt for '{model}' (reason: {blocked}). "
                "Rephrase the question."
            )

        candidates = data.get("candidates") or []
        if not candidates:
            raise LLMResponseError(f"Gemini returned no candidates for '{model}'.")

        candidate = candidates[0]
        finish = str(candidate.get("finishReason") or "")
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(str(p.get("text", "")) for p in parts).strip()

        if text:
            return text

        if finish == "MAX_TOKENS":
            hint = (
                " The model spent its budget on reasoning tokens - set "
                "GEMINI_THINKING_BUDGET=0 in .env for 2.5-series models."
                if settings.GEMINI_THINKING_BUDGET < 0
                else " Raise GEMINI_MAX_OUTPUT_TOKENS in .env."
            )
            raise LLMResponseError(
                f"Gemini hit the output-token limit for '{model}' before producing any "
                f"text.{hint}"
            )
        if finish in ("SAFETY", "RECITATION", "PROHIBITED_CONTENT"):
            raise LLMResponseError(
                f"Gemini stopped generating for '{model}' (finishReason: {finish})."
            )
        raise LLMResponseError(
            f"Gemini returned an empty response for '{model}' (finishReason: "
            f"{finish or 'unknown'})."
        )

    def embed(self, model: str, text: str) -> list[float]:
        payload = {
            "model": f"models/{model}",
            "content": {"parts": [{"text": text}]},
        }
        data = self._post(
            f"/models/{model}:embedContent",
            payload,
            model,
            read_timeout=min(60, settings.GEMINI_REQUEST_TIMEOUT),
        )
        vector = (data.get("embedding") or {}).get("values")
        if not isinstance(vector, list) or not vector:
            raise LLMResponseError(f"Gemini embedding model '{model}' returned no vector.")
        return [float(v) for v in vector]

    def list_models(self) -> list[str]:
        try:
            response = self._session.get(
                f"{self._base_url()}/models",
                headers=self._headers(),
                timeout=self._timeouts(15),
                params={"pageSize": 200},
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:  # noqa: BLE001 - callers treat [] as "unknown"
            return []
        # Names come back fully qualified ("models/gemini-2.0-flash").
        return [
            str(m.get("name", "")).split("/", 1)[-1]
            for m in payload.get("models", [])
            if m.get("name")
        ]

    def check(self) -> tuple[bool, str]:
        try:
            response = self._session.get(
                f"{self._base_url()}/models",
                headers=self._headers(),
                timeout=self._timeouts(15),
                params={"pageSize": 200},
            )
        except Exception as exc:  # noqa: BLE001
            return False, str(self._classify(exc, "-"))

        if response.status_code >= 400:
            return False, str(self._api_error(response, "-"))

        available = set(self.list_models())
        if not available:
            # Reachable and authorised, but the listing was empty/unparsable. Not fatal:
            # generateContent may still work, so report rather than block.
            return True, "ok (model list unavailable)"

        missing = [
            f"{role}='{name}'"
            for role, name in settings.configured_models.items()
            if name not in available
        ]
        if missing:
            return False, (
                "Gemini is reachable but these configured models were not found: "
                + ", ".join(missing)
                + ". Check the GEMINI_*_MODEL values in .env."
            )
        return True, "ok"


# ======================================================================================
# Factory
# ======================================================================================

_INSTANCE: LLMProvider | None = None
_FACTORY_LOCK = threading.Lock()

_PROVIDERS = {
    "ollama": OllamaProvider,
    "gemini": GeminiProvider,
}


def get_provider() -> LLMProvider:
    """Return the single active LLM provider, constructing it on first use."""
    global _INSTANCE
    if _INSTANCE is not None:
        return _INSTANCE
    with _FACTORY_LOCK:
        if _INSTANCE is None:
            mode = settings.LLM_PROVIDER
            provider = _PROVIDERS.get(mode)
            if provider is None:  # settings validation should have caught this already
                raise LLMResponseError(
                    f"LLM_PROVIDER='{mode}' is not supported. "
                    f"Use one of: {', '.join(sorted(_PROVIDERS))}."
                )
            _INSTANCE = provider()
            logger.info("Active LLM provider: %s", _INSTANCE.describe())
    return _INSTANCE


def reset_provider() -> None:
    """Drop the cached instance. Used by tests and by scripts that switch providers."""
    global _INSTANCE
    with _FACTORY_LOCK:
        _INSTANCE = None
