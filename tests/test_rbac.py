"""Role-based redaction of the pipeline's internals.

Hiding the SQL expander in Streamlit is cosmetic - a session_id is enough to POST /chat
directly - so the server strips SQL, raw rows, columns and warnings for a "user". What is
locked down here:
  * every unclear role (missing column, blank, misspelt, legacy session) is a "user"
  * redaction returns a COPY; the cache and the chat log keep the full response, so a
    developer asking a question a user asked first still gets the SQL

No LLM, database or ChromaDB is involved: the graph, cache and chat log are fakes, and
users.csv / sessions.json live in tmp_path.
"""
import csv
import json
import logging
import time
from datetime import datetime, timedelta

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import auth as auth_module
from backend.api import chat as chat_module
from backend.core import rbac, security
from backend.core.cache import _InMemoryCache
from backend.core.rbac import ROLE_DEVELOPER, ROLE_USER, normalize_role, redact_for_role
from backend.models.schemas import ChartPayload, ChatResponse, SectionPayload
from backend.services import user_service

DEV_EMAIL = "dev@example.com"
USER_EMAIL = "analyst@example.com"
QUESTION = "High-performing branch region-wise."
SQL = "SELECT TOP 5 [BRANCH_NAME], SUM([GROSS_PREMIUM]) AS total FROM dbo.May_2 GROUP BY [BRANCH_NAME]"
# The shape of a real DuckDB failure: the message quotes the SQL.
DB_ERROR = (
    'The generated SQL was invalid: Binder Error: Referenced column "GROSS_PREMIUMX" not '
    "found\nLINE 1: SELECT BRANCH_NAME, SUM(GROSS_PREMIUMX) AS total FROM may_2 GRO..."
)


def _write_users(tmp_path, monkeypatch, text: str, encoding: str = "utf-8"):
    path = tmp_path / "users.csv"
    path.write_text(text, encoding=encoding)
    monkeypatch.setattr(user_service.settings, "USERS_CSV", str(path))
    return path


@pytest.fixture
def isolated_sessions(tmp_path, monkeypatch):
    """Never touch the real data/sessions.json."""
    monkeypatch.setattr(security, "_sessions", {})
    path = tmp_path / "sessions.json"
    monkeypatch.setattr(security, "_sessions_path", lambda: path)
    return path


# --------------------------------------------------------------------------------------
# normalize_role
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("developer", ROLE_DEVELOPER),
    ("  Developer ", ROLE_DEVELOPER),
    ("DEVELOPER", ROLE_DEVELOPER),
    ("user", ROLE_USER),
    ("USER", ROLE_USER),
    ("", ROLE_USER),
    ("   ", ROLE_USER),
    (None, ROLE_USER),
    ("admin", ROLE_USER),
    ("dev", ROLE_USER),
])
def test_normalize_role_falls_back_to_least_privilege(raw, expected):
    assert normalize_role(raw) == expected


def test_unknown_role_is_logged_with_the_allowed_roles(caplog):
    with caplog.at_level(logging.WARNING, logger=rbac.logger.name):
        assert normalize_role("superuser") == ROLE_USER
    message = caplog.text
    assert "superuser" in message
    assert "developer" in message and "user" in message


def test_blank_role_is_not_logged_as_unknown(caplog):
    with caplog.at_level(logging.WARNING, logger=rbac.logger.name):
        normalize_role("")
        normalize_role(None)
    assert caplog.text == ""


def test_can_view_internals_only_for_developer():
    assert rbac.can_view_internals("developer")
    assert rbac.can_view_internals(" Developer ")
    assert not rbac.can_view_internals("user")
    assert not rbac.can_view_internals(None)
    assert not rbac.can_view_internals("admin")


# --------------------------------------------------------------------------------------
# users.csv
# --------------------------------------------------------------------------------------

def test_role_is_read_from_users_csv(tmp_path, monkeypatch):
    _write_users(tmp_path, monkeypatch, (
        "user_id,user_name,user_email,role\n"
        "U001,Demo User,demo@example.com,user\n"
        "U002,harshit,HPandey@Example.com,Developer\n"
    ))
    assert user_service.find_user_by_email("demo@example.com")["role"] == ROLE_USER
    dev = user_service.find_user_by_email(" hpandey@example.COM ")
    assert dev == {
        "user_id": "U002", "user_name": "harshit",
        "user_email": "HPandey@Example.com", "role": ROLE_DEVELOPER,
    }


