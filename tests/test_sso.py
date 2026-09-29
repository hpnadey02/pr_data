"""AUTH_MODE=sso: the identity nginx + oauth2-proxy forward, and what each login mode allows.

What is locked down here:
  * only the four headers nginx_sso.conf overwrites are ever read - anything else a browser
    could type (X-Forwarded-Email, Remote-User ...) is ignored
  * /auth/sso needs the shared secret, so a direct POST to the backend cannot pose as someone
  * each mode switches the other login off - the email form must not bypass the SSO
  * users.csv still decides who is a developer; an unlisted SSO user is a "user"

No nginx, identity provider, LLM or database is involved: tokens are hand-built, and
users.csv / sessions.json live in tmp_path.
"""
import base64
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from streamlit.runtime.context import StreamlitHeaders

from backend.api import auth as auth_module
from backend.core import security, sso
from backend.core.rbac import ROLE_DEVELOPER, ROLE_USER
from backend.services import user_service
from config.settings import Settings

SECRET = "x" * 32
DEV_EMAIL = "hpandey@example.com"
USERS = (
    "user_id,user_name,user_email,role\n"
    "U002,harshit,HPandey@Example.com,developer\n"
    "U001,Analyst,analyst@example.com,user\n"
)


def _b64(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def _bearer(**claims) -> str:
    return f"Bearer {_b64({'alg': 'RS256', 'typ': 'JWT'})}.{_b64(claims)}.signature"


# --------------------------------------------------------------------------------------
# Reading the identity
# --------------------------------------------------------------------------------------

def test_oauth2_proxy_email_header_wins_over_the_token():
    identity = sso.identity_from_headers({
        "X-Auth-Request-Email": " Harshit.Pandey@Example.com ",
        "Authorization": _bearer(email="someone.else@example.com", name="Harshit Pandey"),
    })
    assert identity == sso.SsoIdentity(
        email="harshit.pandey@example.com", name="Harshit Pandey", source="x-auth-request-email",
    )


def test_the_reference_nginx_config_forwards_only_the_id_token():
    identity = sso.identity_from_headers(
        {"Authorization": _bearer(email="analyst@example.com", name="Analyst One")}
    )
    assert identity.email == "analyst@example.com"
    assert identity.name == "Analyst One"
    assert identity.source == "token claim 'email'"


@pytest.mark.parametrize("claim", ["preferred_username", "upn", "unique_name"])
def test_azure_token_without_an_email_claim(claim):
    identity = sso.identity_from_headers(
        {"authorization": _bearer(**{claim: "HPandey@Example.com"}, name="Pandey, Harshit")}
    )
    assert identity.email == DEV_EMAIL
    assert identity.name == "Pandey, Harshit"


def test_display_name_falls_back_to_given_and_family_name_then_the_address():
    both = sso.identity_from_headers(
        {"Authorization": _bearer(email=DEV_EMAIL, given_name="Harshit", family_name="Pandey")}
    )
    assert both.name == "Harshit Pandey"
    assert sso.identity_from_headers({"X-Auth-Request-Email": DEV_EMAIL}).name == "hpandey"
    username = sso.identity_from_headers({
        "X-Auth-Request-Email": DEV_EMAIL, "X-Auth-Request-Preferred-Username": "hpandey02",
    })
    assert username.name == "hpandey02"


def test_an_opaque_subject_id_is_not_an_email():
    assert sso.identity_from_headers({"X-Auth-Request-User": "8f14e45f-ceea-467a"}) is None
    user = sso.identity_from_headers({"X-Auth-Request-User": "Analyst@Example.com"})
    assert user.email == "analyst@example.com"


@pytest.mark.parametrize("header", [
    "X-Forwarded-Email", "X-Forwarded-User", "X-Email", "X-User", "Remote-User", "X-Remote-User",
])
def test_headers_a_browser_could_type_are_ignored(header):
    assert sso.identity_from_headers({header: DEV_EMAIL}) is None


@pytest.mark.parametrize("value", [
    "", "Basic aGk6dGhlcmU=", "Bearer", "Bearer a.b", "Bearer a.!!!.c",
    f"Bearer {_b64({'alg': 'none'})}.{_b64(['not', 'a', 'dict'])}.s",
    f"Bearer {_b64({'alg': 'none'})}.{base64.urlsafe_b64encode(b'not json').decode()}.s",
    f"Bearer {_b64({'alg': 'none'})}.{_b64({'name': 'No Address'})}.s",
    _bearer(email="not-an-email"),
])
def test_anything_but_a_token_naming_an_address_is_nobody(value):
    assert sso.identity_from_headers({"Authorization": value}) is None


def test_only_identity_headers_are_forwarded_from_streamlits_own_mapping():
    """st.context.headers is a StreamlitHeaders; the oauth2-proxy cookie must not travel on."""
    headers = StreamlitHeaders([
        ("x-auth-request-email", DEV_EMAIL),
        ("X-AUTH-REQUEST-USER", ""),
        ("Cookie", "_oauth2_proxy=session-cookie"),
        ("X-Forwarded-Email", "ceo@example.com"),
        ("Host", "chatbot.example.com"),
    ])
    assert sso.forwarded_identity_headers(headers) == {"x-auth-request-email": DEV_EMAIL}


# --------------------------------------------------------------------------------------
# The switch in .env
# --------------------------------------------------------------------------------------

def test_csv_is_the_default_and_needs_no_secret():
    settings = Settings(_env_file=None)
    assert settings.AUTH_MODE == "csv"
    assert not settings.is_sso
    assert "email login" in settings.describe_auth()


@pytest.mark.parametrize("raw", ["sso", "SSO", " Sso ", '"sso"'])
def test_sso_mode_is_normalised(raw):
    settings = Settings(_env_file=None, AUTH_MODE=raw, SSO_SHARED_SECRET=SECRET)
    assert settings.AUTH_MODE == "sso" and settings.is_sso
    assert "anyone the SSO lets in" in settings.describe_auth()
    required = Settings(_env_file=None, AUTH_MODE=raw, SSO_SHARED_SECRET=SECRET,
                        SSO_REQUIRE_USERS_CSV=True)
    assert "listed users only" in required.describe_auth()


def test_unknown_auth_mode_names_both_choices():
    with pytest.raises(ValueError, match="AUTH_MODE must be one of") as err:
        Settings(_env_file=None, AUTH_MODE="ldap")
    assert "csv" in str(err.value) and "sso" in str(err.value)


@pytest.mark.parametrize("secret", ["", "   ", "short-secret", '""'])
def test_sso_without_a_real_secret_refuses_to_start(secret):
    with pytest.raises(ValueError, match="SSO_SHARED_SECRET") as err:
        Settings(_env_file=None, AUTH_MODE="sso", SSO_SHARED_SECRET=secret)
    assert "secrets.token_urlsafe" in str(err.value)


# --------------------------------------------------------------------------------------
# /auth/sso and /auth/login
# --------------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    users = tmp_path / "users.csv"
    users.write_text(USERS, encoding="utf-8")
    monkeypatch.setattr(user_service.settings, "USERS_CSV", str(users))
    monkeypatch.setattr(security, "_sessions", {})
    monkeypatch.setattr(security, "_sessions_path", lambda: tmp_path / "sessions.json")
    monkeypatch.setattr(auth_module.settings, "AUTH_MODE", "sso")
    monkeypatch.setattr(auth_module.settings, "SSO_SHARED_SECRET", SECRET)
    monkeypatch.setattr(auth_module.settings, "SSO_REQUIRE_USERS_CSV", False)
    app = FastAPI()
    app.include_router(auth_module.router)
    return TestClient(app)


def _sso(client, headers: dict, secret: str | None = SECRET):
    sent = {} if secret is None else {sso.SECRET_HEADER: secret}
    return client.post("/auth/sso", json={"headers": headers}, headers=sent)


def test_listed_developer_signs_in_with_the_users_csv_identity(client):
    response = _sso(client, {"x-auth-request-email": DEV_EMAIL,
                             "authorization": _bearer(email=DEV_EMAIL, name="Harshit Pandey")})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["user_id"], body["user_name"], body["role"]) == ("U002", "harshit", ROLE_DEVELOPER)
    session = security.get_session(body["session_id"])
    assert security.session_role(session) == ROLE_DEVELOPER


