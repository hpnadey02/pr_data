"""Embedding function for ChromaDB, backed by whichever LLM provider is active.

With LLM_PROVIDER=ollama this is nomic-embed-text running locally - no external API
calls, matching the on-prem restriction. With LLM_PROVIDER=gemini the document text is
sent to Google instead. Collections are namespaced per provider (see chroma_store) so the
two embedding spaces can never be mixed."""
from chromadb import Documents, EmbeddingFunction, Embeddings

from backend.core.llm_client import embed
from config.settings import get_settings

settings = get_settings()


class ProviderEmbeddingFunction(EmbeddingFunction):
    def __call__(self, input: Documents) -> Embeddings:  # noqa: A002 - chromadb's required name
        return [embed(settings.embed_model, text) for text in input]