def test_users_csv_without_role_column_makes_everyone_a_user(tmp_path, monkeypatch):
    """The office laptop's current users.csv has no role column - it must keep working."""
    _write_users(tmp_path, monkeypatch, (
        "user_id,user_name,user_email\n"
        "U002,harshit,hpandey02@gmail.com\n"
    ))
    user = user_service.find_user_by_email("hpandey02@gmail.com")
    assert user is not None
    assert user["role"] == ROLE_USER


@pytest.mark.parametrize("cell", ["", "   ", "admin", "dev"])
def test_blank_or_unknown_role_cell_is_a_user(tmp_path, monkeypatch, cell):
    _write_users(tmp_path, monkeypatch, (
        "user_id,user_name,user_email,role\n"
        f"U002,harshit,hpandey02@gmail.com,{cell}\n"
    ))
    assert user_service.find_user_by_email("hpandey02@gmail.com")["role"] == ROLE_USER


def test_short_row_missing_the_role_cell_is_a_user(tmp_path, monkeypatch):
    _write_users(tmp_path, monkeypatch, (
        "user_id,user_name,user_email,role\n"
        "U002,harshit,hpandey02@gmail.com\n"
    ))
    assert user_service.find_user_by_email("hpandey02@gmail.com")["role"] == ROLE_USER


def test_excel_saved_users_csv_with_bom_and_capitalised_header(tmp_path, monkeypatch):
    """Adding the role column by hand in Excel writes a BOM and often a 'Role' header."""
    _write_users(tmp_path, monkeypatch, (
        "user_id,user_name,user_email, Role \n"
        "U002,harshit,hpandey02@gmail.com,developer\n"
    ), encoding="utf-8-sig")
    user = user_service.find_user_by_email("hpandey02@gmail.com")
    assert user is not None
    assert user["user_id"] == "U002"
    assert user["role"] == ROLE_DEVELOPER


def test_the_shipped_users_csv_rows_carry_valid_roles():
    path = user_service.settings.resolved("./data/users.csv")
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if rows and "role" not in rows[0]:
        pytest.skip("users.csv has no role column yet - everyone is a user, by design")
    assert rows and all(row["role"].strip().lower() in rbac.ROLES for row in rows)


# --------------------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------------------

def test_create_session_stores_the_normalised_role(isolated_sessions):
    dev = security.create_session("U002", "harshit", DEV_EMAIL, role=" Developer ")
    user = security.create_session("U001", "Demo", USER_EMAIL)
    assert dev["role"] == ROLE_DEVELOPER
    assert user["role"] == ROLE_USER
    saved = json.loads(isolated_sessions.read_text(encoding="utf-8"))
    assert saved[dev["session_id"]]["role"] == ROLE_DEVELOPER


def test_legacy_sessions_file_without_role_loads_as_user(isolated_sessions):
    stamp = "2026-09-06T06:23:15.758870"
    base = {"user_id": "U002", "user_name": "harshit", "user_email": DEV_EMAIL,
            "login_time": stamp, "last_activity": stamp, "ques_no": 1, "chat_history": []}
    isolated_sessions.write_text(json.dumps({
        "legacy": {**base, "session_id": "legacy"},
        "typo": {**base, "session_id": "typo", "role": "admin"},
        "dev": {**base, "session_id": "dev", "role": "developer"},
    }), encoding="utf-8")

    security._load()

    assert security._sessions["legacy"]["role"] == ROLE_USER
    assert security._sessions["typo"]["role"] == ROLE_USER
    assert security._sessions["dev"]["role"] == ROLE_DEVELOPER


def test_session_role_never_defaults_to_developer(tmp_path, monkeypatch):
    _write_users(tmp_path, monkeypatch, (
        "user_id,user_name,user_email,role\n"
        f"U002,harshit,{DEV_EMAIL},developer\n"
    ))
    assert security.session_role(None) == ROLE_USER
    assert security.session_role({}) == ROLE_USER
    # The stored role is not trusted - only users.csv grants developer.
    assert security.session_role({"role": "developer"}) == ROLE_USER
    assert security.session_role({"role": "developer", "user_email": USER_EMAIL}) == ROLE_USER
    assert security.session_role({"role": "user", "user_email": DEV_EMAIL}) == ROLE_DEVELOPER


