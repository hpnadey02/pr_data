"""Agent 1 - Query Understanding.

Implements the required rewriting pipeline stages:
  - Query Normalization      -> normalize_text()
  - Conversational Rewriting -> conversational_rewrite() (LLM, only if there's history)
  - Filter-Aware Rewriting   -> extract_filters() + extract_equality_filters()
  - Query Routing            -> classify_route()
  - Query Decomposition      -> decompose()

The important addition is deterministic identifier binding. A question like

    REFERENCE_NUMBER-2316239805635, POLICY_NO-1029156133, give USGI_SUM_INSURED

names its own columns and values. Previously that was left entirely to the LLM, which
filtered POLICY_NO's value on USGIpos_Policy_Number and returned zero rows. Those pairs
are now parsed here and resolved through the column registry (which ignores case, spaces
and underscores), then handed to the SQL model as facts rather than hints.
"""
import re
import time

from backend.agents.state import AgentState
from backend.core.column_registry import get_registry
from backend.core.date_windows import resolve_date_window
from backend.core.llm_client import LLMUnavailableError, chat
from backend.core.logging_config import get_logger
from config.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()

_ABBREVIATIONS = {
    r"\byoy\b": "year over year",
    r"\bqoq\b": "quarter over quarter",
    r"\bwow\b": "week over week",
    r"\bytd\b": "year to date",
    r"\bmtd\b": "month to date",
}

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

# "Top performing branches" with no number still has to mean something definite.
DEFAULT_TOP_N = 5

# Routes where an unstated N means "give me a shortlist". `top1_then_trend` is excluded on
# purpose: "which branch has the highest business" asks for exactly one, and forcing 5
# there would answer a different question.
_RANKING_ROUTES = ("ranking", "ranking_then_trend")

_TOP_N_RE = re.compile(r"\btop[\s-]?(\d+|" + "|".join(_NUMBER_WORDS) + r")\b", re.IGNORECASE)
_GRAIN_MAP = {
    "weekly": "week", "week wise": "week", "week-wise": "week",
    "monthly": "month", "month wise": "month", "month-wise": "month",
    "quarterly": "quarter", "quarter wise": "quarter",
    "daily": "day", "day wise": "day",
}
_CHART_HINT_RE = re.compile(
    r"\b(line chart|bar chart|pie chart|donut chart|scatter plot|scatter chart|table)\b",
    re.IGNORECASE,
)
_FOLLOWUP_HINTS = ("it", "that", "them", "those", "now show", "what about", "and for", "same for")

_ACTUAL_TARGET_RE = re.compile(r"\bactual\b.*\btarget\b|\btarget\b.*\bactual\b", re.IGNORECASE)
_DECOMPOSITION_RE = re.compile(r"\bcompare\b.*\band\b|\band\b.*\bidentify\b|generate separate charts", re.IGNORECASE)
_TREND_RE = re.compile(r"\btrend\b|\bweekly\b|\bmonthly\b|\bover time\b|\bquarterly\b", re.IGNORECASE)
_CONTRIBUTION_RE = re.compile(r"\bcontribution\b|\bshare\b|\bpercentage of\b", re.IGNORECASE)
_RANKING_RE = re.compile(r"\btop\b|\bhighest\b|\bhigh[\s-]perform|\bbest[\s-]perform|\brank", re.IGNORECASE)
_COMPARISON_RE = re.compile(r"\bcompare\b|\bversus\b|\bvs\.?\b", re.IGNORECASE)

# "REFERENCE_NUMBER-2316239805635", "POLICY_NO: 1029156133", "branch name = MUMBAI".
# The value runs to the next comma or end of string, so slash-bearing identifiers such as
# AVO/2316/20138002 and 2316/84507832/00/000 survive intact.
_PAIR_RE = re.compile(
    r"(?P<field>[A-Za-z][A-Za-z0-9_ ]{1,48}?)\s*(?:-|=|:|\bis\b|\bequals\b)\s*"
    r"(?P<value>'[^']*'|\"[^\"]*\"|[A-Za-z0-9][A-Za-z0-9_/\\.@-]*)",
)
# Phrases that introduce the OUTPUT column rather than a filter.
_REQUEST_RE = re.compile(
    r"(?:\bgive(?:\s+me)?\b|\bshow(?:\s+me)?\b|\bwhat\s+is(?:\s+the)?\b|\bfetch\b|\bget\b|"
    r"\bfind\b|\bdisplay\b|\breturn\b|\btell\s+me\b)\s+(?P<target>[A-Za-z][A-Za-z0-9_ ]{1,48})",
    re.IGNORECASE,
)
_STOP_PHRASES = {
    "the", "a", "an", "me", "us", "value", "data", "details", "detail", "record",
    "records", "row", "rows", "result", "results", "info", "information", "for", "of",
}


