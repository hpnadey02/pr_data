"""Tests for the pluggable LLM provider layer.

No Ollama daemon and no Gemini key are needed: these cover the parts that must hold for
BOTH providers - configuration validation, role-to-model resolution, the factory
contract, and the shared error taxonomy.
"""
import pytest

from backend.core.llm_provider import (
    GeminiProvider,
    LLMAuthError,
    LLMConnectionError,
    LLMModelMissingError,
    LLMProvider,
    LLMResponseError,
    LLMTimeoutError,
    LLMUnavailableError,
    OllamaProvider,
)
from config.settings import ALLOWED_LLM_PROVIDERS, Settings


def _settings(**overrides):
    """Settings built in isolation from the on-disk .env."""
    return Settings(_env_file=None, **overrides)


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

def test_only_two_providers_are_allowed():
    assert set(ALLOWED_LLM_PROVIDERS) == {"ollama", "gemini"}


def test_invalid_provider_fails_fast_with_a_clear_message():
    with pytest.raises(Exception) as info:
        _settings(LLM_PROVIDER="openai")
    assert "LLM_PROVIDER" in str(info.value)


@pytest.mark.parametrize("value", ["GEMINI", " gemini ", '"gemini"'])
def test_provider_value_is_normalised(value):
    settings = _settings(LLM_PROVIDER=value, GEMINI_API_KEY="k")
    assert settings.LLM_PROVIDER == "gemini"


def test_gemini_requires_an_api_key():
    with pytest.raises(Exception) as info:
        _settings(LLM_PROVIDER="gemini", GEMINI_API_KEY="")
    assert "GEMINI_API_KEY" in str(info.value)


def test_ollama_does_not_require_a_gemini_key():
    """Local mode must work on an air-gapped box with no Google credentials at all."""
    settings = _settings(LLM_PROVIDER="ollama", GEMINI_API_KEY="")
    assert not settings.is_gemini
    assert "ollama" in settings.describe_llm()


def test_api_key_is_never_exposed_by_describe_llm():
    settings = _settings(LLM_PROVIDER="gemini", GEMINI_API_KEY="super-secret-key")
    assert "super-secret-key" not in settings.describe_llm()


# --------------------------------------------------------------------------------------
# Role -> model resolution: the switch the agents actually depend on
# --------------------------------------------------------------------------------------

def test_roles_resolve_to_ollama_models():
    settings = _settings(LLM_PROVIDER="ollama")
    assert settings.sql_model == settings.OLLAMA_SQL_MODEL
    assert settings.insight_model == settings.OLLAMA_INSIGHT_MODEL
    assert settings.router_model == settings.OLLAMA_ROUTER_MODEL
    assert settings.embed_model == settings.OLLAMA_EMBED_MODEL


def test_roles_resolve_to_gemini_models():
    settings = _settings(LLM_PROVIDER="gemini", GEMINI_API_KEY="k")
    assert settings.sql_model == settings.GEMINI_SQL_MODEL
    assert settings.insight_model == settings.GEMINI_INSIGHT_MODEL
    assert settings.router_model == settings.GEMINI_ROUTER_MODEL
    assert settings.embed_model == settings.GEMINI_EMBED_MODEL


def test_switching_provider_changes_every_role():
    """No role may stay pinned to the other provider's model after a switch."""
    ollama = _settings(LLM_PROVIDER="ollama")
    gemini = _settings(LLM_PROVIDER="gemini", GEMINI_API_KEY="k")
    for role in ("sql", "insight", "router", "embed"):
        assert ollama.configured_models[role] != gemini.configured_models[role]


def test_configured_models_covers_every_role():
    assert set(_settings().configured_models) == {"sql", "insight", "router", "embed"}


# --------------------------------------------------------------------------------------
# Interface parity
# --------------------------------------------------------------------------------------

def test_both_providers_implement_the_same_interface():
    for provider in (OllamaProvider, GeminiProvider):
        assert issubclass(provider, LLMProvider)
        for method in ("chat", "embed", "check", "list_models"):
            assert callable(getattr(provider, method))
            assert getattr(provider, method) is not getattr(LLMProvider, method)


def test_provider_is_abstract():
    with pytest.raises(TypeError):
        LLMProvider()  # type: ignore[abstract]


def test_warm_up_defaults_to_a_no_op():
    """Hosted providers have no weights to preload; the base must not force an override."""
    assert LLMProvider.warm_up(object()) == {}  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# Error taxonomy - agents catch only the base class, so every variant must inherit it
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "error",
    [
        LLMConnectionError,
        LLMTimeoutError,
        LLMModelMissingError,
        LLMResponseError,
        LLMAuthError,
    ],
)
def test_every_error_degrades_through_the_base_class(error):
    assert issubclass(error, LLMUnavailableError)
    with pytest.raises(LLMUnavailableError):
        raise error("boom")


def test_llm_client_reexports_the_shared_taxonomy():
    """Agents import from llm_client; those names must be the same objects."""
    from backend.core import llm_client

    assert llm_client.LLMUnavailableError is LLMUnavailableError
    assert llm_client.LLMConnectionError is LLMConnectionError
