"""Provider-agnostic LLM entry point.

All model access now goes through the pluggable provider layer in
backend/core/llm_provider.py, which lets the same pipeline run against a local Ollama
runtime or the Google Gemini API (LLM_PROVIDER in .env). The transport implementations
live inside those provider classes; no HTTP call to a model is made anywhere else.

This module stays as the single import surface the agents use, so switching providers
never touches agent code:

    from backend.core.llm_client import LLMUnavailableError, chat

Agents pass a model NAME resolved from a role (settings.sql_model, settings.embed_model,
...), so they never encode which provider is active.
"""
from backend.core.llm_provider import (  # noqa: F401 - re-exported as the public surface
    LLMAuthError,
    LLMConnectionError,
    LLMModelMissingError,
    LLMProvider,
    LLMResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    get_provider,
    reset_provider,
)


def chat(
    model: str,
    system: str,
    prompt: str,
    temperature: float = 0.0,
    *,
    num_predict: int | None = None,
    read_timeout: int | None = None,
    seed: int = 42,
) -> str:
    """One deterministic-by-default completion from the active provider.

    temperature is passed straight through: callers producing or restating NUMBERS use
    0.0 (settings.LLM_NUMERIC_TEMPERATURE / LLM_SQL_TEMPERATURE); only prose uses
    settings.LLM_TEXT_TEMPERATURE.
    """
    return get_provider().chat(
        model,
        system,
        prompt,
        temperature,
        num_predict=num_predict,
        read_timeout=read_timeout,
        seed=seed,
    )


def embed(model: str, text: str) -> list[float]:
    return get_provider().embed(model, text)


def list_models() -> list[str]:
    """Models available to the active provider, or [] if that cannot be determined."""
    return get_provider().list_models()


def check_llm() -> tuple[bool, str]:
    """Health probe for the active provider. Never raises."""
    try:
        return get_provider().check()
    except Exception as exc:  # noqa: BLE001 - a health check must always return a verdict
        return False, f"LLM provider could not be initialised: {exc}"


def warm_up(models: list[str] | None = None) -> dict[str, str]:
    """Preload weights where that is meaningful. A no-op for hosted providers."""
    try:
        return get_provider().warm_up(models)
    except Exception as exc:  # noqa: BLE001 - warm-up is an optimisation, never fatal
        return {"-": f"warm-up skipped: {exc}"}
