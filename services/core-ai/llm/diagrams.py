"""DIAGRAM_QA synthesis from extracted slide figures.

The vision-less path: we cannot feed the raster to the language model, so the
model is given the **text context around the figure** (the slide's own prose plus
its caption) and asked for comprehension questions that work when the diagram is
on screen — identifying components, sequencing data flow, reading a trend.

Two guarantees:

* the payload always validates as :class:`schema.DiagramQaChallenge`; and
* when no tier is reachable, a deterministic comprehension question is built
  from the chapter context, so a deck with figures still yields diagram practice.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from llm.generator import LlamaCppError, get_client
from schema import DiagramQaChallenge, json_schema_for

logger = logging.getLogger("campuslingo.diagrams")

DIAGRAM_TEMPERATURE = 0.3
DIAGRAM_MAX_TOKENS = 700

DIAGRAM_SYSTEM_PROMPT = (
    "You write exam-grade comprehension questions about a FIGURE from a "
    "university slide. You only receive the slide's surrounding text and the "
    "figure caption — never invent visual details that text cannot support. "
    "Ask about component roles, the order of a process/data flow, what a trend "
    "or axis means, or how two parts relate. Reply in the language of the "
    "source material. Exactly one option is correct; the others must be "
    "plausible but wrong."
)


def _clip(text: str, limit: int) -> str:
    cleaned = " ".join((text or "").split())
    return cleaned[:limit]


def fallback_diagram_challenge(
    *,
    image_url: str,
    diagram_ref: str,
    caption: str,
    context: str,
    distractors: Sequence[str] = (),
) -> dict[str, Any]:
    """Deterministic DIAGRAM_QA payload when no model tier is reachable.

    Builds a legitimate comprehension check — "which topic does this figure
    illustrate?" — using the chapter as the correct answer and *other* chapters
    of the same lecture as distractors, so the options are distinct and the
    answer is verifiable from the material.
    """
    topic = _clip(caption, 120) or _clip(context, 120) or "This concept"
    topic = topic.split(":")[-1].strip() or topic
    wrong = []
    for candidate in distractors:
        cleaned = _clip(candidate, 120)
        if cleaned and cleaned.lower() != topic.lower() and cleaned not in wrong:
            wrong.append(cleaned)
        if len(wrong) >= 3:
            break
    while len(wrong) < 2:
        wrong.append(f"Unrelated topic {len(wrong) + 1}")
    options = [{"text": topic, "correct": True}] + [
        {"text": text, "correct": False} for text in wrong[:3]
    ]
    return {
        "type": "DIAGRAM_QA",
        "image_url": image_url,
        "diagram_ref": diagram_ref,
        "caption": _clip(caption, 300),
        "questions": [
            {
                "question": "Which concept does this figure illustrate?",
                "options": options,
                "focus": "component_identification",
            }
        ],
        "explanation": f"The figure illustrates {topic}.",
    }


async def synthesize_diagram_challenge(
    *,
    image_url: str,
    diagram_ref: str,
    caption: str = "",
    context: str = "",
    distractors: Sequence[str] = (),
    tier: Optional[str] = None,
) -> dict[str, Any]:
    """Build a validated DIAGRAM_QA payload for one extracted figure.

    Tries the LLM (using the slide's text context), then falls back to the
    deterministic builder. Never raises: a figure without questions is worse
    than a simple question.
    """
    fallback = fallback_diagram_challenge(
        image_url=image_url,
        diagram_ref=diagram_ref,
        caption=caption,
        context=context,
        distractors=distractors,
    )
    if not context.strip() and not caption.strip():
        return fallback

    prompt = (
        f"Figure caption: {caption or '(none)'}\n"
        f"Slide and chapter text:\n{_clip(context, 1800) or '(none)'}\n\n"
        f"Refer to the figure as \"{caption or 'the figure'}\". "
        "Write 1-2 questions about what it shows."
    )
    try:
        client = get_client(tier)
        raw = await client.chat_json(
            prompt,
            json_schema_for(DiagramQaChallenge),
            system_prompt=DIAGRAM_SYSTEM_PROMPT,
            temperature=DIAGRAM_TEMPERATURE,
        )
        payload = dict(raw or {})
        payload.update(
            {
                "type": "DIAGRAM_QA",
                "image_url": image_url,
                "diagram_ref": diagram_ref,
                "caption": _clip(caption, 300) or payload.get("caption", ""),
            }
        )
        return DiagramQaChallenge.model_validate(payload).model_dump(mode="json")
    except (LlamaCppError, Exception) as exc:  # noqa: BLE001 - degrade, never fail
        logger.info("diagram generation fell back for %s: %s", diagram_ref, exc)
        return fallback
