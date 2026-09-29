"""Local ChromaDB persistent store used by the Schema & Example Retrieval agent.

Two collections, both indexed with cosine similarity (hnsw:space=cosine):
  - schema_metadata: one document per real dbo.May_2 column (name + business aliases +
    description), so the agent can map free-text business terms to actual columns.
  - example_queries: the 12 required domain example questions (+ any future curated
    examples), so similar past questions steer SQL generation via few-shot context.

Populated by scripts/setup_chromadb.py; read at query time by
backend/agents/schema_retrieval.py.
"""
import os

from backend.core.logging_config import get_logger
from config.settings import get_settings

settings = get_settings()

# Chroma's telemetry client is broken in 0.5.x ("capture() takes 1 positional argument but
# 3 were given") and logged an ERROR on every single collection access - thousands of lines
# of noise in logs/errors.log that buried the real failures. It must be disabled BEFORE
# chromadb is imported, because the flag is read at module import time.
# NOTE: the telemetry IMPL must be left at its default. Blanking it makes chromadb fail
# to resolve the component ("not enough values to unpack"). Disabling the flag is enough
# to stop the network call; the client that still initialises is silenced by
# backend/core/logging_config._quieten_noisy_libraries().
if not settings.CHROMA_TELEMETRY:
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

logger = get_logger(__name__)


def _native_runtime_is_healthy() -> str | None:
    """Return None if native extensions loaded cleanly, else why they did not.

    chromadb 0.5.x evaluates `DefaultEmbeddingFunction()` as a default argument while
    defining CollectionCommon, so importing chromadb always constructs ONNXMiniLM_L6_V2 -
    an embedder this app never uses, since every collection is created with
    ProviderEmbeddingFunction. If onnxruntime's native library cannot load, importing
    chromadb therefore fails outright.

    backend/__init__.py imports onnxruntime before anything can pull in pandas/pyarrow,
    which is what keeps that load working (see the note there for the DLL-ordering
    conflict). This function just reports the outcome.

    Stubbing onnxruntime out when it fails is NOT safe: the error means the DLL was mapped
    into the process and its initialiser then failed, leaving the process damaged. Doing so
    let startup succeed but segfaulted the worker (exit 139) on the first ChromaDB vector
    search, inside a different native library. Treating a failed native load as "ChromaDB
    is unavailable for this process" is the only sound response.

    Retrieval degrades rather than breaking: schema_retrieval falls back to lexical
    matching through the column registry, which resolves any column the user names or
    abbreviates - it is what answered every question while ChromaDB was down.
    """
    from backend import NATIVE_RUNTIME_ERROR

    if NATIVE_RUNTIME_ERROR:
        return NATIVE_RUNTIME_ERROR

    import importlib

    try:
        importlib.import_module("onnxruntime")
        return None
    except Exception as exc:  # noqa: BLE001 - ImportError, OSError and DLL errors alike
        return f"{type(exc).__name__}: {exc}"


CHROMA_IMPORT_ERROR: str | None = _native_runtime_is_healthy()

if CHROMA_IMPORT_ERROR:
    chromadb = None  # type: ignore[assignment]
    ChromaSettings = None  # type: ignore[assignment]
    ProviderEmbeddingFunction = None  # type: ignore[assignment]
    logger.error(
        "Native extension load failed (%s), so ChromaDB is disabled for this process. "
        "Semantic schema retrieval is off; lexical column matching will be used instead. "
        "This is usually memory pressure - free RAM, or lower OLLAMA_KEEP_ALIVE, and "
        "restart the backend.",
        CHROMA_IMPORT_ERROR,
    )
else:
    try:
        import chromadb  # noqa: E402 - must follow the telemetry environment flags
        from chromadb.config import Settings as ChromaSettings  # noqa: E402

        from backend.retrieval.embeddings import ProviderEmbeddingFunction  # noqa: E402
    except Exception as exc:  # noqa: BLE001 - any import failure degrades, never crashes
        chromadb = None  # type: ignore[assignment]
        ChromaSettings = None  # type: ignore[assignment]
        ProviderEmbeddingFunction = None  # type: ignore[assignment]
        CHROMA_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
        logger.error(
            "ChromaDB could not be imported (%s). Semantic schema retrieval is disabled; "
            "lexical column matching will be used instead.", CHROMA_IMPORT_ERROR,
        )

