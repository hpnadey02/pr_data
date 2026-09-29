"""UNISONIC skin for the Streamlit chat: page CSS, header, mascot, user bubble, footer, and
the script that scrolls the chat window to the newest question.

Kept out of streamlit_app.py so the look can change without touching the request and
render logic. The selectors target Streamlit 1.41's DOM - `data-testid` attributes and the
`st-key-<key>` class a keyed container gets - so re-check them after upgrading streamlit.
The matching dark base theme lives in .streamlit/config.toml; without it Streamlit's own
widgets (expanders, code blocks, charts) stay light and clash with this background.
"""
import base64
import html
from functools import lru_cache
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
MASCOT_IMAGE = ASSETS_DIR / "unisonic_mascot.jpg"
AVATAR_IMAGE = ASSETS_DIR / "unisonic_avatar.jpg"

HEADER_TITLE = "UNISONIC AI – VIBE CORE"
HEADER_SUBTITLE = "USGI Business Insight Chatbot · ask in plain English, get an insight and a chart"
FOOTER_TEXT = "© 2077 UNISONIC Neural Systems"
WELCOME_MESSAGE = "🧠 UNISONIC online. Ask anything..."
INPUT_PLACEHOLDER = "Enter your query..."

EXAMPLE_QUESTIONS = [
    "High-performing branch region-wise.",
    "Vertical-wise business trend.",
    "Product-wise and branch-wise trend.",
    "Zone-wise and vertical-wise business contribution.",
    "Business-type-wise vertical performance.",
    "High-performing intermediaries.",
    "Show the top five branches",
    "Compare zone contribution and identify high-performing intermediaries.",
    "Show business contribution by zone, vertical, and product.",
    "Which branch has the highest business?",
    "Show actual versus target if the relevant columns exist.",
    "Generate separate charts for branch performance and vertical contribution.",
]

# A missing asset must cost only the picture, never the chat.
BOT_AVATAR = str(AVATAR_IMAGE) if AVATAR_IMAGE.exists() else "🧠"

# Streamlit only takes a pixel height; the CSS below stretches the box to the viewport in
# browsers that support :has(), and this is what the rest fall back to.
CHAT_BOX_HEIGHT = 560

