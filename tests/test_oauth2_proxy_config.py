"""scripts/build_oauth2_proxy_config.py: the ENTRA_* values in .env become oauth2-proxy's
config, and a value the proxy would reject at sign-in time stops the build instead."""
import importlib.util
import tomllib
from pathlib import Path

import pytest

from config.settings import Settings

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_oauth2_proxy_config.py"
_spec = importlib.util.spec_from_file_location("build_oauth2_proxy_config", SCRIPT)
builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(builder)

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
GOOD = {
    "ENTRA_TENANT_ID": TENANT,
    "ENTRA_CLIENT_ID": CLIENT,
    "ENTRA_CLIENT_SECRET": 'Ab1~c.D"e\\f',
    "ENTRA_REDIRECT_URI": "https://chatbot.example.com/oauth2/callback",
    "SSO_COOKIE_SECRET": "0123456789abcdef0123456789abcdef",
}


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**GOOD, **overrides})


def test_complete_entra_settings_render_a_config_oauth2_proxy_can_parse():
    settings = _settings(SSO_EMAIL_DOMAINS="Example.com, sompo.co.in")
    assert builder.problems(settings) == []
    config = tomllib.loads(builder.render(settings))

    assert config["oidc_issuer_url"] == f"https://login.microsoftonline.com/{TENANT}/v2.0"
    assert config["client_id"] == CLIENT
    # Quotes and backslashes in a real secret survive the round trip.
    assert config["client_secret"] == GOOD["ENTRA_CLIENT_SECRET"]
    assert config["redirect_url"] == GOOD["ENTRA_REDIRECT_URI"]
    assert config["cookie_secret"] == GOOD["SSO_COOKIE_SECRET"]
    assert config["cookie_secure"] is True
    assert config["email_domains"] == ["example.com", "sompo.co.in"]
    assert config["insecure_oidc_allow_unverified_email"] is True
    assert config["set_xauthrequest"] is True and config["set_authorization_header"] is True


def test_email_domains_default_to_anyone_in_the_tenant():
    assert tomllib.loads(builder.render(_settings()))["email_domains"] == ["*"]


def test_http_redirect_does_not_mark_the_cookie_secure():
    settings = _settings(ENTRA_REDIRECT_URI="http://localhost/oauth2/callback")
    assert builder.problems(settings) == []
    assert tomllib.loads(builder.render(settings))["cookie_secure"] is False


def test_every_empty_value_is_named():
    found = builder.problems(Settings(_env_file=None))
    for name in ("ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ENTRA_CLIENT_SECRET",
                 "ENTRA_REDIRECT_URI", "SSO_COOKIE_SECRET"):
        assert any(name in problem for problem in found), name


def test_a_tenant_domain_is_accepted():
    assert builder.problems(_settings(ENTRA_TENANT_ID="contoso.onmicrosoft.com")) == []


@pytest.mark.parametrize("overrides, expected", [
    ({"ENTRA_TENANT_ID": "my tenant"}, "ENTRA_TENANT_ID"),
    ({"ENTRA_CLIENT_ID": "chatbot-app"}, "ENTRA_CLIENT_ID"),
    ({"ENTRA_CLIENT_SECRET": CLIENT}, "Secret ID"),
    ({"ENTRA_REDIRECT_URI": "chatbot.example.com/oauth2/callback"}, "full URL"),
    ({"ENTRA_REDIRECT_URI": "https://chatbot.example.com/unisonic/"}, "/oauth2/callback"),
    ({"SSO_COOKIE_SECRET": "short"}, "16, 24 or 32 bytes"),
])
def test_values_entra_or_the_proxy_would_reject_are_caught(overrides, expected):
    found = builder.problems(_settings(**overrides))
    assert len(found) == 1 and expected in found[0]


@pytest.mark.parametrize("secret", [
    "0123456789abcdef",                                   # raw 16 bytes
    "0123456789abcdef01234567",                           # raw 24 bytes
    "q0ZyqI3Lp2cGSfEuYyb5M4mvjbSPvF5hRqh0t3NZ5Ok=",       # base64 of 32 bytes
])
def test_cookie_secret_forms_oauth2_proxy_accepts(secret):
    assert builder.problems(_settings(SSO_COOKIE_SECRET=secret)) == []


def test_quoted_env_values_are_unquoted():
    settings = _settings(ENTRA_CLIENT_ID=f'"{CLIENT}"', ENTRA_TENANT_ID=f"'{TENANT}'")
    assert (settings.ENTRA_CLIENT_ID, settings.ENTRA_TENANT_ID) == (CLIENT, TENANT)