def test_unlisted_sso_user_gets_in_as_a_user(client):
    body = _sso(client, {"authorization": _bearer(
        preferred_username="New.Joiner@Example.com", name="New Joiner")}).json()
    assert (body["user_id"], body["user_name"], body["role"]) == (
        "new.joiner@example.com", "New Joiner", ROLE_USER)


def test_unlisted_sso_user_is_refused_when_users_csv_is_required(client, monkeypatch):
    monkeypatch.setattr(auth_module.settings, "SSO_REQUIRE_USERS_CSV", True)
    response = _sso(client, {"x-auth-request-email": "new.joiner@example.com"})
    assert response.status_code == 403
    assert "new.joiner@example.com" in response.json()["detail"]
    assert security._sessions == {}
    listed = _sso(client, {"x-auth-request-email": "analyst@example.com"})
    assert listed.status_code == 200 and listed.json()["role"] == ROLE_USER


@pytest.mark.parametrize("secret", [None, "", "wrong", SECRET[:-1], SECRET + "x"])
def test_identity_without_the_shared_secret_is_refused(client, secret):
    response = _sso(client, {"x-auth-request-email": DEV_EMAIL}, secret=secret)
    assert response.status_code == 401
    assert "SSO_SHARED_SECRET" in response.json()["detail"]
    assert security._sessions == {}


@pytest.mark.parametrize("headers", [{}, {"x-forwarded-email": DEV_EMAIL}, {"authorization": "Bearer junk"}])
def test_no_identity_is_refused_with_the_fix(client, headers):
    response = _sso(client, headers)
    assert response.status_code == 401
    detail = response.json()["detail"]
    assert "8501" in detail and "set-xauthrequest" in detail
    assert security._sessions == {}


def test_email_login_is_off_in_sso_mode(client):
    response = client.post("/auth/login", json={"email": DEV_EMAIL})
    assert response.status_code == 403
    assert "AUTH_MODE=sso" in response.json()["detail"]
    assert security._sessions == {}


def test_sso_login_is_off_in_csv_mode(client, monkeypatch):
    monkeypatch.setattr(auth_module.settings, "AUTH_MODE", "csv")
    response = _sso(client, {"x-auth-request-email": DEV_EMAIL})
    assert response.status_code == 403
    assert "AUTH_MODE=csv" in response.json()["detail"]
    assert security._sessions == {}
    email = client.post("/auth/login", json={"email": DEV_EMAIL})
    assert email.status_code == 200 and email.json()["role"] == ROLE_DEVELOPER


def test_demoting_an_sso_developer_in_users_csv_applies_to_the_next_question(client, tmp_path):
    body = _sso(client, {"x-auth-request-email": DEV_EMAIL}).json()
    (tmp_path / "users.csv").write_text(
        "user_id,user_name,user_email,role\nU002,harshit,HPandey@Example.com,user\n",
        encoding="utf-8",
    )
    assert security.session_role(security.get_session(body["session_id"])) == ROLE_USER
