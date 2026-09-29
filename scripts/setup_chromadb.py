"""Builds/refreshes the two local ChromaDB collections used by the Schema & Example
Retrieval agent (cosine similarity search):

  1. schema_metadata   - one embedded document per REAL column of the configured table,
                          merging the live schema with the shortcut/alias metadata in
                          backend/knowledge/column_aliases.json.
  2. example_queries   - the curated domain example questions in data/example_queries.json,
                          used as few-shot retrieval anchors for SQL generation.

Two defects fixed here:

  * Documents were keyed by `name.strip().upper()` while data/schema_metadata.json used
    SPACE-separated keys ("BRANCH OFFICE CODE") and the real columns use underscores
    ("BRANCH_OFFICE_CODE"). Nothing ever matched, so every curated alias and description
    was silently discarded and the index held only "Column X (varchar)." placeholders.
    Lookup is now by compacted key, which ignores spaces, underscores and case.

  * The embedded document now includes the generated shortcuts, so a question using an
    abbreviation retrieves the column semantically as well as lexically.

Requires: the active data source reachable, and the active LLM provider able to embed -
Ollama running with OLLAMA_EMBED_MODEL pulled (`ollama pull nomic-embed-text`), or a valid
GEMINI_API_KEY when LLM_PROVIDER=gemini.

The index is written into a collection namespaced by provider, so switching LLM_PROVIDER
never mixes two embedding spaces. Re-run this script after switching.

Usage: python scripts/setup_chromadb.py [--reset]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.core.column_registry import get_registry  # noqa: E402
from backend.core.identifiers import compact  # noqa: E402
from backend.core.llm_client import check_llm  # noqa: E402
from backend.retrieval import chroma_store  # noqa: E402
from config.settings import get_settings  # noqa: E402

settings = get_settings()


def _load_legacy_metadata() -> dict:
    """data/schema_metadata.json, keyed by compacted name so its style no longer matters."""
    path = settings.resolved(settings.SCHEMA_METADATA_FILE)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle).get("columns", {})
    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARNING: could not read {path} ({exc}) - continuing without it.")
        return {}
    return {compact(name): entry for name, entry in raw.items()}


def build_schema_index(reset: bool = False):
    registry = get_registry(refresh=True)
    if not registry.columns:
        print("FAILED: the data source returned no columns. Run scripts/test_db_connection.py.")
        return False

    print(f"Live columns for {settings.DB_TABLE}: {len(registry.columns)}")
    legacy = _load_legacy_metadata()

    ids, documents, metadatas = [], [], []
    for info in registry.columns:
        legacy_entry = legacy.get(compact(info.name), {})
        aliases = list(dict.fromkeys([*info.aliases, *legacy_entry.get("aliases", [])]))
        description = info.description or legacy_entry.get("description") or f"Column {info.name}."
        shortcuts = registry.shortcuts_for(info.name)

        document = (
            f"{info.name}. "
            f"Aliases: {', '.join(aliases) if aliases else 'none'}. "
            f"Shortcuts: {', '.join(shortcuts[:12]) if shortcuts else 'none'}. "
            f"Type: {info.data_type} ({info.category}). "
            f"{description}"
        )
        ids.append(compact(info.name))
        documents.append(document)
        metadatas.append(
            {
                "column_name": info.name,
                "category": info.category,
                "data_type": info.data_type,
            }
        )

    target = chroma_store.schema_collection_name()
    collection = chroma_store.schema_collection()
    if reset:
        print(f"Clearing the existing schema collection '{target}' ...")
        chroma_store.reset_collection(target)
        collection = chroma_store.schema_collection()

    print(f"Embedding and upserting {len(ids)} columns into '{target}' ...")
    batch = 25
    for i in range(0, len(ids), batch):
        collection.upsert(
            ids=ids[i:i + batch],
            documents=documents[i:i + batch],
            metadatas=metadatas[i:i + batch],
        )
        print(f"  ... {min(i + batch, len(ids))}/{len(ids)}")
    print("Schema index ready.")
    return True


def build_examples_index():
    path = settings.resolved("./data/example_queries.json")
    if not path.exists():
        print(f"WARNING: {path} not found - skipping example index.")
        return
    with open(path, "r", encoding="utf-8") as handle:
        examples = json.load(handle)

    ids, documents, metadatas = [], [], []
    for i, example in enumerate(examples):
        ids.append(f"ex-{i}")
        documents.append(example["question"])
        metadatas.append({
            "route": example.get("route", ""),
            "dimensions": ",".join(example.get("dimensions", [])),
            "measure": example.get("measure", ""),
            "chart_type": example.get("chart_type", ""),
            "notes": example.get("notes", ""),
        })

    print(
        f"Embedding and upserting {len(ids)} example questions into "
        f"'{chroma_store.examples_collection_name()}' ..."
    )
    chroma_store.examples_collection().upsert(ids=ids, documents=documents, metadatas=metadatas)
    print("Example index ready.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="drop and rebuild the schema collection")
    args = parser.parse_args()

    print(f"Data source:  {settings.describe_source()}")
    print(f"LLM provider: {settings.describe_llm()}\n")

    ok, message = check_llm()
    if not ok:
        print(f"FAILED: {message}")
        return 1

    if not build_schema_index(reset=args.reset):
        return 1
    build_examples_index()
    print("\nDone. Restart the backend so /health/detailed reflects the refreshed index.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