def test_unreadable_users_csv_makes_everyone_a_user(tmp_path, monkeypatch):
    monkeypatch.setattr(user_service.settings, "USERS_CSV", str(tmp_path / "missing.csv"))
    assert security.session_role({"role": "developer", "user_email": DEV_EMAIL}) == ROLE_USER


def test_expired_sessions_are_purged_and_their_logout_time_recorded(isolated_sessions, monkeypatch):
    backfills = []
    monkeypatch.setattr(
        security.logging_service, "update_logout_time",
        lambda user_id, login_time, logout_time=None: backfills.append(
            (user_id, login_time, logout_time)),
    )
    idle = security.create_session("U001", "Analyst", USER_EMAIL)
    active = security.create_session("U002", "harshit", DEV_EMAIL)
    last_seen = datetime.utcnow() - timedelta(minutes=security.settings.SESSION_TIMEOUT_MINUTES + 5)
    idle["last_activity"] = last_seen

    assert security.get_session(idle["session_id"]) is None
    assert security.get_session(active["session_id"]) is active
    assert idle["session_id"] not in json.loads(isolated_sessions.read_text(encoding="utf-8"))
    # logout_time is when the user was last seen, not when the purge ran.
    assert backfills == [("U001", idle["login_time"].isoformat(), last_seen.isoformat())]


# --------------------------------------------------------------------------------------
# redact_for_role
# --------------------------------------------------------------------------------------

def _full_response() -> ChatResponse:
    chart = ChartPayload(chart_type="bar", title="Branches", figure_json="{}")
    section = SectionPayload(
        question="branch wise", insight="Mumbai leads.", charts=[chart], sql=SQL,
        row_count=2, answer_mode="direct", data_preview=[{"BRANCH_NAME": "MUMBAI", "total": 1.0}],
    )
    return ChatResponse(
        status="ok", request_id="r1", sections=[section, section.model_copy()],
        insight="Mumbai leads.", charts=[chart], sql=SQL, columns_used=["BRANCH_NAME"],
        data_preview=[{"BRANCH_NAME": "MUMBAI", "total": 1.0}], row_count=2,
        answer_mode="direct", data_source="local", warnings=["pie became bar"], ques_no=3,
    )


def test_user_gets_a_redacted_copy():
    original = _full_response()
    redacted = redact_for_role(original, ROLE_USER)

    assert redacted.sql == ""
    assert redacted.data_preview == []
    assert redacted.columns_used == []
    assert redacted.warnings == []
    for section in redacted.sections:
        assert section.sql == ""
        assert section.data_preview == []
    # What a user is meant to see survives.
    assert redacted.insight == "Mumbai leads."
    assert len(redacted.charts) == 1
    assert [s.insight for s in redacted.sections] == ["Mumbai leads.", "Mumbai leads."]
    assert all(len(s.charts) == 1 for s in redacted.sections)
    assert redacted.row_count == 2 and redacted.ques_no == 3 and redacted.status == "ok"
    # Same shape: it round-trips through the schema.
    assert ChatResponse(**redacted.model_dump()) == redacted


def test_redaction_never_mutates_the_original():
    original = _full_response()
    redact_for_role(original, ROLE_USER)
    assert original.sql == SQL
    assert original.warnings == ["pie became bar"]
    assert original.columns_used == ["BRANCH_NAME"]
    assert all(s.sql == SQL and s.data_preview for s in original.sections)


@pytest.mark.parametrize("role", [None, "", "admin"])
def test_unclear_role_is_redacted(role):
    assert redact_for_role(_full_response(), role).sql == ""


def test_developer_gets_everything():
    original = _full_response()
    assert redact_for_role(original, ROLE_DEVELOPER) is original
    assert redact_for_role(original, " DEVELOPER ").sections[0].sql == SQL


# --------------------------------------------------------------------------------------
# /auth/login and /chat at the API boundary
# --------------------------------------------------------------------------------------

