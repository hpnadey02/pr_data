"""Single chat interface for the USGI Business Insight Chatbot.
Run with: streamlit run frontend/streamlit_app.py
"""
import html
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import streamlit as st

from backend.core import sso
from backend.core.figure_codec import figure_from_json
from config.settings import get_settings
from frontend.theme import (
    BOT_AVATAR,
    CHAT_BOX_HEIGHT,
    EXAMPLE_QUESTIONS,
    INPUT_PLACEHOLDER,
    WELCOME_MESSAGE,
    apply_theme,
    render_footer,
    render_header,
    render_mascot,
    render_scroll_to_latest,
    render_user_bubble,
)

settings = get_settings()

st.set_page_config(
    page_title="UNISONIC · USGI Business Insight",
    page_icon=BOT_AVATAR,
    layout="wide",
    initial_sidebar_state="expanded",
)

DEVELOPER_ROLE = "developer"
CHAT_INPUT_KEY = "uni-question"


def _init_state():
    defaults = {
        "logged_in": False, "session_id": None, "user_id": None, "user_name": None,
        "role": "user", "messages": [], "pending_question": None,
    }
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)


def _role_from(login_response: dict) -> str:
    # Least privilege: anything but an explicit "developer" gets the plain user view.
    role = str(login_response.get("role") or "").strip().lower()
    return DEVELOPER_ROLE if role == DEVELOPER_ROLE else "user"


def _is_developer() -> bool:
    return st.session_state.get("role") == DEVELOPER_ROLE


def _api(
    path: str, json_body: dict, timeout: float | None = None, headers: dict | None = None,
) -> tuple[bool, dict | str]:
    url = f"{settings.BACKEND_URL}{path}"
    try:
        resp = httpx.post(
            url, json=json_body, timeout=timeout or settings.FRONTEND_REQUEST_TIMEOUT_SECONDS,
            headers=headers,
        )
    except httpx.ConnectError:
        return False, "Cannot reach the backend service. Please ensure the FastAPI server is running, then refresh the page."
    except httpx.TimeoutException:
        return False, "Request timed out. Please refresh the page."
    except Exception as exc:  # noqa: BLE001
        return False, f"Unexpected connection error: {exc}. Please refresh the page."

    if resp.status_code == 200:
        return True, resp.json()
    if resp.status_code in (401, 403):
        return False, resp.json().get("detail", "Access denied. Please refresh the page and log in again.")
    if resp.status_code == 504:
        return False, "Request timed out. Please refresh the page."
    try:
        detail = resp.json().get("detail") or resp.json().get("message")
    except Exception:  # noqa: BLE001
        detail = resp.text
    return False, detail or f"Request failed with status {resp.status_code}. Please refresh the page."


def _start_session(login_response: dict) -> None:
    st.session_state.logged_in = True
    st.session_state.session_id = login_response["session_id"]
    st.session_state.user_id = login_response["user_id"]
    st.session_state.user_name = login_response["user_name"]
    st.session_state.role = _role_from(login_response)


def _proxy_identity_headers() -> dict[str, str]:
    try:
        headers = st.context.headers
    except RuntimeError:  # no Streamlit server behind this run (AppTest, `python app.py`)
        return {}
    return sso.forwarded_identity_headers(headers)


def _sso_sign_in() -> str | None:
    """Signs in whoever nginx + oauth2-proxy vouch for; returns why not when it cannot."""
    headers = _proxy_identity_headers()
    if not headers:
        return (
            "Single sign-on is on (AUTH_MODE=sso) but this page was opened without a "
            "signed-in identity. Open it through the company SSO address (nginx), not "
            "directly on port 8501."
        )
    ok, data = _api(
        "/auth/sso", {"headers": headers}, timeout=15,
        headers={sso.SECRET_HEADER: settings.SSO_SHARED_SECRET},
    )
    if not ok:
        return data
    _start_session(data)
    return None


def _login_screen(sso_error: str | None):
    form_col, mascot_col = st.columns([3, 2], gap="large", vertical_alignment="center")
    with mascot_col:
        render_mascot()
    with form_col:
        if settings.is_sso:
            # Only reached when the automatic sign-in failed - there is no form to fall back
            # to, or it would be a way around the company sign-in.
            st.markdown("### 🔐 Single sign-on")
            st.error(sso_error or "Single sign-on failed.")
            st.caption("Refresh the page once this is fixed.")
        else:
            _login_form()


def _login_form():
    with st.form("login_form"):
        st.markdown("### 🔐 Sign in")
        st.caption("Ask a business question in plain English - get an NLP insight and a chart, grounded in dbo.May_2.")
        email = st.text_input("Work email", placeholder="you@company.com")
        submitted = st.form_submit_button("Log in", type="primary", use_container_width=True)
    if submitted:
        if not email.strip():
            st.error("Please enter your email.")
            return
        ok, data = _api("/auth/login", {"email": email.strip()}, timeout=15)
        if ok:
            _start_session(data)
            st.rerun()
        else:
            st.error(data)