def normalize_text(question: str) -> str:
    q = " ".join(str(question or "").strip().split())
    for pattern, replacement in _ABBREVIATIONS.items():
        q = re.sub(pattern, replacement, q, flags=re.IGNORECASE)
    return q


def _looks_like_followup(question: str, chat_history: list[dict]) -> bool:
    """Only genuinely context-dependent questions get rewritten.

    The previous rule ("<= 8 words" OR a hint word) treated any short question as a
    follow-up. "sub inward no for policy no 1029156133" is seven words, so it was sent to
    the model for rewriting - which returned the PREVIOUS question, and the user got the
    previous answer. A question that carries its own identifier is self-contained by
    definition, so digits and resolvable column names now veto the rewrite.
    """
    if not chat_history:
        return False
    q_lower = question.lower()

    if any(hint in q_lower for hint in _FOLLOWUP_HINTS):
        return True
    if any(char.isdigit() for char in question):
        return False
    if len(question.split()) > 5:
        return False
    # A short question that already names a column stands on its own ("branch premium").
    registry = get_registry()
    if registry.columns and registry.resolve(question, allow_fuzzy=False):
        return False
    return True


# Scaffolding from the rewrite prompt. If any of it comes back, the model echoed the
# prompt instead of answering, and its output must be discarded.
_REWRITE_LEAKS = ("interpreted as", "standalone question", "prior conversation", "q1:", "q2:", "q3:")


def _rewrite_is_usable(rewritten: str, original: str) -> bool:
    lowered = rewritten.lower()
    if not rewritten.strip():
        return False
    if any(leak in lowered for leak in _REWRITE_LEAKS):
        return False
    # A rewrite that balloons the question is paraphrasing history, not resolving a pronoun.
    return len(rewritten) <= max(240, len(original) * 6)


def conversational_rewrite(question: str, chat_history: list[dict]) -> str:
    if not _looks_like_followup(question, chat_history):
        return question
    history_text = "\n".join(
        f"Q{i+1}: {h['question']} (interpreted as: {h['rewritten']})"
        for i, h in enumerate(chat_history[-3:])
    )
    system = (
        "You rewrite a short insurance-business follow-up question into a fully standalone "
        "question using the prior conversation for context. Output ONLY the rewritten "
        "question, no explanation, no quotes."
    )
    prompt = f"Prior conversation:\n{history_text}\n\nFollow-up question: {question}\n\nStandalone question:"
    # The small router model is enough for this and keeps short follow-ups fast.
    rewritten = chat(
        settings.router_model, system, prompt,
        temperature=settings.LLM_SQL_TEMPERATURE, num_predict=120,
    )
    rewritten = rewritten.strip().strip('"')
    if not _rewrite_is_usable(rewritten, question):
        logger.warning("Discarded an unusable conversational rewrite: %r", rewritten[:200])
        return question
    return rewritten


def _clean_value(raw: str) -> str:
    return str(raw or "").strip().strip("'\"").strip()


def _looks_like_filter_value(raw: str, quoted: bool) -> bool:
    """Distinguish a real filter value from ordinary hyphenated English.

    Without this, "High-performing branch region-wise" parses as
    field="branch region", value="wise" and produces a confusing "could not match that
    field" warning. A genuine identifier value is quoted, contains a digit, or is an
    upper-case code.
    """
    value = raw.strip()
    if not value:
        return False
    if quoted:
        return True
    if any(char.isdigit() for char in value):
        return True
    # ALLCAPS codes and names ("MUMBAI", "AVO/2316"), but not lowercase words ("wise").
    letters = [c for c in value if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters) and len(value) >= 2


