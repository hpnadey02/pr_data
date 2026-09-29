"""Agent 2 - Schema & Example Retrieval.

Grounds business terms ("business", "zone", "vertical") in the REAL column names of the
configured table before SQL generation.

Retrieval is hybrid, following the pattern proven in the Enterprise_DB_Chatbot project:

  * LEXICAL first - every column whose name or shortcut appears in the question is
    retrieved with certainty. Pure vector search is probabilistic, and a question that
    literally spells out "USGI_SUM_INSURED" must never have that column ranked out of the
    prompt by an embedding score.
  * SEMANTIC second - ChromaDB cosine search fills in the columns the user described
    rather than named.
  * PINNED third - columns bound by the deterministic filter parser (equality_filters,
    requested_columns) are always included regardless of score, because the SQL is
    required to reference them.

If ChromaDB or the embedding model is unavailable, lexical retrieval alone still produces
a usable column list, so a question can be answered with Ollama's embedder down.
"""
import re
import time

from backend.agents.state import AgentState
from backend.core.column_registry import get_registry
from backend.core.identifiers import compact, tokens
from backend.core.logging_config import get_logger
from backend.retrieval import chroma_store
from config.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()

MAX_COLUMNS_IN_PROMPT = 24
# Enough aliases to disambiguate, few enough that 24 columns still leave room for the rules.
MAX_ALIASES_IN_PROMPT = 6

_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "for", "by", "to", "in", "on", "is", "are",
    "was", "were", "give", "show", "me", "what", "which", "how", "many", "much",
    "with", "from", "top", "highest", "lowest", "best", "wise", "please", "get",
    "find", "list", "all", "total", "sum", "count", "average", "avg", "per",
}


def _phrases(question: str, max_words: int = 4) -> list[str]:
    """Contiguous word n-grams, longest first, so multi-word column names win."""
    words = [w for w in re.split(r"[^A-Za-z0-9_]+", question) if w]
    out: list[str] = []
    for size in range(min(max_words, len(words)), 0, -1):
        for start in range(len(words) - size + 1):
            out.append(" ".join(words[start:start + size]))
    return out


def lexical_columns(question: str) -> dict[str, float]:
    """{physical_column: score} for every column the question names or hints at.

    Scores sit above the 0..1 cosine band so a literal name always outranks a vector hit.
    """
    registry = get_registry()
    if not registry.columns:
        return {}

    scores: dict[str, float] = {}
    question_tokens = {t for t in tokens(question) if t not in _STOPWORDS}
    question_key = compact(question)

    for phrase in _phrases(question):
        if phrase.lower() in _STOPWORDS:
            continue
        column = registry.resolve(phrase, allow_fuzzy=False)
        if column:
            # Longer phrases are stronger evidence than single words.
            score = 2.0 + 0.1 * len(phrase.split())
            scores[column] = max(scores.get(column, 0.0), score)

    for info in registry.columns:
        if info.name in scores:
            continue
        # Whole compacted name appearing anywhere in the question ("usgisuminsured").
        if len(info.key) >= 6 and info.key in question_key:
            scores[info.name] = max(scores.get(info.name, 0.0), 1.8)
            continue
        overlap = question_tokens & set(tokens(info.name))
        if overlap:
            scores[info.name] = max(scores.get(info.name, 0.0), 1.0 + 0.15 * len(overlap))
    return scores


def _as_hit(name: str, similarity: float) -> dict:
    registry = get_registry()
    info = registry.get(name)
    return {
        "column": name,
        "description": (info.description if info else "") or f"Column {name}.",
        "category": info.category if info else "dimension",
        "data_type": info.data_type if info else "varchar",
        # The business words for this column. Shown to the SQL model so it can tell which
        # of two similar columns the question actually named.
        "aliases": (info.aliases[:MAX_ALIASES_IN_PROMPT] if info else []),
        "similarity": round(similarity, 4),
    }


def schema_retrieval_node(state: AgentState) -> dict:
    t0 = time.time()
    warnings = list(state.get("warnings", []))
    registry = get_registry()

    query = state["rewritten_question"]
    queries = [query, *state.get("sub_questions", [])]

    scored: dict[str, float] = {}

    # 1. Lexical - certain matches.
    for q in queries:
        for column, score in lexical_columns(q).items():
            scored[column] = max(scored.get(column, 0.0), score)

    # 2. Semantic - ChromaDB cosine similarity.
    semantic_ok = True
    for q in queries:
        hits = chroma_store.query_schema(q, top_k=settings.CHROMA_TOP_K)
        if not hits:
            semantic_ok = False
            continue
        for hit in hits:
            name = registry.resolve(hit.get("column") or "", allow_fuzzy=False) or hit.get("column")
            if not name:
                continue
            scored[name] = max(scored.get(name, 0.0), float(hit.get("similarity") or 0.0))

    # 3. Pinned - columns the SQL is contractually required to use.
    pinned = list((state.get("equality_filters") or {}).keys()) + list(
        state.get("requested_columns") or []
    )
    for name in pinned:
        scored[name] = max(scored.get(name, 0.0), 10.0)

    ranked = sorted(scored.items(), key=lambda kv: -kv[1])[:MAX_COLUMNS_IN_PROMPT]
    retrieved_columns = [_as_hit(name, score) for name, score in ranked]

    retrieved_examples = chroma_store.query_examples(query, top_k=3)

    if not retrieved_columns:
        warnings.append(
            "Schema index returned no matches (index may be empty). "
            "Run `python scripts/setup_chromadb.py` after confirming data-source connectivity."
        )
    elif not semantic_ok:
        # Lexical retrieval carried the request; say so rather than failing silently.
        logger.warning(
            "Semantic schema retrieval returned nothing; used lexical matching only.",
            extra={"request_id": state.get("request_id")},
        )

    logger.info(
        "schema_retrieval top_columns=%s pinned=%s",
        [c["column"] for c in retrieved_columns[:6]], pinned,
        extra={"request_id": state.get("request_id")},
    )

    return {
        "retrieved_columns": retrieved_columns,
        "retrieved_examples": retrieved_examples,
        "warnings": warnings,
        "timings_ms": {**state.get("timings_ms", {}), "schema_retrieval": round((time.time() - t0) * 1000, 1)},
    }
