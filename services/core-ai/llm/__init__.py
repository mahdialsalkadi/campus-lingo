"""Ollama chat/embedding client for structured challenge generation."""

from llm.generator import (
    EMBEDDING_DIM,
    OllamaClient,
    OllamaError,
    get_client,
)

__all__ = ["EMBEDDING_DIM", "OllamaClient", "OllamaError", "get_client"]
