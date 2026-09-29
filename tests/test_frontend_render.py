"""Both frontend screens must render without a backend: the UNISONIC skin is cosmetic and
must never be the reason the chat fails to load."""
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pandas as pd
import plotly.express as px
import pytest
import streamlit as st
from streamlit.runtime.context import StreamlitHeaders
from streamlit.testing.v1 import AppTest

from backend.core import sso
from backend.core.figure_codec import figure_to_json
from config.settings import get_settings
from frontend.theme import EXAMPLE_QUESTIONS

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP = PROJECT_ROOT / "frontend" / "streamlit_app.py"

SQL = "SELECT TOP 5 [BRANCH_NAME], SUM([GROSS_PREMIUM]) AS total FROM dbo.May_2 GROUP BY [BRANCH_NAME]"
WARNING = "TOP 5 applied: ranking questions default to the top rows."


def _app() -> AppTest:
    return AppTest.from_file(str(APP), default_timeout=60)


def _logged_in_app(role: str | None = None) -> AppTest:
    at = _app()
    at.session_state["logged_in"] = True
    at.session_state["session_id"] = "s"
    at.session_state["user_id"] = "user@example.com"
    at.session_state["user_name"] = "User"
    if role is not None:
        at.session_state["role"] = role
    return at


def _chart() -> dict:
    df = pd.DataFrame({"BRANCH_NAME": ["MUMBAI MAIN", "DELHI CP"], "total": [4210000.0, 3875000.5]})
    fig = px.bar(df, x="BRANCH_NAME", y="total", title="Gross premium by branch")
    return {"chart_type": "bar", "title": "Gross premium by branch", "figure_json": figure_to_json(fig)}


def _answer(question: str) -> dict:
    insight = f"MUMBAI MAIN has the highest gross premium: 4,210,000.00 ({question})"
    section = {"question": question, "insight": insight, "charts": [_chart()], "sql": SQL,
               "row_count": 2, "answer_mode": "direct",
               "data_preview": [{"BRANCH_NAME": "MUMBAI MAIN", "total": 4210000.0}]}
    return {"status": "ok", "request_id": "r", "answer_mode": "direct", "sections": [section],
            "insight": insight, "charts": section["charts"], "sql": SQL, "row_count": 2,
            "data_preview": section["data_preview"], "warnings": [WARNING]}


class _Response:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


@pytest.fixture(autouse=True)
def email_login(monkeypatch):
    """The email form, whatever login .env has switched on; the SSO tests switch it."""
    monkeypatch.setattr(get_settings(), "AUTH_MODE", "csv")


@pytest.fixture
def backend(monkeypatch):
    """Stands in for the FastAPI service. AppTest runs the script in this process, so
    patching httpx here is what the app calls."""
    calls: list[tuple[str, dict]] = []
    sent_headers: list[tuple[str, dict | None]] = []
    login_role = {"role": "developer"}
    sso_refusal: dict = {}

    def fake_post(url, json=None, timeout=None, headers=None, **_):
        calls.append((url, json))
        sent_headers.append((url, headers))
        if url.endswith("/auth/login"):
            body = {"session_id": "s-1", "user_id": json["email"], "user_name": "Dev"}
            body.update(login_role)
            return _Response(body)
        if url.endswith("/auth/sso"):
            if sso_refusal:
                return _Response(sso_refusal, status_code=403)
            body = {"session_id": "s-sso", "user_id": "U002", "user_name": "harshit"}
            body.update(login_role)
            return _Response(body)
        if url.endswith("/chat"):
            return _Response(_answer(json["question"]))
        return _Response({"status": "ok"})

    monkeypatch.setattr(httpx, "post", fake_post)
    return {"calls": calls, "headers": sent_headers, "login_role": login_role,
            "sso_refusal": sso_refusal}


def _chat_questions(calls) -> list[str]:
    return [body["question"] for url, body in calls if url.endswith("/chat")]


def _markdown_text(at: AppTest) -> str:
    return " ".join(m.value for m in at.markdown)


def test_mascot_assets_ship_with_the_frontend():
    for name in ("unisonic_mascot.jpg", "unisonic_avatar.jpg"):
        assert (PROJECT_ROOT / "frontend" / "assets" / name).is_file(), name


def test_login_screen_renders():
    at = _app().run()
    assert not at.exception
    assert [t.label for t in at.text_input] == ["Work email"]
    assert any('class="uni-mascot"' in m.value for m in at.markdown)
    # The header carries no account or second mascot before sign-in.
    [header] = [m.value for m in at.markdown if 'class="uni-header"' in m.value]
    assert 'class="uni-header-mascot"' not in header and 'class="uni-account"' not in header
    # Example questions are for signed-in users only.
    assert len(at.sidebar.children) == 0