def extract_equality_filters(question: str) -> tuple[dict[str, str], list[str]]:
    """Parse `COLUMN-value` / `COLUMN = value` pairs into {physical_column: value}.

    Returns (filters, unresolved_field_names). A field that does not resolve to exactly
    one column is reported rather than guessed - matching two columns to one shortcut is
    exactly the failure mode this design forbids.
    """
    registry = get_registry()
    filters: dict[str, str] = {}
    unresolved: list[str] = []
    if not registry.columns:
        return filters, unresolved

    for match in _PAIR_RE.finditer(question):
        field = match.group("field").strip(" ,.")
        raw_value = match.group("value")
        quoted = raw_value.startswith(("'", '"'))
        value = _clean_value(raw_value)
        if not field or not value:
            continue
        if not _looks_like_filter_value(value, quoted):
            continue
        # Trim leading connectives the regex may have swept up ("and POLICY_NO").
        field = re.sub(r"(?i)^(?:and|or|with|for|where|the)\s+", "", field).strip()
        if not field or field.lower() in _STOP_PHRASES:
            continue
        column = registry.resolve(field)
        if column:
            filters[column] = value
        elif "_" in field or len(field.split()) > 1:
            unresolved.append(field)
    return filters, unresolved


def extract_requested_columns(question: str, exclude: set[str]) -> list[str]:
    """Columns the question explicitly asks to SEE ("give USGI_SUM_INSURED")."""
    registry = get_registry()
    if not registry.columns:
        return []
    requested: list[str] = []
    for match in _REQUEST_RE.finditer(question):
        target = match.group("target").strip(" ,.")
        words = [w for w in target.split() if w.lower() not in _STOP_PHRASES]
        # Try the longest phrase first, then shrink - "me the USGI_SUM_INSURED" -> the column.
        for size in range(len(words), 0, -1):
            candidate = " ".join(words[:size])
            column = registry.resolve(candidate)
            if column and column not in exclude and column not in requested:
                requested.append(column)
                break
    return requested


def extract_filters(question: str) -> dict:
    filters: dict = {}
    q_lower = question.lower()

    top_match = _TOP_N_RE.search(question)
    if top_match:
        token = top_match.group(1).lower()
        filters["top_n"] = int(token) if token.isdigit() else _NUMBER_WORDS.get(token)

    for phrase, grain in _GRAIN_MAP.items():
        if phrase in q_lower:
            filters["date_grain"] = grain
            break

    chart_match = _CHART_HINT_RE.search(question)
    if chart_match:
        hint = chart_match.group(1).lower()
        filters["explicit_chart_type"] = {
            "line chart": "line", "bar chart": "bar", "pie chart": "pie",
            "donut chart": "donut", "scatter plot": "scatter", "scatter chart": "scatter",
            "table": "table",
        }[hint]

    return filters


def classify_route(question: str, filters: dict, equality_filters: dict | None = None) -> str:
    """Combines multiple signals (not just a first-match keyword list) so compound
    phrasing like "top five branches ... weekly trend" still routes correctly."""
    # An explicit identifier filter means a single-record lookup, whatever else the
    # sentence contains. Routing it as "aggregation" is what produced a bare
    # `SELECT TOP 1 [USGI_SUM_INSURED] FROM dbo.May_2` with no WHERE clause.
    if equality_filters:
        return "lookup"

    is_trend = bool(_TREND_RE.search(question))
    is_ranking = bool(_RANKING_RE.search(question)) or bool(filters.get("top_n"))

    if _ACTUAL_TARGET_RE.search(question):
        return "actual_vs_target"
    if _looks_decomposable(question):
        return "decomposition"
    if is_ranking and is_trend:
        return "ranking_then_trend" if (filters.get("top_n") or 1) > 1 else "top1_then_trend"
    if is_trend:
        return "trend"
    if _CONTRIBUTION_RE.search(question):
        return "contribution"
    if is_ranking:
        return "ranking"
    if _COMPARISON_RE.search(question):
        return "comparison"
    return "aggregation"


_LIST_RE = re.compile(r"^(.*?\bby\s+)([\w\s]+(?:,\s*[\w\s]+)*,?\s+and\s+[\w\s]+)$", re.IGNORECASE)
_WISE_RE = re.compile(r"\b[\w]+-wise\b", re.IGNORECASE)