_CSS = """
<style>
:root {
    --uni-bg-top: #0a0f1f;
    --uni-bg-bottom: #02040a;
    --uni-text: #e0f7ff;
    --uni-cyan: #00ffff;
    --uni-line: rgba(0, 255, 255, 0.2);
    --uni-glow: rgba(0, 255, 255, 0.15);
    --uni-glass: rgba(255, 255, 255, 0.05);
    --uni-bubble: rgba(255, 255, 255, 0.08);
    /* The design's cyan -> purple, with a lighter violet end: black text on pure
       `purple` is 2.2:1, on this violet it stays above 5:1. */
    --uni-accent: linear-gradient(90deg, #00ffff, #a855f7);
}

.stApp {
    background: radial-gradient(circle at top, var(--uni-bg-top), var(--uni-bg-bottom)) !important;
    color: var(--uni-text);
    font-family: 'Segoe UI', sans-serif;
    /* Lets the grid below sit at z-index -1 without dropping behind the page itself. */
    isolation: isolate;
}
.stApp::before {
    content: "";
    position: fixed;
    top: 0;
    left: 0;
    width: calc(100% + 120px);
    height: calc(100% + 120px);
    background-image: linear-gradient(rgba(0, 255, 255, 0.05) 1px, transparent 1px),
                      linear-gradient(90deg, rgba(0, 255, 255, 0.05) 1px, transparent 1px);
    background-size: 60px 60px;
    /* Two whole tiles per loop, so the restart is seamless. */
    animation: uni-grid 24s linear infinite;
    pointer-events: none;
    z-index: -1;
}
@keyframes uni-grid {
    from { transform: translate(0, 0); }
    to { transform: translate(-120px, -120px); }
}

[data-testid="stAppViewContainer"],
[data-testid="stMain"],
[data-testid="stHeader"] {
    background: transparent !important;
}
[data-testid="stDecoration"] { display: none; }
[data-testid="stMainBlockContainer"] {
    padding-top: 2.2rem;
    padding-bottom: 1rem;
    /* Streamlit's 5rem sides cost the chat a quarter of a laptop screen next to the sidebar. */
    padding-left: clamp(1rem, 3vw, 3rem);
    padding-right: clamp(1rem, 3vw, 3rem);
    max-width: 1400px;
}
.stApp p, .stApp li, .stApp label, .stApp button, .stApp input, .stApp textarea,
.stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5 {
    font-family: 'Segoe UI', sans-serif;
}

/* ---- header / footer ----
   Three columns: an empty one, the title, then the account and the mascot. The two outer
   columns share the leftover space equally, so the title stays centred; when the right one
   needs more than its share the title moves left instead of being overlapped. */
.uni-header {
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    align-items: center;
    column-gap: 16px;
    text-align: center;
    padding: 10px 16px 10px 20px;
    background: rgba(0, 0, 0, 0.4);
    backdrop-filter: blur(10px);
    border: 1px solid var(--uni-line);
    border-radius: 16px;
}
.uni-header-right {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 12px;
}
/* The header's own height (title + subtitle), so it never makes the band taller. */
.uni-header-mascot {
    height: 70px;
    width: auto;
    border-radius: 10px;
    filter: drop-shadow(0 0 8px var(--uni-cyan));
}
.uni-header-title {
    font-size: 2rem;
    font-weight: 700;
    line-height: 1.2;
    letter-spacing: 0.04em;
    color: var(--uni-text);
    text-shadow: 0 0 10px var(--uni-cyan);
}
.uni-header-sub {
    margin-top: 4px;
    font-size: 0.9rem;
    opacity: 0.7;
}
.uni-footer {
    text-align: center;
    padding: 6px 0 10px;
    font-size: 0.85rem;
    opacity: 0.6;
}

/* Account chip, left of the mascot. Plain HTML inside the header now that there is no
   Log out button; a fixed width keeps a long name from pushing the title around. */
.st-key-uni-top { margin-bottom: 8px; }
.uni-account {
    width: 150px;
    min-width: 0;
}
.uni-account-name,
.uni-account-role {
    text-align: right;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.uni-account-name {
    font-size: 0.9rem;
    opacity: 0.85;
}
.uni-account-role {
    font-size: 0.68rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    opacity: 0.55;
}
/* A phone has no room beside the title: the account drops under it, the mascot goes. */
@media (max-width: 640px) {
    .uni-header { grid-template-columns: 1fr; row-gap: 6px; }
    .uni-header > .uni-header-side:first-child { display: none; }
    .uni-header-right { justify-content: center; }
    .uni-header-mascot { display: none; }
    .uni-account-name, .uni-account-role { text-align: center; }
}

/* ---- chat panel ----
   The glass styling goes on the border wrapper Streamlit draws for border=True, not on the
   keyed block itself: Streamlit sizes children from that block's measured width, so
   padding added to the block would push charts past its edge. */
[data-testid="stVerticalBlockBorderWrapper"]:has(> div > .st-key-uni-chat),
[data-testid="stForm"] {
    background: var(--uni-glass);
    border: 1px solid var(--uni-line);
    border-radius: 20px;
    box-shadow: 0 0 25px var(--uni-glow);
}
[data-testid="stVerticalBlockBorderWrapper"]:has(> div > .st-key-uni-chatbox) {
    /* Whatever the viewport leaves after the header and the input row, so the SEND bar
       stays on screen on a 768px laptop and the window grows on anything taller. */
    height: max(360px, calc(100vh - 268px)) !important;
    scrollbar-width: thin;
    scrollbar-color: rgba(0, 255, 255, 0.35) transparent;
}
/* Narrower screens spend more height above the box: next to an open sidebar the title can
   wrap at 1000px, and on a phone the account also drops under it. */
@media (max-width: 1000px) {
    [data-testid="stVerticalBlockBorderWrapper"]:has(> div > .st-key-uni-chatbox) {
        height: max(360px, calc(100vh - 324px)) !important;
    }
}
@media (max-width: 640px) {
    [data-testid="stVerticalBlockBorderWrapper"]:has(> div > .st-key-uni-chatbox) {
        height: max(320px, calc(100vh - 385px)) !important;
    }
}

/* The conversation reads down a centred lane instead of stretching across a wide screen.
   Set on the border wrapper, which is what Streamlit measures to size charts, and with an
   explicit width: auto margins alone would make it shrink-to-fit, and a shrink-to-fit box
   feeds its own width back into itself. */
[data-testid="stVerticalBlockBorderWrapper"]:has(> div > .st-key-uni-lane) {
    width: 100%;
    max-width: 1080px;
    margin: 0 auto;
}

.st-key-uni-chatbox [data-testid="stChatMessage"] {
    background: var(--uni-bubble);
    border: 1px solid var(--uni-line);
    border-radius: 12px;
    padding: 12px 15px;
}
.st-key-uni-chatbox [data-testid="stChatMessage"] img {
    border-radius: 50%;
    box-shadow: 0 0 8px var(--uni-glow);
}
.uni-user {
    width: fit-content;
    max-width: 75%;
    margin: 4px 0 4px auto;
    padding: 12px 15px;
    border-radius: 12px;
    background: var(--uni-accent);
    color: #000;
    font-weight: 500;
    overflow-wrap: anywhere;
}

/* Plotly paints its own opaque background; let the bubble show through instead. */
.st-key-uni-chatbox .js-plotly-plot .main-svg { background: transparent !important; }

[data-testid="stExpander"] details {
    background: rgba(0, 0, 0, 0.25);
    border: 1px solid var(--uni-line);
    border-radius: 10px;
}

/* ---- input area ---- */
.st-key-uni-chat [data-testid="stChatInput"] {
    padding-top: 12px;
    border-top: 1px solid var(--uni-line);
    border-radius: 0 !important;
    background: transparent !important;
}
.st-key-uni-chat [data-testid="stChatInput"] > div {
    /* Lines up with the conversation lane above it. */
    max-width: 1080px;
    margin: 0 auto;
    background: rgba(0, 0, 0, 0.25) !important;
    border: 1px solid var(--uni-line) !important;
    border-radius: 12px !important;
}
[data-testid="stChatInputTextArea"] {
    color: var(--uni-text) !important;
    /* Room for the SEND label, which is wider than the icon it replaces. */
    padding-right: 96px !important;
}
[data-testid="stChatInputSubmitButton"] {
    align-self: center;
    width: auto !important;
    min-width: 76px;
    height: 32px !important;
    margin-right: 4px;
    padding: 0 18px !important;
    border-radius: 10px !important;
    background: var(--uni-accent) !important;
    color: #000 !important;
}
[data-testid="stChatInputSubmitButton"] svg { display: none; }
[data-testid="stChatInputSubmitButton"]::after {
    content: "SEND";
    font-weight: 700;
    letter-spacing: 0.05em;
}
[data-testid="stChatInputSubmitButton"]:disabled { opacity: 0.45; }

/* ---- buttons ---- */
[data-testid="stBaseButton-primary"],
[data-testid="stBaseButton-primaryFormSubmit"] {
    background: var(--uni-accent) !important;
    border: none !important;
    color: #000 !important;
    font-weight: 700;
}
[data-testid="stBaseButton-primary"]:hover,
[data-testid="stBaseButton-primaryFormSubmit"]:hover {
    box-shadow: 0 0 16px var(--uni-glow);
    filter: brightness(1.08);
}

/* ---- mascot ---- */
.uni-mascot-wrap {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 320px;
}
.uni-mascot {
    width: min(220px, 85%);
    border-radius: 14px;
    filter: drop-shadow(0 0 20px var(--uni-cyan));
}

/* ---- sidebar: example questions ---- */
[data-testid="stSidebar"] {
    /* Near-opaque: on a narrow screen the sidebar opens over the chat, not beside it. */
    background: rgba(2, 6, 18, 0.9) !important;
    backdrop-filter: blur(10px);
    border-right: 1px solid var(--uni-line);
}
.st-key-uni-examples { gap: 0.5rem; }
.uni-side-title {
    font-size: 1.05rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-shadow: 0 0 10px var(--uni-glow);
}
.uni-side-hint {
    /* Streamlit pulls a markdown block up 1rem to cancel a paragraph margin these divs
       do not have, so the gap above the first button is given back here. */
    margin: 4px 0 calc(1rem + 2px);
    font-size: 0.85rem;
    opacity: 0.65;
}
.st-key-uni-examples [data-testid="stBaseButton-secondary"] {
    justify-content: flex-start;
    min-height: 0;
    padding: 8px 12px;
    background: var(--uni-glass);
    border: 1px solid var(--uni-line);
    border-radius: 10px;
    color: var(--uni-text);
}
.st-key-uni-examples [data-testid="stBaseButton-secondary"] p {
    white-space: normal;
    text-align: left;
    font-size: 0.88rem;
    line-height: 1.35;
}
.st-key-uni-examples [data-testid="stBaseButton-secondary"]:hover:enabled {
    border-color: var(--uni-cyan);
    color: var(--uni-cyan);
    box-shadow: 0 0 12px var(--uni-glow);
}
.st-key-uni-examples [data-testid="stBaseButton-secondary"]:disabled { opacity: 0.45; }

/* Holds the one-shot scroll script: out of flow so it adds no gap, and clipped rather than
   display:none, which can stop a browser loading the iframe at all. */
[data-testid="stVerticalBlockBorderWrapper"]:has(> div > .st-key-uni-scroll) {
    position: absolute;
    width: 0;
    height: 0;
    overflow: hidden;
    pointer-events: none;
}

@media (prefers-reduced-motion: reduce) {
    .stApp::before { animation: none; }
}
</style>
"""