def test_chat_screen_sidebar_lists_examples_and_has_no_logout():
    at = _logged_in_app().run()
    assert not at.exception
    assert [b.label for b in at.sidebar.button] == EXAMPLE_QUESTIONS
    assert not any(b.disabled for b in at.sidebar.button)
    assert len(at.toggle) == 0
    assert not any("uni-ring" in m.value for m in at.markdown)
    # The chat screen's mascot sits in the header band, beside the account - not in a
    # column of its own, and not the login screen's large one.
    [header] = [m.value for m in at.markdown if 'class="uni-header"' in m.value]
    assert 'class="uni-header-mascot"' in header
    assert 'class="uni-account-name"' in header and "User" in header
    assert not any('class="uni-mascot"' in m.value for m in at.markdown)
    assert len(at.columns) == 0

    # Log out was removed by the user's decision.
    assert not any(b.label == "Log out" for b in at.button)
    assert [b.label for b in at.button] == EXAMPLE_QUESTIONS


def test_header_escapes_the_user_name():
    at = _logged_in_app()
    at.session_state["user_name"] = "<b>Eve</b>"
    at.run()
    [header] = [m.value for m in at.markdown if 'class="uni-header"' in m.value]
    assert "&lt;b&gt;Eve&lt;/b&gt;" in header and "<b>Eve</b>" not in header


def test_chat_screen_renders_history_as_bubbles():
    at = _logged_in_app()
    at.session_state["messages"] = [
        {"role": "user", "content": "Which branch has the <b>highest</b> business?"},
        {"role": "assistant", "id": 1, "status": "ok", "answer_mode": "direct",
         "sections": [{"question": "Which branch has the highest business?",
                       "insight": "MUMBAI MAIN has the highest gross premium: 4,210,000.",
                       "charts": [], "sql": "SELECT 1", "row_count": 1}]},
    ]
    at.run()

    assert not at.exception
    # The welcome line plus the one answer; the user's turn is a plain bubble, not a
    # chat message, so it cannot pick up the bot's avatar or styling.
    assert len(at.chat_message) == 2
    assert "MUMBAI MAIN" in " ".join(m.value for m in at.chat_message[1].markdown)
    user_bubbles = [m.value for m in at.markdown if 'class="uni-user"' in m.value]
    assert user_bubbles == [
        '<div class="uni-user">Which branch has the &lt;b&gt;highest&lt;/b&gt; business?</div>'
    ]
    assert len(at.chat_input) == 1


def test_clicking_an_example_question_asks_it(backend):
    at = _logged_in_app().run()
    question = EXAMPLE_QUESTIONS[9]
    at.sidebar.button(key="uni-example-9").click().run()

    assert not at.exception
    assert _chat_questions(backend["calls"]) == [question]
    assert [m["role"] for m in at.session_state["messages"]] == ["user", "assistant"]
    assert f'<div class="uni-user">{question}</div>' in _markdown_text(at)
    assert "MUMBAI MAIN has the highest gross premium" in _markdown_text(at)
    assert len(at.get("plotly_chart")) == 1
    # Answered, so the widgets that queue a question are live again.
    assert not any(b.disabled for b in at.sidebar.button)
    assert not at.chat_input[0].disabled


def test_typed_question_is_asked(backend):
    at = _logged_in_app().run()
    at.chat_input[0].set_value("  Show the top five branches  ").run()

    assert not at.exception
    assert _chat_questions(backend["calls"]) == ["Show the top five branches"]
    assert "MUMBAI MAIN has the highest gross premium" in _markdown_text(at)


def test_question_in_flight_disables_everything_that_could_queue_another(backend, monkeypatch):
    # Without the rerun that follows the answer, the tree is the one drawn while the
    # request was running.
    monkeypatch.setattr(st, "rerun", lambda *a, **k: None)
    at = _logged_in_app().run()
    at.sidebar.button(key="uni-example-0").click().run()

    assert not at.exception
    assert _chat_questions(backend["calls"]) == [EXAMPLE_QUESTIONS[0]]
    assert all(b.disabled for b in at.sidebar.button)
    assert at.chat_input[0].disabled


def test_scroll_script_runs_only_on_runs_that_add_a_message(backend):
    at = _logged_in_app().run()
    assert len(at.get("iframe")) == 0

    at.sidebar.button(key="uni-example-0").click().run()
    assert len(at.get("iframe")) == 1

    at.run()
    assert len(at.get("iframe")) == 0


def _history_with_warnings() -> list[dict]:
    return [
        {"role": "user", "content": "Show the top five branches"},
        {"role": "assistant", "id": 1, **_answer("Show the top five branches")},
    ]