_TABLE_CSS = """
<style>
.usgi-preview { max-height: 420px; overflow: auto; border: 1px solid rgba(128,128,128,.25);
                border-radius: .4rem; }
.usgi-preview table { border-collapse: collapse; width: 100%; font-size: .82rem; }
.usgi-preview th { position: sticky; top: 0; background: #101a30;
                   text-align: left; padding: .35rem .55rem; white-space: nowrap; }
.usgi-preview td { padding: .3rem .55rem; border-top: 1px solid rgba(128,128,128,.18);
                   white-space: nowrap; }
</style>
"""


def _render_table(rows: list[dict], max_rows: int = 200) -> None:
    """Render the result preview as plain HTML.

    Deliberately NOT `st.dataframe`/`st.table`: both serialise through Apache Arrow and so
    require pyarrow, which cannot be installed here - it shadows the native DLLs
    onnxruntime needs and takes ChromaDB down with it (see requirements.txt). Plain HTML
    has no such dependency; the trade-off is losing in-browser column sorting.
    """
    if not rows:
        return
    columns = list(rows[0].keys())
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in columns)
    body = []
    for row in rows[:max_rows]:
        cells = "".join(
            f"<td>{html.escape('' if row.get(c) is None else str(row.get(c)))}</td>"
            for c in columns
        )
        body.append(f"<tr>{cells}</tr>")
    st.markdown(
        _TABLE_CSS
        + f'<div class="usgi-preview"><table><thead><tr>{head}</tr></thead>'
        + f"<tbody>{''.join(body)}</tbody></table></div>",
        unsafe_allow_html=True,
    )
    if len(rows) > max_rows:
        st.caption(f"Showing the first {max_rows} of {len(rows)} preview rows.")


def _render_message(msg: dict):
    role = msg["role"]
    if role == "user":
        render_user_bubble(msg["content"])
        return
    with st.chat_message(role, avatar=BOT_AVATAR):
        status = msg.get("status", "ok")
        if status in ("error", "timeout"):
            st.error(msg.get("error_message") or "Something went wrong. Please refresh the page.")
            return

        badges = []
        if msg.get("cache_hit"):
            badges.append("⚡ Served from cache")
        # Make it visible when a figure came straight from SQL versus being narrated.
        mode_label = {
            "direct": "🔒 Answer taken verbatim from the query result",
            "template": "🔒 Figures taken directly from the query result",
            "narrative": "✅ Narrative verified against the query result",
        }.get(msg.get("answer_mode", ""))
        if mode_label:
            badges.append(mode_label)
        if msg.get("data_source"):
            badges.append(f"source: {msg['data_source']}")
        if badges:
            st.caption("  •  ".join(badges))

        sections = msg.get("sections") or []
        if sections:
            # A compound question is answered one part at a time: each part has its own
            # SQL, insight and chart, so they are rendered as separate blocks rather than
            # one merged answer.
            for position, section in enumerate(sections):
                _render_section(msg, section, position, numbered=len(sections) > 1)
        else:
            # A client-side fallback for a cached response saved before sections existed.
            st.markdown(msg.get("insight") or "_No insight generated._")
            _render_charts(msg.get("charts", []), key_prefix=f"{msg['id']}-flat")

        # SQL and pipeline warnings are for developers. The backend already strips them for
        # a user; this keeps the UI from drawing an empty panel in their place.
        if _is_developer() and msg.get("warnings"):
            with st.expander("⚠️ Warnings"):
                for w in msg["warnings"]:
                    st.caption(f"- {w}")


def _render_charts(charts: list[dict], key_prefix: str) -> None:
    for index, chart in enumerate(charts):
        try:
            # Not pio.from_json: that fails outright when the backend wrote the figure
            # with a different plotly major version ("Invalid property ... 'heatmapgl'").
            fig = figure_from_json(chart["figure_json"])
            st.plotly_chart(fig, use_container_width=True, key=f"chart-{key_prefix}-{index}")
        except Exception as exc:  # noqa: BLE001
            st.warning(f"Could not render chart '{chart.get('title')}': {exc}")