class _Api:
    def __init__(self, client, cache, logs, graph):
        self.client = client
        self.cache = cache
        self.logs = logs
        self.graph = graph

    def login(self, email: str) -> dict:
        response = self.client.post("/auth/login", json={"email": email})
        assert response.status_code == 200, response.text
        return response.json()

    def ask(self, session_id: str, question: str = QUESTION) -> dict:
        response = self.client.post("/chat", json={"session_id": session_id, "question": question})
        assert response.status_code == 200, response.text
        return response.json()


class _FakeGraph:
    def __init__(self):
        self.calls = 0
        self.behaviour = "ok"

    def invoke(self, state, config=None):
        self.calls += 1
        if self.behaviour == "raise":
            raise RuntimeError("boom")
        if self.behaviour == "slow":
            time.sleep(0.5)
        frame = pd.DataFrame({"BRANCH_NAME": ["MUMBAI", "DELHI"], "total": [200.0, 100.0]})
        section = {
            "question": state["raw_question"], "insight": "MUMBAI leads with 200.",
            "charts": [{"chart_type": "bar", "title": "Branches", "figure_json": "{}"}],
            "sql": SQL, "row_count": 2, "answer_mode": "direct", "dataframe": frame,
            "error": None,
        }
        failed = {
            "question": "and by zone", "insight": "", "charts": [], "sql": SQL,
            "row_count": 0, "answer_mode": "error", "dataframe": None, "error": DB_ERROR,
        }
        if self.behaviour == "failed":
            return {"status": "error", "error_message": DB_ERROR, "sections": [failed],
                    "timings_ms": {}}
        if self.behaviour == "partial":
            return {
                "status": "partial", "sections": [section, failed],
                "insight": section["insight"], "charts": section["charts"], "sql_query": SQL,
                "dataframe": frame, "row_count": 2, "answer_mode": "direct",
                "warnings": [f'Could not answer "and by zone": {DB_ERROR}'], "timings_ms": {},
                "rewritten_question": state["raw_question"],
            }
        return {
            "status": "ok", "sections": [section], "insight": section["insight"],
            "charts": section["charts"], "sql_query": SQL, "dataframe": frame,
            "row_count": 2, "answer_mode": "direct", "warnings": ["a warning"],
            "retrieved_columns": [{"column": "BRANCH_NAME"}, {"column": "GROSS_PREMIUM"}],
            "timings_ms": {}, "rewritten_question": state["raw_question"],
        }


@pytest.fixture
def api(tmp_path, monkeypatch, isolated_sessions):
    # These tests sign in through the email form, whatever login .env has switched on.
    monkeypatch.setattr(auth_module.settings, "AUTH_MODE", "csv")
    _write_users(tmp_path, monkeypatch, (
        "user_id,user_name,user_email,role\n"
        f"U001,Analyst,{USER_EMAIL},user\n"
        f"U002,harshit,{DEV_EMAIL},developer\n"
    ))
    cache = _InMemoryCache(ttl_seconds=600)
    monkeypatch.setattr(chat_module, "get_cache", lambda: cache)

    graph = _FakeGraph()
    monkeypatch.setattr(chat_module, "get_graph", lambda: graph)

    logs: list[dict] = []

    def record(user_id, user_name, user_question, generated_sql_query, generated_output,
               login_time, ques_no):
        logs.append({"user_id": user_id, "sql": generated_sql_query, "output": generated_output})

    monkeypatch.setattr(chat_module.logging_service, "log_interaction", record)

    app = FastAPI()
    app.include_router(auth_module.router)
    app.include_router(chat_module.router)
    return _Api(TestClient(app), cache, logs, graph)


def _assert_redacted(body: dict):
    assert body["sql"] == ""
    assert body["data_preview"] == []
    assert body["columns_used"] == []
    assert body["warnings"] == []
    assert body["sections"], "sections must still be returned, only their internals emptied"
    for section in body["sections"]:
        assert section["sql"] == ""
        assert section["data_preview"] == []
    assert body["insight"] == "MUMBAI leads with 200."
    assert body["charts"] and body["sections"][0]["charts"]


def _assert_full(body: dict):
    assert body["sql"] == SQL
    assert body["data_preview"]
    assert body["columns_used"] == ["BRANCH_NAME", "GROSS_PREMIUM"]
    assert body["warnings"] == ["a warning"]
    assert body["sections"][0]["sql"] == SQL
    assert body["sections"][0]["data_preview"]