def test_developer_sees_sql_and_warnings():
    at = _logged_in_app(role="developer")
    at.session_state["messages"] = _history_with_warnings()
    at.run()

    assert not at.exception
    labels = [e.label for e in at.expander]
    assert any(label.endswith("Warnings") for label in labels)
    assert any(label.endswith("View SQL & data") for label in labels)
    assert [c.value for c in at.code] == [SQL]
    assert any(WARNING in c.value for c in at.caption)
    assert any('class="usgi-preview"' in m.value for m in at.markdown)


@pytest.mark.parametrize("role", [None, "user"])
def test_user_sees_insight_and_chart_but_no_sql_or_warnings(role):
    at = _logged_in_app(role=role)
    at.session_state["messages"] = _history_with_warnings()
    at.run()

    assert not at.exception
    assert len(at.expander) == 0
    assert len(at.code) == 0
    assert not any(WARNING in c.value for c in at.caption)
    assert not any('class="usgi-preview"' in m.value for m in at.markdown)
    assert "MUMBAI MAIN has the highest gross premium" in _markdown_text(at)
    assert len(at.get("plotly_chart")) == 1


@pytest.mark.parametrize(
    ("returned", "stored"),
    [({"role": "developer"}, "developer"), ({"role": " Developer "}, "developer"),
     ({"role": "user"}, "user"), ({"role": "admin"}, "user"), ({"role": ""}, "user"),
     ({"role": None}, "user"), ({}, "user")],
)
def test_login_stores_role_with_least_privilege_default(backend, returned, stored):
    backend["login_role"].clear()
    backend["login_role"].update(returned)
    at = _app().run()
    at.text_input[0].input("dev@example.com")
    at.button[0].click().run()

    assert not at.exception
    assert at.session_state["logged_in"] is True
    assert at.session_state["role"] == stored


# --------------------------------------------------------------------------------------
# AUTH_MODE=sso
# --------------------------------------------------------------------------------------

SSO_SECRET = "y" * 32


def _sso_mode(monkeypatch, headers: list[tuple[str, str]] | None = None) -> None:
    """headers=None leaves st.context alone - AppTest has no server, so no request."""
    monkeypatch.setattr(get_settings(), "AUTH_MODE", "sso")
    monkeypatch.setattr(get_settings(), "SSO_SHARED_SECRET", SSO_SECRET)
    if headers is not None:
        monkeypatch.setattr(st, "context", SimpleNamespace(headers=StreamlitHeaders(headers)))


def _sso_calls(backend) -> list[tuple[dict, dict | None]]:
    bodies = [body for url, body in backend["calls"] if url.endswith("/auth/sso")]
    headers = [sent for url, sent in backend["headers"] if url.endswith("/auth/sso")]
    return list(zip(bodies, headers))


def test_sso_signs_in_without_a_form(backend, monkeypatch):
    _sso_mode(monkeypatch, [
        ("X-Auth-Request-Email", "hpandey@example.com"),
        ("Cookie", "_oauth2_proxy=session-cookie"),
        ("X-Forwarded-Email", "ceo@example.com"),
    ])
    at = _app().run()

    assert not at.exception
    assert len(at.text_input) == 0
    # Only the identity header travels on, with the secret; the cookie and the header a
    # browser could have typed stay behind.
    assert _sso_calls(backend) == [(
        {"headers": {"x-auth-request-email": "hpandey@example.com"}},
        {sso.SECRET_HEADER: SSO_SECRET},
    )]
    assert at.session_state["logged_in"] is True
    assert at.session_state["user_name"] == "harshit"
    assert at.session_state["role"] == "developer"
    # Straight onto the chat screen, name in the header.
    assert [b.label for b in at.sidebar.button] == EXAMPLE_QUESTIONS
    [header] = [m.value for m in at.markdown if 'class="uni-header"' in m.value]
    assert "harshit" in header

    at.run()
    assert len(_sso_calls(backend)) == 1, "signed in once per browser session, not per run"


@pytest.mark.parametrize("headers", [None, [], [("X-Forwarded-Email", "ceo@example.com")]])
def test_sso_without_an_identity_shows_the_fix_and_no_form(backend, monkeypatch, headers):
    _sso_mode(monkeypatch, headers)
    at = _app().run()

    assert not at.exception
    assert len(at.text_input) == 0
    assert "8501" in at.error[0].value
    assert _sso_calls(backend) == []
    assert at.session_state["logged_in"] is False
    assert len(at.sidebar.children) == 0


def test_sso_refusal_from_the_backend_is_shown(backend, monkeypatch):
    backend["sso_refusal"]["detail"] = (
        "Access denied. new.joiner@example.com signed in through SSO but is not in the "
        "authorized users list - contact your admin."
    )
    _sso_mode(monkeypatch, [("X-Auth-Request-Email", "new.joiner@example.com")])
    at = _app().run()

    assert not at.exception
    assert len(at.text_input) == 0
    assert "new.joiner@example.com" in at.error[0].value
    assert at.session_state["logged_in"] is False
