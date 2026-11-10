"""Socratic AI tutor — multi-tier fallback router (master blueprint §6).

The tutor streams ONE Socratic clue per hint request. Historically this lived
inline in ``main.py``; it now lives here so the routing policy is testable in
isolation. The rules, unchanged:

* NEVER hardcode port 8081: :func:`get_active_client` probes the preferred
  tier (default 0.8B on 8081) first and transparently re-routes to whichever
  tier is actually healthy (2B -> 8082, 4B -> 8083, 9B -> 8084).
* Stream via SSE with ``temperature=0.3`` and ``max_tokens=150``.
* Socratic guidance only: hints and analogies, never the direct answer.
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Optional

from pydantic import BaseModel, Field

from llm.generator import LlamaCppError, get_active_client

# In-quiz Socratic tutor (POST /api/tutor/hint) prefers the ultra-fast 0.8B
# tier so a hint costs no meaningful latency, but falls back to whichever
# llama-server tier is actually healthy when Tier 1 is down. One hint = 10 gems.
TUTOR_TIER = os.getenv("TUTOR_TIER", "800M")
TUTOR_HINT_COST = int(os.getenv("TUTOR_HINT_COST", "10"))
TUTOR_MAX_TOKENS = int(os.getenv("TUTOR_MAX_TOKENS", "150"))
TUTOR_TEMPERATURE = float(os.getenv("TUTOR_TEMPERATURE", "0.3"))

TUTOR_SYSTEM_PROMPT = (
    "You are CampusTutor, a warm and witty academic mentor. Guide the student "
    "towards the answer using the Socratic method. Give a punchy, 2-sentence "
    "intuitive clue or analogy. NEVER give the direct answer. "
    "Reply in the language of the question (English slides -> English hint)."
)

# The drawer degrades to this canned nudge instead of a broken stream, so a
# llama-server hiccup never blocks the quiz session.
TUTOR_FALLBACK_NUDGE = (
    "تخيّل المفهوم كصورة بسيطة: ما الذي يتغيّر إذا عكسنا الفرض؟ ابدأ من التعريف نفسه."
)


class TutorHintRequest(BaseModel):
    """Body of ``POST /api/tutor/hint``."""

    challenge_id: Optional[str] = None
    question: str = Field("", max_length=4000)
    student_choice: Optional[str] = Field(None, max_length=1000)
    context: Optional[str] = Field(None, max_length=4000)
    user_id: Optional[str] = None


def encode_sse(event: dict[str, Any]) -> str:
    """Encode one server-sent-event line pair."""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def build_tutor_messages(
    question: str,
    student_choice: Optional[str] = None,
    context: Optional[str] = None,
) -> list[dict[str, str]]:
    """Compose the Socratic chat messages for one hint request."""
    parts = [f"Question:\n{question.strip()}"]
    if student_choice:
        parts.append(f"The student is leaning towards: {student_choice.strip()}")
    if context:
        parts.append(f"Lecture context:\n{context.strip()}")
    parts.append(
        "Give ONE Socratic clue (max 2 sentences) that nudges them without "
        "revealing the answer."
    )
    return [
        {"role": "system", "content": TUTOR_SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


async def stream_socratic_hint(
    messages: list[dict[str, str]],
    *,
    preferred_tier: Optional[str] = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield typed tutor stream events, re-routing to any healthy tier.

    Events: ``{"type": "open", "tier": ...}`` first, then
    ``{"type": "delta", "text": ...}`` per streamed token, then on failure
    ``{"type": "error"}`` + the canned nudge delta, and finally
    ``{"type": "done"}``.
    """
    # Multi-tier resilience: probe the preferred (Tier 1) endpoint first and
    # fall back to any healthy tier. get_active_client() streams from the
    # first tier that answers /health, so a hint streams even when only
    # Tier 3 (port 8083) is running.
    client, resolved_tier = await get_active_client(preferred=preferred_tier or TUTOR_TIER)
    yield {"type": "open", "tier": resolved_tier, "cost": TUTOR_HINT_COST}
    emitted = False
    try:
        async for delta in client.stream_chat(
            messages,
            max_tokens=TUTOR_MAX_TOKENS,
            temperature=TUTOR_TEMPERATURE,
        ):
            emitted = True
            yield {"type": "delta", "text": delta}
    except LlamaCppError as exc:
        logger = __import__("logging").getLogger(__name__)
        logger.warning("tutor stream failed: %s", exc)
        yield {"type": "error", "message": str(exc)}
        yield {"type": "delta", "text": TUTOR_FALLBACK_NUDGE}
        emitted = True
    if not emitted:
        # A reachable-but-silent model (empty completion, or a stream that closes
        # before the first token) would otherwise leave the drawer with no hint
        # at all. Always hand back the canned nudge so "💡 أحتاج تلميحاً" is
        # never an empty box.
        yield {"type": "delta", "text": TUTOR_FALLBACK_NUDGE}
    yield {"type": "done"}