_client = None
_embed_fn = None


def is_available() -> bool:
    return chromadb is not None


def get_client():
    global _client
    if chromadb is None:
        raise RuntimeError(f"ChromaDB is unavailable: {CHROMA_IMPORT_ERROR}")
    if _client is None:
        path = str(settings.resolved(settings.CHROMA_PERSIST_DIR))
        _client = chromadb.PersistentClient(
            path=path,
            settings=ChromaSettings(anonymized_telemetry=settings.CHROMA_TELEMETRY),
        )
    return _client


def get_embed_fn():
    global _embed_fn
    if _embed_fn is None:
        _embed_fn = ProviderEmbeddingFunction()
    return _embed_fn


_collections: dict = {}


def _get_or_create(name: str):
    """Collections are cached: re-resolving one on every query was doing needless work on
    each retrieval call (and emitted a telemetry event per call)."""
    if name not in _collections:
        _collections[name] = get_client().get_or_create_collection(
            name=name, embedding_function=get_embed_fn(), metadata={"hnsw:space": "cosine"}
        )
    return _collections[name]


def _scoped(name: str) -> str:
    """Namespace a collection by the LLM provider that embedded it.

    Ollama's nomic-embed-text and Gemini's text-embedding-004 produce vectors in
    completely different spaces (and potentially different dimensions). Reusing one
    collection across both would either make Chroma reject the insert on a dimension
    mismatch or - worse - silently return meaningless nearest neighbours. Keeping them
    apart means switching LLM_PROVIDER is safe; you just re-run setup_chromadb.py once
    per provider.
    """
    return f"{name}__{settings.LLM_PROVIDER}"


def schema_collection_name() -> str:
    return _scoped(settings.CHROMA_SCHEMA_COLLECTION)


def examples_collection_name() -> str:
    return _scoped(settings.CHROMA_EXAMPLES_COLLECTION)


def schema_collection():
    return _get_or_create(schema_collection_name())


def examples_collection():
    return _get_or_create(examples_collection_name())


def reset_collection(name: str) -> None:
    """Drop a collection so it can be rebuilt from scratch (scripts/setup_chromadb.py --reset)."""
    _collections.pop(name, None)
    try:
        get_client().delete_collection(name)
    except Exception as exc:  # noqa: BLE001 - a missing collection is not an error here
        logger.info("Could not delete collection '%s' (it may not exist yet): %s", name, exc)


def collections_ready() -> tuple[bool, str]:
    if not is_available():
        return False, f"ChromaDB unavailable: {CHROMA_IMPORT_ERROR}"
    try:
        s, e = schema_collection().count(), examples_collection().count()
        if s == 0:
            return False, (
                f"'{schema_collection_name()}' is empty. Run scripts/setup_chromadb.py "
                f"(collections are per-provider, so each LLM_PROVIDER needs its own index)."
            )
        return True, f"{schema_collection_name()}={s} {examples_collection_name()}={e}"
    except Exception as exc:  # noqa: BLE001
        return False, f"ChromaDB unavailable: {exc}"


def query_schema(question: str, top_k: int | None = None) -> list[dict]:
    top_k = top_k or settings.CHROMA_TOP_K
    if not is_available():
        return []
    try:
        result = schema_collection().query(query_texts=[question], n_results=top_k)
    except Exception as exc:  # noqa: BLE001
        logger.error("Schema retrieval query failed: %s", exc)
        return []
    hits = []
    for doc, meta, dist in zip(
        result["documents"][0], result["metadatas"][0], result["distances"][0]
    ):
        hits.append({
            "column": meta.get("column_name"),
            "description": doc,
            "category": meta.get("category"),
            "similarity": round(1 - dist, 4),
        })
    return hits


def query_examples(question: str, top_k: int = 3) -> list[dict]:
    if not is_available():
        return []
    try:
        result = examples_collection().query(query_texts=[question], n_results=top_k)
    except Exception as exc:  # noqa: BLE001
        logger.error("Example retrieval query failed: %s", exc)
        return []
    hits = []
    for doc, meta, dist in zip(
        result["documents"][0], result["metadatas"][0], result["distances"][0]
    ):
        hits.append({"question": doc, "metadata": meta, "similarity": round(1 - dist, 4)})
    return hits