def _render_section(msg: dict, section: dict, position: int, numbered: bool) -> None:
    """One answered part of the question: heading, insight, chart, then (developer) its SQL."""
    question = section.get("question") or ""
    if numbered:
        st.markdown(f"##### {position + 1}. {question}")

    if section.get("error"):
        st.warning(f"Could not answer this part: {section['error']}")
        return

    st.markdown(section.get("insight") or "_No insight generated._")
    _render_charts(section.get("charts", []), key_prefix=f"{msg['id']}-{position}")

    if not _is_developer():
        return
    label = f"🔍 View SQL & data — {question[:60]}" if numbered else "🔍 View SQL & data"
    with st.expander(label):
        st.code(section.get("sql") or "(no SQL generated)", language="sql")
        st.caption(f"Rows returned: {section.get('row_count', 0)}")
        if section.get("data_preview"):
            _render_table(section["data_preview"])


def _queue_question(question: str) -> None:
    st.session_state.pending_question = question


def _queue_typed_question() -> None:
    typed = (st.session_state.get(CHAT_INPUT_KEY) or "").strip()
    if typed:
        _queue_question(typed)


def _ask(question: str, scroll_slot):
    st.session_state.messages.append({"role": "user", "content": question})
    # Drawn in place while the pipeline runs (30-150s on a CPU-only host), so the question
    # and a working bubble are on screen at once; the rerun replaces it with the answer.
    render_user_bubble(question)
    with scroll_slot:
        render_scroll_to_latest(nonce=len(st.session_state.messages))
    with st.chat_message("assistant", avatar=BOT_AVATAR):
        with st.spinner(f"🤖 Analyzing: {question}"):
            ok, data = _api("/chat", {"session_id": st.session_state.session_id, "question": question})
            # Stored before the spinner closes: closing it is the first Streamlit call after
            # the request, and that is where a Stop pressed meanwhile ends this run.
            msg_id = len(st.session_state.messages)
            if ok:
                st.session_state.messages.append({"role": "assistant", "id": msg_id, **data})
            else:
                st.session_state.messages.append({
                    "role": "assistant", "id": msg_id, "status": "error", "error_message": data,
                })
            st.session_state.scroll_to_latest = True


def _example_sidebar(busy: bool) -> None:
    with st.sidebar, st.container(key="uni-examples"):
        st.markdown(
            '<div class="uni-side-title">💡 Example questions</div>'
            '<div class="uni-side-hint">Click one to ask it.</div>',
            unsafe_allow_html=True,
        )
        for index, question in enumerate(EXAMPLE_QUESTIONS):
            st.button(
                question, key=f"uni-example-{index}", on_click=_queue_question, args=(question,),
                disabled=busy, use_container_width=True,
            )


def _chat_screen(busy: bool):
    # The mascot lives in the header band, so the chat panel takes the full width.
    with st.container(border=True, key="uni-chat"):
        # Fixed height makes this the scrolling chat window.
        chat_box = st.container(height=CHAT_BOX_HEIGHT, border=False, key="uni-chatbox")
        st.chat_input(
            INPUT_PLACEHOLDER, key=CHAT_INPUT_KEY, on_submit=_queue_typed_question,
            disabled=busy,
        )
    scroll_slot = st.container(key="uni-scroll")

    # The lane centres the conversation. Being one level down it also means the window
    # holds no chat message directly, which is what makes Streamlit pin it to the bottom -
    # and so drag a long answer's question and insight out of view.
    # render_scroll_to_latest positions it instead, on the runs that add a message.
    with chat_box, st.container(key="uni-lane"):
        with st.chat_message("assistant", avatar=BOT_AVATAR):
            st.markdown(WELCOME_MESSAGE)
        for msg in st.session_state.messages:
            _render_message(msg)
        question = st.session_state.pop("pending_question", None)
        if question:
            _ask(question, scroll_slot)
            st.rerun()

    if st.session_state.pop("scroll_to_latest", False):
        with scroll_slot:
            render_scroll_to_latest(nonce=len(st.session_state.messages))


def main():
    _init_state()
    apply_theme()
    sso_error = None
    if settings.is_sso and not st.session_state.logged_in:
        # No form in this mode: the sign-in happens on the page's first run, before the
        # header needs the user's name. A refresh after an idle-out signs in again.
        sso_error = _sso_sign_in()
    with st.container(key="uni-top"):
        if st.session_state.logged_in:
            # Name, role and mascot at the header's right end. No Log out, by the user's
            # decision (2026-09): a session ends when it idles out.
            role = DEVELOPER_ROLE if _is_developer() else "user"
            render_header(st.session_state.user_name or "", role)
        else:
            render_header()
    if not st.session_state.logged_in:
        _login_screen(sso_error)
    else:
        # A question is queued by a click or a submit and answered further down this run,
        # which blocks for the whole request. Everything that could queue another is drawn
        # disabled first: a click mid-request would interrupt the run and leave that
        # question with no answer. The rerun after the answer enables them again.
        busy = bool(st.session_state.pending_question)
        _example_sidebar(busy)
        _chat_screen(busy)
    render_footer()


if __name__ == "__main__":
    main()