def test_login_returns_the_role(api):
    assert api.login(DEV_EMAIL)["role"] == ROLE_DEVELOPER
    assert api.login(USER_EMAIL)["role"] == ROLE_USER


def test_user_answer_is_redacted_but_cache_and_log_keep_the_sql(api):
    body = api.ask(api.login(USER_EMAIL)["session_id"])

    _assert_redacted(body)
    cached = list(api.cache._store.values())
    assert len(cached) == 1
    assert cached[0]["sql"] == SQL
    assert cached[0]["sections"][0]["sql"] == SQL
    assert api.logs[-1]["sql"] == SQL, "the audit row must record the SQL for every role"


def test_developer_cache_hit_after_a_user_still_gets_the_sql(api):
    user_session = api.login(USER_EMAIL)["session_id"]
    dev_session = api.login(DEV_EMAIL)["session_id"]

    _assert_redacted(api.ask(user_session))
    dev_body = api.ask(dev_session)

    assert api.graph.calls == 1, "the second question must be served from the cache"
    assert dev_body["cache_hit"] is True
    _assert_full(dev_body)


def test_user_cache_hit_after_a_developer_is_still_redacted(api):
    dev_session = api.login(DEV_EMAIL)["session_id"]
    user_session = api.login(USER_EMAIL)["session_id"]

    _assert_full(api.ask(dev_session))
    user_body = api.ask(user_session)

    assert api.graph.calls == 1
    assert user_body["cache_hit"] is True
    _assert_redacted(user_body)
    assert [entry["sql"] for entry in api.logs] == [SQL, SQL]


def test_demoting_a_developer_in_users_csv_applies_to_the_next_question(api, tmp_path, monkeypatch):
    session = api.login(DEV_EMAIL)["session_id"]
    _assert_full(api.ask(session))

    _write_users(tmp_path, monkeypatch, (
        "user_id,user_name,user_email,role\n"
        f"U002,harshit,{DEV_EMAIL},user\n"
    ))
    _assert_redacted(api.ask(session))


def test_removing_a_developer_from_users_csv_leaves_only_the_user_view(api, tmp_path, monkeypatch):
    session = api.login(DEV_EMAIL)["session_id"]
    _write_users(tmp_path, monkeypatch, "user_id,user_name,user_email,role\n")
    _assert_redacted(api.ask(session))


def test_pipeline_error_path_still_answers(api):
    api.graph.behaviour = "raise"
    body = api.ask(api.login(USER_EMAIL)["session_id"])
    assert body["status"] == "error"
    assert body["error_message"]
    assert body["sql"] == ""


def test_failed_part_error_text_is_a_reference_for_a_user(api):
    """A failed part's error is the raw database message, which quotes the SQL."""
    api.graph.behaviour = "partial"
    user_body = api.ask(api.login(USER_EMAIL)["session_id"])
    dev_body = api.ask(api.login(DEV_EMAIL)["session_id"])

    assert user_body["status"] == "partial"
    failed = user_body["sections"][1]
    assert "SELECT" not in json.dumps(user_body)
    assert failed["error"].startswith("the query behind it failed")
    assert user_body["request_id"][:8] in failed["error"]
    assert user_body["sections"][0]["error"] is None
    assert dev_body["sections"][1]["error"] == DB_ERROR


def test_failed_question_error_message_is_a_reference_for_a_user(api):
    api.graph.behaviour = "failed"
    user_body = api.ask(api.login(USER_EMAIL)["session_id"])
    dev_body = api.ask(api.login(DEV_EMAIL)["session_id"])

    assert user_body["status"] == "error"
    assert "SELECT" not in json.dumps(user_body)
    assert user_body["error_message"].startswith("This question could not be answered")
    assert user_body["request_id"][:8] in user_body["error_message"]
    assert dev_body["error_message"] == DB_ERROR


def test_timeout_path_still_answers(api, monkeypatch):
    monkeypatch.setattr(chat_module.settings, "REQUEST_TIMEOUT_SECONDS", 0.05)
    api.graph.behaviour = "slow"
    body = api.ask(api.login(DEV_EMAIL)["session_id"])
    assert body["status"] == "timeout"
    assert body["error_message"]


def test_invalid_session_is_rejected(api):
    response = api.client.post("/chat", json={"session_id": "nope", "question": QUESTION})
    assert response.status_code == 401
