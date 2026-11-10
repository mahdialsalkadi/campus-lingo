"""LLM client: llama-server (llama.cpp) for structured challenge generation."""

from .generator import (
    EMBEDDING_DIM,
    MODEL_TIERS,
    LlamaCppClient,
    LlamaCppError,
    aclose_all_clients,
    get_client,
    get_tier,
    normalize_tier,
)

__all__ = [
    "EMBEDDING_DIM",
    "MODEL_TIERS",
    "LlamaCppClient",
    "LlamaCppError",
    "aclose_all_clients",
    "get_client",
    "get_tier",
    "normalize_tier",
]