@lru_cache(maxsize=1)
def _mascot_data_uri() -> str:
    # Inlined because st.markdown cannot serve a local file; the image is 27 KB.
    if not MASCOT_IMAGE.exists():
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(MASCOT_IMAGE.read_bytes()).decode("ascii")


def apply_theme() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


def render_header(user_name: str | None = None, role: str | None = None) -> None:
    """The title band. Signed in, its right end carries the account and the mascot; the
    login screen shows neither, since it has the large mascot beside the form."""
    right = ""
    if user_name is not None:
        name = html.escape(user_name)
        src = _mascot_data_uri()
        mascot = f'<img class="uni-header-mascot" src="{src}" alt="UNISONIC mascot">' if src else ""
        right = (
            f'<div class="uni-account"><div class="uni-account-name" title="{name}">👤 {name}</div>'
            f'<div class="uni-account-role">{html.escape(role or "")}</div></div>{mascot}'
        )
    st.markdown(
        '<div class="uni-header"><div class="uni-header-side"></div>'
        f'<div class="uni-header-main"><div class="uni-header-title">{HEADER_TITLE}</div>'
        f'<div class="uni-header-sub">{HEADER_SUBTITLE}</div></div>'
        f'<div class="uni-header-side uni-header-right">{right}</div></div>',
        unsafe_allow_html=True,
    )