def _looks_decomposable(question: str) -> bool:
    q = question.rstrip(". ")
    if _DECOMPOSITION_RE.search(q):
        return True
    if len(_WISE_RE.findall(q)) > 1:
        return True
    list_match = _LIST_RE.match(q)
    if list_match:
        items = [i for i in re.split(r",|\band\b", list_match.group(2), flags=re.IGNORECASE) if i.strip()]
        return len(items) > 1
    return False


def decompose(question: str) -> list[str]:
    """Splits a compound question into one sub-question per dimension so chart_agent can
    render a separate chart for each."""
    q = question.rstrip(". ")

    list_match = _LIST_RE.match(q)
    if list_match:
        prefix, list_part = list_match.groups()
        items = [i.strip() for i in re.split(r",|\band\b", list_part, flags=re.IGNORECASE) if i.strip()]
        if len(items) > 1:
            return [f"{prefix}{item}" for item in items]

    wise_terms = _WISE_RE.findall(q)
    if len(wise_terms) > 1:
        tail = _WISE_RE.sub("", q, count=1)
        tail = re.sub(r"^\s*and\s+[\w]+-wise\s*", "", tail, flags=re.IGNORECASE).strip()
        return [f"{term} {tail}".strip() for term in wise_terms]

    parts = re.split(r"\s+\band\b\s+", q, flags=re.IGNORECASE)
    parts = [p.strip(" ,.") for p in parts if len(p.strip(" ,.")) > 3]
    return parts if len(parts) > 1 else [question]


def query_understanding_node(state: AgentState) -> dict:
    t0 = time.time()
    warnings = list(state.get("warnings", []))
    raw = state["raw_question"]

    normalized = normalize_text(raw)
    rewritten = normalized

    # Parse identifiers BEFORE any rewriting. A question that names its own columns and
    # values is already standalone, so the follow-up rewrite is both unnecessary (it costs
    # a full LLM round-trip) and risky (a model paraphrasing "POLICY_NO-1029156133" can
    # corrupt the identifier). Only genuinely context-dependent questions are rewritten.
    equality_filters, unresolved = extract_equality_filters(normalized)

    if not equality_filters:
        try:
            rewritten = conversational_rewrite(normalized, state.get("chat_history", []))
        except LLMUnavailableError as exc:
            # Non-fatal: continue with the literal question rather than failing the request.
            warnings.append(f"Conversational rewriting skipped: {exc}")
        if rewritten != normalized:
            equality_filters, unresolved = extract_equality_filters(rewritten)

    requested_columns = extract_requested_columns(rewritten, exclude=set(equality_filters))

    if unresolved:
        registry = get_registry()
        details = "; ".join(
            f"'{field}'"
            + (f" (closest: {', '.join(registry.suggest(field))})" if registry.suggest(field) else "")
            for field in unresolved
        )
        warnings.append(
            f"Could not match {len(unresolved)} field name(s) in your question to a column: {details}"
        )

    filters = extract_filters(rewritten)
    route = classify_route(rewritten, filters, equality_filters)

    # Applied AFTER routing: classify_route reads top_n to tell "the single highest
    # branch" (top1_then_trend) from "the top branches" (ranking_then_trend). Defaulting
    # earlier would collapse that distinction.
    if route in _RANKING_ROUTES and not filters.get("top_n"):
        filters["top_n"] = DEFAULT_TOP_N

    sub_questions = decompose(rewritten) if route == "decomposition" else [rewritten]

    # Resolved to literal dates here so the SQL model is told the range rather than asked
    # to derive it. A wrong range produces a confidently wrong answer with no error.
    date_window = resolve_date_window(rewritten)
    if date_window:
        warnings.append(f"Date range applied: {date_window.label}.")

    logger.info(
        "query_understanding route=%s filters=%s equality=%s requested=%s "
        "date_window=%s sub_questions=%s",
        route, filters, equality_filters, requested_columns,
        date_window.label if date_window else None, sub_questions,
        extra={"request_id": state.get("request_id")},
    )

    return {
        "normalized_question": normalized,
        "rewritten_question": rewritten,
        "filters": filters,
        "equality_filters": equality_filters,
        "requested_columns": requested_columns,
        "date_window": date_window,
        "route": route,
        "sub_questions": sub_questions,
        "warnings": warnings,
        "timings_ms": {**state.get("timings_ms", {}), "query_understanding": round((time.time() - t0) * 1000, 1)},
    }