def render_footer() -> None:
    st.markdown(f'<div class="uni-footer">{FOOTER_TEXT}</div>', unsafe_allow_html=True)


def render_mascot() -> None:
    src = _mascot_data_uri()
    image = f'<img class="uni-mascot" src="{src}" alt="UNISONIC mascot">' if src else ""
    st.markdown(f'<div class="uni-mascot-wrap">{image}</div>', unsafe_allow_html=True)


# Runs in a same-origin component iframe and reaches into the page. Every step is inside
# try/catch: a Streamlit DOM change must cost only the scroll, never the chat.
_SCROLL_SCRIPT = """<script>
/* run __NONCE__ */
(function () {
  try {
    var doc = window.parent.document;
    var view = doc.defaultView;
    var stopAt = Date.now() + 1600;
    var userMoved = false;
    var scroller = null;
    var events = ["wheel", "touchstart", "pointerdown"];
    function onUser(event) { if (event.isTrusted) { userMoved = true; } }
    function findScroller() {
      var el = doc.querySelector(".st-key-uni-chatbox");
      for (; el && el !== doc.body; el = el.parentElement) {
        var overflow = view.getComputedStyle(el).overflowY;
        if (overflow === "auto" || overflow === "scroll") { return el; }
      }
      return null;
    }
    function finish() {
      try {
        if (scroller) {
          events.forEach(function (name) { scroller.removeEventListener(name, onUser); });
        }
      } catch (e) {}
    }
    function step() {
      try {
        if (userMoved) { finish(); return; }
        if (!scroller) {
          scroller = findScroller();
          if (scroller) {
            events.forEach(function (name) {
              scroller.addEventListener(name, onUser, { passive: true });
            });
          }
        }
        var bubbles = scroller ? scroller.querySelectorAll(".uni-user") : [];
        var latest = bubbles[bubbles.length - 1];
        if (latest) {
          var top = latest.getBoundingClientRect().top - scroller.getBoundingClientRect().top
                    + scroller.scrollTop - 10;
          var target = Math.max(0, Math.min(top, scroller.scrollHeight - scroller.clientHeight));
          if (Math.abs(scroller.scrollTop - target) > 2) { scroller.scrollTop = target; }
        }
      } catch (e) {}
      if (Date.now() < stopAt) { setTimeout(step, 120); } else { finish(); }
    }
    step();
  } catch (e) {}
})();
</script>"""


def render_scroll_to_latest(nonce: int) -> None:
    """Scroll the chat window so the newest question sits at its top.

    Otherwise a long answer (insight, a 420px chart, an expander) leaves the reader looking
    at its end, with the question and the insight scrolled out of view above. It keeps
    re-applying for ~1.5 s because plotly lays charts out after the page renders, and
    gives up at once if the reader scrolls. The nonce makes each call a new document, so the
    browser runs it again; the caller draws it only on a run that added a message.
    """
    components.html(_SCROLL_SCRIPT.replace("__NONCE__", str(int(nonce))), height=0)


def render_user_bubble(text: str) -> None:
    # Escaped, not rendered as markdown: it is the user's own words, shown verbatim. A raw
    # newline would let a blank line end the HTML block and spill the rest out as markdown.
    body = html.escape(text).replace("\n", "<br>")
    st.markdown(f'<div class="uni-user">{body}</div>', unsafe_allow_html=True)
