"""Structured challenge generation with a local Qwen GGUF served by ``llama-server``.

Two entry points:

* :func:`generate_learning_bundle` — the primary path. One
  ``POST /v1/chat/completions`` call against the OpenAI-compatible API exposed
  by ``llama-server`` (default ``http://127.0.0.1:8080``), constrained by
  ``LearningUnitBundle.model_json_schema()``, produces a whole learning unit
  (concept title, difficulty level, 6-8 challenges) for a lecture chunk under a
  language-locked academic system prompt.
* :meth:`LlamaCppClient.generate_one` / :meth:`generate_for_node` — the fallback
  path: one constrained call per challenge type, run in parallel, each with its
  own repair loop. Used when the single bundle call fails validation.

Transport is a persistent :class:`httpx.AsyncClient` connection pool: repeated
calls during a multi-chunk ingestion reuse warm TCP connections instead of
paying a handshake per request. Decoding is double-constrained — a top-level
``json_schema`` object (converted to a GBNF grammar server-side) plus
``response_format: {"type": "json_object"}`` — so the model cannot emit
markdown chatter or prose. Business rules no JSON Schema can express
(a "wrong" token that occurs exactly once, one correct option per question,
per-type coverage) are enforced by Pydantic and fed back to the model as a
repair turn before a challenge is dropped.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import uuid
from typing import Any, AsyncIterator, Optional, Sequence

import httpx
from pydantic import ValidationError

from schema import (
    ALL_CHALLENGE_TYPES,
    BLANK_PLACEHOLDER,
    GENERATION_CHALLENGE_TYPES,
    PAYLOAD_MODEL_BY_TYPE,
    GeneratedChallenge,
    LearningUnitBundle,
    json_schema_for,
    normalize_blank,
    validate_payload,
)


def BLANK_PATTERN_SEARCH(text: str) -> bool:
    """True when the text already carries any blank-marker variant."""
    from schema import _BLANK_VARIANTS_RE

    return bool(_BLANK_VARIANTS_RE.search(text))


# Challenge kinds whose payload carries one fill-in blank. Both share the SAME
# normalisation/repair path, so a TAP_TO_FILL answer with a non-canonical
# placeholder (``____``, ``[blank]``, ``<blank>``, ``( )``) — or with no blank
# at all — is fixed deterministically instead of burning the 3x retry loop
# (~2 minutes per challenge) during ingestion.
CLOZE_LIKE_TYPES: frozenset[str] = frozenset({"CONTEXTUAL_CLOZE", "TAP_TO_FILL"})

# ---------------------------------------------------------------------------
# Model tiers — run.sh boots ONE llama-server PER SELECTED TIER of the Qwen 3.5
# family, each pinned to its own port (800M/2B/4B/9B -> 8081..8084), all
# hardware-tuned for the 14GB-VRAM BC-250: 16k context on a DEDICATED single
# stream (-np 1) so 100% of GPU compute and memory bandwidth focus on the
# active ingestion, with per-tier logical/physical batch sizes (-b/-ub). The
# backend picks the endpoint per request via the tier chosen at upload time;
# the model id stays the generic "default" so the client contract never
# changes when GGUFs change. Tier ports and GGUF paths are env-overridable
# (LLAMA_TIERn_PORT / LLAMA_TIERn_MODEL).
# ---------------------------------------------------------------------------

DEFAULT_TIER = os.getenv("SELECTED_MODEL_TIER", "4B")

MODEL_TIERS: dict[str, dict[str, Any]] = {
    "800M": {
        "id": "800M",
        "name": "Qwen 3.5 0.8B",
        "label": "Ultra Fast",
        "description": "High concurrency, instant quizzes",
        "port": int(os.getenv("LLAMA_TIER1_PORT", "8081")),
        "parallel_slots": 1,
        "context_size": 16384,
        "batch_size": 4096,
        "ubatch_size": 2048,
        "model_path": os.getenv("LLAMA_TIER1_MODEL", "models/qwen3.5-0.8b-instruct-q4_k_m.gguf"),
    },
    "2B": {
        "id": "2B",
        "name": "Qwen 3.5 2B",
        "label": "Balanced",
        "description": "Fast generation, lightweight",
        "port": int(os.getenv("LLAMA_TIER2_PORT", "8082")),
        "parallel_slots": 1,
        "context_size": 16384,
        "batch_size": 4096,
        "ubatch_size": 1024,
        "model_path": os.getenv("LLAMA_TIER2_MODEL", "models/qwen3.5-2b-instruct-q4_k_m.gguf"),
    },
    "4B": {
        "id": "4B",
        "name": "Qwen 3.5 4B (DeltaNet)",
        "label": "Sweet Spot",
        "description": "DeltaNet 3:1, high accuracy",
        "port": int(os.getenv("LLAMA_TIER3_PORT", "8083")),
        "parallel_slots": 1,
        "context_size": 16384,
        "batch_size": 4096,
        "ubatch_size": 1024,
        "model_path": os.getenv("LLAMA_TIER3_MODEL", "models/qwen3.5-4b-instruct-q4_k_m.gguf"),
    },
    "9B": {
        "id": "9B",
        "name": "Qwen 3.5 9B",
        "label": "Deep Reasoning",
        "description": "Heavy academic lectures",
        "port": int(os.getenv("LLAMA_TIER4_PORT", "8084")),
        "parallel_slots": 1,
        "context_size": 16384,
        "batch_size": 2048,
        "ubatch_size": 512,
        "model_path": os.getenv("LLAMA_TIER4_MODEL", "models/qwen3.5-9b-instruct-q4_k_m.gguf"),
    },
}

# CLI/env spelling -> canonical tier id ("1"/"800m"/"tiny" -> "800M", ...).
_TIER_ALIASES: dict[str, str] = {
    "1": "800M", "800m": "800M", "0.8b": "800M", "tiny": "800M",
    "2": "2B", "2b": "2B", "light": "2B",
    "3": "4B", "4b": "4B", "default": "4B", "sweet": "4B",
    "4": "9B", "9b": "9B", "heavy": "9B",
}


def normalize_tier(tier: Optional[str]) -> Optional[str]:
    """Map a user-supplied tier spelling to a canonical id; None when unknown."""
    if tier is None:
        return None
    return _TIER_ALIASES.get(str(tier).strip().lower())


def get_tier(tier: Optional[str] = None) -> dict[str, Any]:
    """Resolve a tier id/alias ('1'..'4', '800m', '9b', ...); unknown -> the default."""
    canonical = normalize_tier(tier) or (
        DEFAULT_TIER if DEFAULT_TIER in MODEL_TIERS else "4B"
    )
    return MODEL_TIERS[canonical]


def _base_url_for(tier: dict[str, Any]) -> str:
    """Endpoint for a tier: explicit LLAMA_SERVER_URL override wins (remote GPU
    host), otherwise the tier's own llama-server port from the registry."""
    override = os.getenv("LLAMA_SERVER_URL", "").strip()
    if override:
        return override.rstrip("/")
    return f"http://127.0.0.1:{tier['port']}"


# Default tier's OpenAI-compatible endpoint (kept for back-compat logging).
LLAMA_SERVER_URL = _base_url_for(
    MODEL_TIERS[DEFAULT_TIER if DEFAULT_TIER in MODEL_TIERS else "4B"]
)
# llama-server serves exactly the model given by -m; this name is decorative
# and only echoed back in the "model" field of requests.
# Generic model identifier: each llama-server serves whatever GGUF run.sh
# loaded for its tier, so the client contract never changes when GGUFs change.
LLAMA_MODEL = os.getenv("LLAMA_MODEL", "default")
LLAMA_EMBED_MODEL = os.getenv("LLAMA_EMBED_MODEL", "")

logger = logging.getLogger("campuslingo.llm")

EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1536"))

REQUEST_TIMEOUT = float(os.getenv("LLAMA_TIMEOUT", "300"))
EMBED_TIMEOUT = float(os.getenv("LLAMA_EMBED_TIMEOUT", "120"))
# Embeddings on the generative llama-server ports (8081-8084) are disabled by
# default: run.sh boots those servers for TEXT/JSON GENERATION ONLY (no
# --embeddings/--pooling — coupling them with speculative-generation flags
# aborts at startup, and an embeddings+pooling load slows the serving path).
# Set LLAMA_EMBEDDINGS_ENABLED=1 only for a dedicated embeddings server.
EMBEDDINGS_ENABLED = os.getenv("LLAMA_EMBEDDINGS_ENABLED", "0").strip().lower() not in (
    "0", "false", "no", "off",
)
MAX_ATTEMPTS = int(os.getenv("LLAMA_MAX_ATTEMPTS", "3"))
# Single-shot bundle attempts (default 0): small models tend to emit
# homogeneous challenge types across one long completion, so the single-shot
# path rarely passes coverage — going straight to per-type composition (tiny
# schemas, const-pinned types) is faster and deterministic. Set
# LLAMA_BUNDLE_ATTEMPTS=1+ to try the one-call bundle first (bigger models).
BUNDLE_ATTEMPTS = int(os.getenv("LLAMA_BUNDLE_ATTEMPTS", "0"))
MAX_SOURCE_CHARS = int(os.getenv("LLAMA_MAX_SOURCE_CHARS", "7000"))

# Ultra-low-latency sampling: greedy-leaning decoding for fast, deterministic
# structured output, with a slight escalation per repair attempt so retries
# explore new fixes.
TEMPERATURE = float(os.getenv("LLAMA_TEMPERATURE", "0.1"))
TOP_P = float(os.getenv("LLAMA_TOP_P", "0.85"))

# Qwen 3.5 is a thinking model: with the default chat template it spends the
# entire completion budget on ``reasoning_content`` and returns an EMPTY
# ``content`` (finish_reason=length). Structured output never wants that, so
# thinking is disabled per request via the template flag (LLAMA_ENABLE_THINKING=1
# restores it — then raise the token budgets to cover the reasoning).
ENABLE_THINKING = os.getenv("LLAMA_ENABLE_THINKING", "0").strip().lower() not in (
    "0", "false", "no", "off",
)
MAX_TOKENS_BUNDLE = int(os.getenv("LLAMA_MAX_TOKENS_BUNDLE", "8192"))
MAX_TOKENS_ONE = int(os.getenv("LLAMA_MAX_TOKENS_ONE", "2048"))

# Persistent connection pool: warm keepalive sockets eliminate the per-request
# handshake cost during multi-chunk fan-out generation.
POOL_KEEPALIVE_CONNECTIONS = int(os.getenv("LLAMA_POOL_KEEPALIVE", "8"))
POOL_MAX_CONNECTIONS = int(os.getenv("LLAMA_POOL_MAX_CONNECTIONS", "16"))

DEFAULT_BUNDLE_MODEL = LLAMA_MODEL

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

# The one rule that stops language drift: output language == source language.
LANGUAGE_LOCK_RULE = (
    "CRITICAL LANGUAGE DIRECTIVE: "
    "1. Detect the primary language of the source text. "
    "2. You MUST produce all titles, concept summaries, questions, options, and "
    "explanations in the EXACT SAME LANGUAGE as the source text. "
    "3. If the input slides are in English, every single word of your JSON output "
    "must be in English. NEVER translate English source content into Arabic."
)

PEDAGOGY_RULE = (
    "PEDAGOGICAL COVERAGE (content-adaptive, no fixed quota): "
    "- Generate a comprehensive set of challenges that naturally covers every "
    "critical definition, theorem, and mechanism introduced in this section. "
    "Do not artificially pad or truncate the questions to hit an arbitrary number. "
    "- Scale with real density: a short summary section yields a handful of focused "
    "challenges (6-10); a detailed technical section yields many in-depth ones (16-24). "
    "- Cover every definition, formula, mechanism, diagram explanation, and edge "
    "case present in the slides — at least one challenge each. "
    "- Distribute questions evenly across all presented concepts in the chunk. "
    "- Mix the high-yield variants: conceptual recall via multiple choice (MCQ_QUIZ), "
    "deep comprehension via planted-error statements (SPOT_MISTAKE), critical "
    "mechanics and formulas via fill-in-the-blank (CONTEXTUAL_CLOZE), rapid "
    "terminology association via term-definition pairing (MATCH_MADNESS), plus the "
    "micro-games for short attention spans: swipe-judgement (SWIPE_BOOLEAN), "
    "one-false-among-three-truths (SPOT_IMPOSTER) and one-touch cloze (TAP_TO_FILL). "
    "Chat/dialogue challenges (INTERACTIVE_CHAT / DIALOGUE / INTERACTIVE_STORY) are "
    "retired — never emit them."
)

MASTERY_DIRECTIVE = (
    "You are an expert academic curriculum designer. Your goal is exhaustive mastery. "
    "Generate a comprehensive set of challenges that naturally covers every critical "
    "definition, theorem, and mechanism introduced in this section. Do not "
    "artificially pad or truncate the questions to hit an arbitrary number. Extract "
    "and test EVERY critical concept, mechanism, and fact from the provided slides "
    "so a student solving these challenges masters 100% of the lecture material."
)

TITLE_RULE = (
    "CRITICAL TITLE RULE: The title of each Part/Node MUST be a descriptive "
    "conceptual topic (e.g. 'Pre-training Methodology', 'Evaluation & Results', "
    "'Maritime Domain Adaptation'). NEVER, UNDER ANY CIRCUMSTANCES, use table "
    "headers ('TABLE I', 'Table 2'), figure captions ('Figure 3'), raw numbers, "
    "graph coordinates/axes (e.g. '0.30 0.45 accuracy'), or slide numbers as node "
    "titles. If a section starts with a table or benchmark, name the node based on "
    "what the table represents (e.g. 'Performance Benchmarks & Metrics'). "
    "Do NOT output titles with spaces between individual letters (never 'C o u r s e' "
    "or 'L e c t u r e'). Normalize every title into standard, legible English or "
    "Arabic words."
)

BUNDLE_SYSTEM_PROMPT = f"""{MASTERY_DIRECTIVE} Convert lecture materials into structured, engaging challenges following the exact JSON schema provided.

{LANGUAGE_LOCK_RULE}

{PEDAGOGY_RULE}

{TITLE_RULE}

Strict rules:
1. Scientific accuracy first: never invent information that is not in the text, never use a \
term from outside it, and choose central ideas rather than marginal details.
2. Keep technical terms, symbols and units exactly as they appear in the source text.
3. Add a light touch of academic humour in the feedback and explanations — a witty remark or a \
funny comparison — but never trivialise the scientific content or mock the student.
4. The 'mistake_token' field must contain the exact wrong substring as it appears in \
code_or_text, occurring exactly once in that text.
5. 'sentence_with_blank' must contain the blank written exactly as _____.
6. Every question must have exactly one correct option: set "correct": true on \
EXACTLY ONE option and "correct": false on every other option.
7. Vary the challenge types inside the unit across the four high-yield variants: \
multiple choice, spot-the-mistake, cloze, and matching — every one of the four \
types must appear at least once. NEVER emit chat/dialogue (INTERACTIVE_STORY) \
or true/false challenges.

Answer with a single JSON object matching the given schema — no extra prose, no markdown fences."""


BUNDLE_PROMPT_TEMPLATE = """Lecture text:

--- BEGIN LECTURE TEXT ---
{lecture_text}
--- END LECTURE TEXT ---

Required output (every string in the SAME language as the lecture text above):
- concept_title: a short, DESCRIPTIVE conceptual topic for this text (about 2-6 words, NO \
numbering and no "Chapter"/"Unit" prefix — the platform numbers nodes itself). It must NEVER be a \
table header ('TABLE I'), a figure caption ('Figure 3'), raw numbers, graph coordinates/axes \
('0.30 0.45 accuracy') or a slide number. If the section opens with a table or benchmark, name \
the concept after what it represents (e.g. "Performance Benchmarks & Metrics"). NEVER output \
a title with spaces between individual letters ('C o u r s e') — normalize every title into \
standard, legible English or Arabic words.
- difficulty_level: 1 (introductory) to 5 (advanced) according to the depth of the text.
- challenges: as many varied challenges as the material naturally demands — generate a \
comprehensive set that covers every critical definition, theorem and mechanism, and do NOT \
artificially pad or truncate to hit an arbitrary number (a short summary section yields \
about 6-10; a dense technical section about 16-24). NEVER leave an important concept \
untested. Every challenge MUST set its "type" field to EXACTLY one of these four strings: \
"MCQ_QUIZ", "SPOT_MISTAKE", "CONTEXTUAL_CLOZE", "MATCH_MADNESS" — and every one of \
the four types must appear at least once.

Return ONE JSON object only."""


_TYPE_BRIEFS: dict[str, str] = {
    "MATCH_MADNESS": (
        "Build a tap-to-pair drill. Each pair maps a key TERM from the lecture to the ONE definition "
        "that means it. Terms must be distinct, definitions must be distinct and self-contained, and a "
        "definition must never repeat its own term. Use 4 to 6 pairs. 'one_liner_rationale' is ONE "
        "punchy sentence (max 200 chars) tying the terms together — the memory hook, not a summary."
    ),
    "SPOT_MISTAKE": (
        "Write ONE sentence, formula or short code block about the lecture that contains exactly one "
        "factual error (a swapped definition, an inverted sign, a wrong cause/effect, a wrong value). "
        "'code_or_text' holds the full sentence; 'mistake_token' is the exact wrong substring as it "
        "appears in that text (must appear exactly once); 'explanation' says why it is wrong and what "
        "the correct version is. 'one_liner_rationale' is ONE punchy sentence (max 200 chars) "
        "naming the fix — a student should grasp it in two seconds."
    ),
    "CONTEXTUAL_CLOZE": (
        "Write ONE sentence taken from the lecture with ONE key term REMOVED and replaced "
        "by exactly five underscores _____ . Example format: 'The mitochondria is the _____ "
        "of the cell.' The five-underscore blank _____ MUST appear in 'sentence_with_blank' — "
        "a sentence without the literal _____ blank is INVALID. 'correct_answer' is the removed "
        "term, and provide 2 to 4 plausible distractors from the same topic area that are NOT "
        "the answer. Add a short 'explanation'. 'one_liner_rationale' is ONE punchy sentence "
        "(max 200 chars) saying why the answer fits. "
        "Variation: vary the blank position across a unit and avoid reusing the same sentence skeleton."
    ),
    "MCQ_QUIZ": (
        "Write ONE classic multiple-choice question about a central fact from the lecture. "
        "'question' asks about one definition, mechanism or comparison; provide 3 to 4 'options' "
        "each with 'text' and 'correct'. Mark EXACTLY ONE option with correct=true and make "
        "the distractors plausible but clearly wrong to someone who studied the text. "
        "'explanation' justifies the correct choice in one or two sentences. 'one_liner_rationale' "
        "is ONE punchy sentence (max 200 chars) a student sees when they pick wrong — no paragraphs."
    ),
    "TRUE_FALSE": (
        "Write ONE statement that is either clearly true or clearly false according to the "
        "lecture. 'statement' is a single declarative sentence; 'answer' is the boolean truth of "
        "the statement (true = correct as written, false = the statement contains an error). "
        "Roughly alternate true and false statements across a unit. 'explanation' explains the "
        "verdict, correcting the error when the statement is false. 'one_liner_rationale' is ONE "
        "punchy sentence (max 200 chars) — the takeaway, not an essay."
    ),
    "SWIPE_BOOLEAN": (
        "Write ONE crisp conceptual statement (max 140 characters) that a student can judge in "
        "3 seconds: either clearly true or clearly false per the lecture. 'statement' is a "
        "single declarative sentence; 'answer' is its boolean truth; 'one_liner_rationale' is "
        "ONE punchy sentence correcting the error when the statement is false (or confirming "
        "the key fact when true). Roughly alternate true and false across a unit."
    ),
    "SPOT_IMPOSTER": (
        "Write FOUR short factual statements (max 200 characters each) about ONE concept from "
        "the lecture: three must be TRUE as written and exactly one must be a SUBTLE IMPOSTER — "
        "the same kind of statement with a quietly swapped term, inverted relationship or "
        "altered quantity, never an absurd lie. Set 'is_imposter': true on the false one only. "
        "'one_liner_rationale' is ONE punchy sentence exposing the imposter's flaw."
    ),
    "TAP_TO_FILL": (
        "Write ONE short sentence (max 240 characters) taken from the lecture with ONE key term "
        "REMOVED and replaced by exactly five underscores _____ . The blank _____ MUST appear in "
        "'sentence_with_blank'. 'correct_answer' is the removed term (max 80 chars) and provide "
        "EXACTLY 2 plausible distractors from the same topic area. 'one_liner_rationale' is ONE "
        "punchy sentence saying why the answer fits. Vary blank positions across a unit."
    ),
    "BOSS_FIGHT": (
        "Synthesize a chapter-culminating boss battle: 6 to 10 rapid-fire questions that TOGETHER "
        "span the whole material (definitions, mechanisms, comparisons, edge cases), each a "
        "short question with 2 to 4 options and exactly one correct. 'boss_name' is a playful "
        "chapter boss title; 'one_liner_rationale' is a short victory line. Never repeat a "
        "question."
    ),
}

_SYSTEM_PROMPT = (
    BUNDLE_SYSTEM_PROMPT
    + "\n\nYou are generating a SINGLE challenge object for one of the six challenge types below. "
    "Every string you emit MUST be in the same language as the lecture text."
)


_USER_TEMPLATE = """Lecture node: {title}

--- BEGIN LECTURE TEXT ---
{content}
--- END LECTURE TEXT ---

Task: {brief}

Return ONLY the JSON object for this task, in the same language as the lecture text."""


class LlamaCppError(RuntimeError):
    """Any transport, HTTP, or decoding failure talking to llama-server."""


# Backward-compat alias for code written against the old engine name.
OllamaError = LlamaCppError


# ---------------------------------------------------------------------------
# Decoding helpers
# ---------------------------------------------------------------------------


def build_schema(challenge_type: str) -> dict[str, Any]:
    """Self-contained JSON Schema for one challenge payload model."""
    model = PAYLOAD_MODEL_BY_TYPE[challenge_type]
    schema = json_schema_for(model)
    props = schema.setdefault("properties", {})
    props["type"] = {"type": "string", "const": challenge_type, "enum": [challenge_type]}
    schema["required"] = sorted(props.keys())
    schema["additionalProperties"] = False
    return schema


def build_bundle_schema() -> dict[str, Any]:
    """``LearningUnitBundle.model_json_schema()`` with refs inlined + object closed."""
    schema = json_schema_for(LearningUnitBundle)
    schema["additionalProperties"] = False
    # Pydantic's tagged-union leaves "type" optional (it has a default), so the
    # grammar would let the model omit it — and validation then collapses every
    # challenge into the first branch. Pin it: each anyOf branch must emit its
    # own const "type".
    for branch in (
        schema.get("properties", {}).get("challenges", {})
        .get("items", {}).get("anyOf", [])
    ):
        if isinstance(branch, dict) and "type" in branch.get("properties", {}):
            required = branch.setdefault("required", [])
            if "type" not in required:
                required.append("type")
    return schema


def _extract_json(text: str) -> dict[str, Any]:
    """Parse a model reply into a dict, tolerating stray fences/prose."""
    if not text or not text.strip():
        raise LlamaCppError("model returned an empty response")
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise LlamaCppError(f"no JSON object in model response: {text[:200]!r}")
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LlamaCppError(f"malformed JSON from model: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LlamaCppError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def _dummy_embedding(seed_text: str = "", dim: int = EMBEDDING_DIM) -> list[float]:
    """Deterministic, L2-normalised placeholder vector for a failed embedding.

    Embeddings are a retrieval nicety, never a hard dependency, so a 500 from
    ``/v1/embeddings`` (server started without ``--embeddings``/``--pooling``,
    oversized input, OOM, timeout) must not interrupt ingestion. Returning a
    unit vector keeps every downstream write valid — the column is
    ``vector(1536)`` and cosine distance stays finite — while the text hash
    keeps distinct chunks distinct instead of collapsing them onto one point.
    """
    if dim <= 0:
        return []
    digest = hashlib.sha256((seed_text or "campuslingo").encode("utf-8")).digest()
    values = [(digest[index % len(digest)] / 255.0) - 0.5 for index in range(dim)]
    norm = sum(value * value for value in values) ** 0.5 or 1.0
    return [value / norm for value in values]


def _fit_dim(vector: Sequence[float], dim: int = EMBEDDING_DIM) -> list[float]:
    """Truncate/zero-pad an embedding to the schema's fixed vector(1536)."""
    values = [float(v) for v in vector]
    if not values:
        raise LlamaCppError("embedding model returned an empty vector")
    if len(values) >= dim:
        values = values[:dim]
    else:
        values = values + [0.0] * (dim - len(values))
    norm = sum(v * v for v in values) ** 0.5
    if norm > 0:
        values = [v / norm for v in values]
    return values


def _describe_validation_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ())) or "payload"
        return f"{location}: {first.get('msg', str(exc))}"
    return str(exc)


def _normalize_cloze_blank(payload: dict[str, Any]) -> dict[str, Any]:
    """Rewrite any blank-marker variant (``____``, ``[blank]``, ``<blank>``) to
    the canonical ``_____`` with a single regex BEFORE Pydantic validation.

    Deterministic and free, so a model that emits ``____`` never burns the 3x
    rejection/repair loop (~2 minutes per cloze) during ingestion.
    """
    if str(payload.get("type") or "").upper() in CLOZE_LIKE_TYPES:
        sentence = payload.get("sentence_with_blank")
        if isinstance(sentence, str):
            payload["sentence_with_blank"] = normalize_blank(sentence)
    return payload


def _repair_cloze_blank(payload: dict[str, Any]) -> dict[str, Any]:
    """Inject the missing ``_____`` into a cloze payload that has none.

    Small models routinely answer with a complete sentence (the term left in
    place, no blank anywhere), which the schema rightly rejects. When the
    payload carries the removed term as ``correct_answer``, the fix is
    deterministic: replace that term's first occurrence with the canonical
    blank instead of burning 3 repair turns asking the model to do it.
    """
    if str(payload.get("type") or "").upper() not in CLOZE_LIKE_TYPES:
        return payload
    sentence = payload.get("sentence_with_blank")
    answer = str(payload.get("correct_answer") or "").strip()
    if not isinstance(sentence, str) or not answer:
        return payload
    if BLANK_PATTERN_SEARCH(sentence):
        return payload
    # Case-insensitive first-occurrence replacement of the term by the blank.
    repaired, count = re.subn(re.escape(answer), BLANK_PLACEHOLDER, sentence, count=1, flags=re.IGNORECASE)
    if count:
        payload["sentence_with_blank"] = repaired
    return payload


def _target_challenge_count(content_chars: int) -> int:
    """Content-adaptive challenge target for one chunk (NO rigid 18/18 clamp).

    The count follows the material's actual density instead of a hardcoded
    quota: a short 10-slide summary lands at 6-10 focused challenges, a detailed
    40-slide technical lecture at 16-24 in-depth ones. The 24 ceiling mirrors
    ``LearningUnitBundle.challenges`` max_length — a runaway-loop guard, not a
    pedagogical target.
    """
    if content_chars < 1200:
        return 6
    if content_chars < 2500:
        return 8
    if content_chars < 4000:
        return 10
    if content_chars < 5500:
        return 12
    if content_chars < 7000:
        return 16
    if content_chars < 8500:
        return 20
    return 24


def _allocate_variants(total: int, type_count: int) -> list[int]:
    """Spread ``total`` challenges across ``type_count`` types (near-equal).

    Unlike the old fixed per-type cap, the remainder is distributed so the
    total hits the density target exactly (e.g. 10 over 6 types -> 2,2,2,2,1,1).
    """
    type_count = max(1, type_count)
    base, extra = divmod(max(1, total), type_count)
    return [base + (1 if index < extra else 0) for index in range(type_count)]


def _estimate_difficulty(challenge_type: str, payload: dict[str, Any]) -> float:
    """Heuristic 0..1 difficulty from payload breadth (cold-start prior)."""
    if challenge_type == "MATCH_MADNESS":
        raw = (len(payload.get("pairs", [])) - 4) / 6.0 + 0.15
    elif challenge_type == "SPOT_MISTAKE":
        raw = 0.3 + min(0.35, len(payload.get("code_or_text", "")) / 2400.0)
    elif challenge_type == "CONTEXTUAL_CLOZE":
        raw = 0.2 + len(payload.get("distractors", [])) / 12.0
    elif challenge_type == "MCQ_QUIZ":
        raw = 0.15 + (len(payload.get("options", [])) - 2) / 8.0
    elif challenge_type == "TRUE_FALSE":
        raw = 0.2
    elif challenge_type == "SWIPE_BOOLEAN":
        raw = 0.2
    elif challenge_type == "SPOT_IMPOSTER":
        raw = 0.35
    elif challenge_type == "TAP_TO_FILL":
        raw = 0.2 + len(payload.get("distractors", [])) / 12.0
    elif challenge_type == "BOSS_FIGHT":
        raw = 0.45 + min(0.3, len(payload.get("questions", [])) / 40.0)
    else:
        raw = 0.3
    return round(min(1.0, max(0.05, raw)), 3)


# ---------------------------------------------------------------------------
# Chapter-culmination boss synthesis
# ---------------------------------------------------------------------------

BOSS_EMOJIS = ("🐲", "👹", "🧟", "🦂", "🐙", "🦖", "👿", "🤖")
BOSS_NAMES = (
    "حارس المفاهيم",
    "وحش منحنى النسيان",
    "طاغية الفصل",
    "سيد الامتحان",
    "عبس المراجعة النهائية",
)


def _mcq_options_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize any MCQ-like payload's options into [{text, correct}].

    Dedupes case-insensitively and guarantees exactly one correct option, so a
    sloppy source payload can never fail the boss validator.
    """
    raw_options = payload.get("options")
    options: list[dict[str, Any]] = []
    if isinstance(raw_options, list) and raw_options:
        for option in raw_options:
            if not isinstance(option, dict):
                continue
            text = str(option.get("text") or "").strip()
            if not text:
                continue
            options.append({"text": text[:200], "correct": bool(option.get("correct"))})
    if not options:
        # CONTEXTUAL_CLOZE / TAP_TO_FILL shape: answer + distractors -> options.
        answer = str(payload.get("correct_answer") or "").strip()
        if answer:
            options.append({"text": answer[:200], "correct": True})
            for distractor in payload.get("distractors") or []:
                text = str(distractor or "").strip()
                if text:
                    options.append({"text": text[:200], "correct": False})
    if not options:
        return []
    # Dedupe by lowercased text, keeping the first occurrence; if the correct
    # one was the duplicate that got dropped, promote the first survivor.
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for option in options:
        key = option["text"].lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(option)
    if not any(option["correct"] for option in unique):
        unique[0]["correct"] = True
    correct_count = sum(1 for option in unique if option["correct"])
    if correct_count > 1:  # keep only the first correct
        fixed_first = False
        for option in unique:
            if option["correct"]:
                if fixed_first:
                    option["correct"] = False
                else:
                    fixed_first = True
    return unique


def build_chapter_boss_fight(
    node_title: str,
    payloads: Sequence[dict[str, Any]],
    *,
    max_questions: int = 10,
) -> Optional[GeneratedChallenge]:
    """Synthesize the chapter-culmination BOSS_FIGHT from existing challenges.

    The spec wants a 10-question rapid-fire finale per chapter with a 15-second
    per-question countdown and a depleting HP bar. Rather than burn a GPU batch
    on a tenth duplicated pass, the boss RE-USES the strongest questions the
    deck already produced (MCQ options verbatim; cloze answers turned into
    options), deduplicated and trimmed to ``max_questions``. Deterministic,free,
    and always on-topic. Returns ``None`` when the material is too thin (<4
    usable questions) so thin lectures don't get a fake boss.
    """
    seen_prompts: set[str] = set()
    questions: list[dict[str, Any]] = []
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        challenge_type = str(payload.get("type") or "")
        if challenge_type in ("BOSS_FIGHT", "DIAGRAM_QA", "MATCH_MADNESS"):
            continue
        if challenge_type == "SPOT_IMPOSTER":
            # Each imposter statement becomes a true/false-style pick.
            statements = payload.get("statements") or []
            imposter = next(
                (s for s in statements if isinstance(s, dict) and s.get("is_imposter")), None
            )
            if not isinstance(imposter, dict):
                continue
            prompt = str(imposter.get("text") or "").strip()
            key = prompt.lower()
            if not prompt or key in seen_prompts:
                continue
            seen_prompts.add(key)
            questions.append(
                {
                    "question": (f"أي العبارات حفّة كاذبة: «{prompt[:220]}»؟")[:240],
                    "options": [
                        {"text": str(imposter.get("text") or "")[:200], "correct": True},
                        {"text": "عبارة صحيحة من الفصل", "correct": False},
                    ],
                }
            )
            continue
        # MCQ_QUIZ / CONTEXTUAL_CLOZE / TAP_TO_FILL / SPOT_MISTAKE / TRUE_FALSE
        prompt = str(
            payload.get("question")
            or payload.get("sentence_with_blank")
            or payload.get("code_or_text")
            or payload.get("statement")
            or ""
        ).strip()
        options = _mcq_options_from_payload(payload)
        if not prompt or not options:
            continue
        key = prompt.lower()
        if key in seen_prompts:
            continue
        seen_prompts.add(key)
        questions.append({"question": prompt[:240], "options": options[:4]})

    if len(questions) < 4:
        return None

    # Interleave option order safety: the boss keeps each source question's
    # single correct option, asserted again here by the payload validator.
    payload = {
        "type": "BOSS_FIGHT",
        "instructions": "معركة الزعيم: أفرغ شريط صحته قبل نهاية الوقت!",
        "boss_name": f"{node_title[:40]} — {BOSS_NAMES[sum(len(q) for q in questions) % len(BOSS_NAMES)]}",
        "boss_emoji": BOSS_EMOJIS[len(questions) % len(BOSS_EMOJIS)],
        "seconds_per_question": 15,
        "questions": questions[:max_questions],
        "one_liner_rationale": "سقط الزعيم! هذه الأسئلة كانت خلاصة الفصل كله.",
    }
    try:
        challenge_payload = validate_payload("BOSS_FIGHT", payload)
    except Exception:  # noqa: BLE001 - a thin/degenerate pool must never crash ingestion
        return None
    return GeneratedChallenge(
        challenge_type="BOSS_FIGHT",
        difficulty=0.75,
        payload=challenge_payload,
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LlamaCppClient:
    """Thin async wrapper over llama-server's OpenAI-compatible HTTP API."""

    def __init__(
        self,
        base_url: str = LLAMA_SERVER_URL,
        model: str = LLAMA_MODEL,
        embed_model: str = LLAMA_EMBED_MODEL,
        timeout: float = REQUEST_TIMEOUT,
        tier: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        # llama-server embeds with the model it was started with (--embeddings);
        # an explicit LLAMA_EMBED_MODEL only renames the request's model field.
        self.embed_model = embed_model or model
        self.timeout = timeout
        self.tier = tier
        self._http: Optional[httpx.AsyncClient] = None

    # -- connection pool ----------------------------------------------------
    def _get_http(self) -> httpx.AsyncClient:
        """Lazily build the persistent client (warm keepalive connections)."""
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                limits=httpx.Limits(
                    max_keepalive_connections=POOL_KEEPALIVE_CONNECTIONS,
                    max_connections=POOL_MAX_CONNECTIONS,
                    keepalive_expiry=600.0,
                ),
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    # -- transport -----------------------------------------------------------
    async def _post_json(self, path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        try:
            response = await self._get_http().post(path, json=payload, timeout=timeout)
        except httpx.HTTPError as exc:
            raise LlamaCppError(
                f"cannot reach llama-server at {self.base_url} - is `llama-server` running? ({exc})"
            ) from exc
        if response.status_code >= 400:
            detail = response.text[:300]
            # Actionable hint for the classic misconfigured-server 500: the
            # model was loaded without --pooling, so /v1/embeddings is not
            # OpenAI-compatible. run.sh deliberately does NOT pass
            # --embeddings/--pooling (generative servers only); embeddings are
            # disabled client-side via LLAMA_EMBEDDINGS_ENABLED=0.
            if "pooling type" in detail.lower():
                detail += (
                    " -- this llama-server was started without --embeddings/--pooling;"
                    " set LLAMA_EMBEDDINGS_ENABLED=0 or start a dedicated embeddings server"
                )
            raise LlamaCppError(
                f"llama-server HTTP {response.status_code} on {path}: {detail}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise LlamaCppError(f"llama-server returned non-JSON on {path}") from exc

    async def _get_json(self, path: str, timeout: float = 15.0) -> dict[str, Any]:
        try:
            response = await self._get_http().get(path, timeout=timeout)
        except httpx.HTTPError as exc:
            raise LlamaCppError(f"cannot reach llama-server at {self.base_url}: {exc}") from exc
        if response.status_code >= 400:
            raise LlamaCppError(f"llama-server HTTP {response.status_code} on {path}")
        try:
            return response.json()
        except ValueError as exc:
            raise LlamaCppError(f"llama-server returned non-JSON on {path}") from exc

    async def list_models(self) -> list[str]:
        data = await self._get_json("/v1/models")
        items = data.get("data", []) if isinstance(data, dict) else []
        return [str(item.get("id", "")) for item in items if item.get("id")]

    @staticmethod
    def _model_stem(name: str) -> str:
        base = name.rstrip("/").split("/")[-1]
        return base[:-5] if base.lower().endswith(".gguf") else base

    def _present(self, name: str, models: list[str]) -> bool:
        target = self._model_stem(name)
        if not target or target == "default":
            # llama-server serves exactly the -m model; the API name is decorative.
            return True
        return any(
            target == self._model_stem(m)
            or target in self._model_stem(m)
            or self._model_stem(m) in target
            for m in models
        )

    async def health(self) -> dict[str, Any]:
        try:
            status_body = await self._get_json("/health")
        except LlamaCppError as exc:
            return {"ok": False, "base_url": self.base_url, "error": str(exc), "models": []}
        if not (isinstance(status_body, dict) and status_body.get("status") in ("ok", True)):
            return {
                "ok": False,
                "base_url": self.base_url,
                "error": "llama-server is starting or still loading the model",
                "models": [],
            }
        try:
            models = await self.list_models()
        except LlamaCppError as exc:
            return {"ok": False, "base_url": self.base_url, "error": str(exc), "models": []}
        return {
            "ok": True,
            "base_url": self.base_url,
            "models": models,
            "chat_model": self.model,
            "chat_model_present": self._present(self.model, models),
            "embed_model": self.embed_model,
            "embed_model_present": self._present(self.embed_model, models),
            "embeddings_enabled": EMBEDDINGS_ENABLED,
        }

    # -- streaming ------------------------------------------------------------
    async def stream_chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 220,
        temperature: float = 0.3,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[str]:
        """Yield assistant text deltas from an OpenAI-compatible SSE stream.

        Used by the in-quiz Socratic tutor (``POST /api/tutor/hint``): the
        caller relays each delta to the browser as a server-sent event so the
        hint appears word-by-word. ``stream_options.include_usage`` is left off
        on purpose — llama.cpp emits a final ``[DONE]`` and we stop there.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        try:
            async with self._get_http().stream(
                "POST",
                "/v1/chat/completions",
                json=payload,
                timeout=httpx.Timeout(timeout or self.timeout, connect=10.0),
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")[:300]
                    raise LlamaCppError(
                        f"llama-server HTTP {response.status_code} on /v1/chat/completions: {body}"
                    )
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    chunk = line[len("data:"):].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        parsed = json.loads(chunk)
                    except ValueError:
                        continue
                    for choice in parsed.get("choices", []) or []:
                        delta = (choice.get("delta") or {}).get("content")
                        if delta:
                            yield str(delta)
        except httpx.HTTPError as exc:
            raise LlamaCppError(
                f"cannot reach llama-server at {self.base_url} - is `llama-server` running? ({exc})"
            ) from exc

    # -- completions ----------------------------------------------------------
    async def _chat_completion(
        self,
        *,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        max_tokens: int,
        temperature: float,
        seed: Optional[int] = None,
    ) -> str:
        """One schema-constrained completion; returns the message content."""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": round(max(0.0, temperature), 3),
            "top_p": TOP_P,
            "max_tokens": max_tokens,
            # Structured output: the schema travels as a JSON *object* in the
            # top-level "json_schema" field (stringifying it breaks the server's
            # grammar converter with "Unrecognized schema"), and response_format
            # json_object forbids prose around the constrained object.
            "response_format": {"type": "json_object"},
            "json_schema": schema,
            # Reuse the server-side KV prefix cache across repair turns.
            "cache_prompt": True,
            "repeat_penalty": 1.05,
        }
        if not ENABLE_THINKING:
            # Turn off Qwen 3.5's thinking mode so the completion budget goes to
            # the JSON answer, not to reasoning_content that never reaches content.
            body["chat_template_kwargs"] = {"enable_thinking": False}
        if seed is not None:
            body["seed"] = seed
        data = await self._post_json("/v1/chat/completions", body, self.timeout)
        try:
            message = data["choices"][0]["message"]
            finish_reason = data["choices"][0].get("finish_reason")
        except (KeyError, IndexError, TypeError) as exc:
            raise LlamaCppError(
                f"unexpected completion payload: {json.dumps(data, default=str)[:200]}"
            ) from exc
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise LlamaCppError(
                "model returned an empty response"
                + (f" (finish_reason={finish_reason})" if finish_reason else "")
            )
        return content

    # -- learning bundle (primary path) ---------------------------------------
    async def generate_bundle(
        self,
        lecture_text: str,
        model_name: Optional[str] = None,
        *,
        temperature: float = TEMPERATURE,
        seed: Optional[int] = None,
        max_chars: int = MAX_SOURCE_CHARS,
    ) -> dict[str, Any]:
        """One constrained ``/v1/chat/completions`` call -> validated LearningUnitBundle."""
        schema = build_bundle_schema()
        text = (lecture_text or "").strip()
        if len(text) < 80:
            raise LlamaCppError("lecture text is too short to build a learning unit")
        messages = [
            {"role": "system", "content": BUNDLE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": BUNDLE_PROMPT_TEMPLATE.format(lecture_text=text[:max_chars]),
            },
        ]
        last_error: Optional[Exception] = None

        # LLAMA_BUNDLE_ATTEMPTS=0 skips the single-shot bundle entirely and
        # lets the caller fall straight through to per-type composition.
        for attempt in range(1, max(0, BUNDLE_ATTEMPTS) + 1):
            content = await self._chat_completion(
                messages=messages,
                schema=schema,
                # Content-adaptive bundles (up to 24 challenges) need a big window.
                max_tokens=MAX_TOKENS_BUNDLE,
                temperature=temperature + 0.1 * (attempt - 1),
                seed=seed if seed is not None else random.randint(0, 2**31 - 1),
            )
            try:
                raw = _extract_json(content)
                for challenge in raw.get("challenges", []):
                    if isinstance(challenge, dict):
                        _normalize_cloze_blank(challenge)
                        _repair_cloze_blank(challenge)
                unit = LearningUnitBundle.model_validate(raw)
                # Pedagogical coverage is a soft target at persistence time:
                # warn loudly (fed back to the caller's error list) but keep
                # the partial bundle instead of discarding good challenges.
                missing = unit.missing_challenge_types
                if missing:
                    logger.warning(
                        "bundle for chunk is missing challenge types: %s", missing
                    )
            except (LlamaCppError, ValidationError, ValueError) as exc:
                last_error = exc
                logger.warning(
                    "learning bundle rejected on attempt %d/%d: %s",
                    attempt,
                    MAX_ATTEMPTS,
                    _describe_validation_error(exc),
                )
                messages = messages + [
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": (
                            f"Your previous answer was rejected by the validator: "
                            f"{_describe_validation_error(exc)}.\n"
                            f"Previous answer: {content[:1200]}\n"
                            "Return the complete corrected JSON object. Fix only that problem "
                            "and keep every other constraint satisfied."
                        ),
                    },
                ]
                continue
            return unit.model_dump(mode="json")

        raise LlamaCppError(
            f"learning bundle failed validation after {max(0, BUNDLE_ATTEMPTS)} attempt(s): "
            f"{_describe_validation_error(last_error) if last_error else 'skipped'}"
        )

    # -- chat (fallback path) --------------------------------------------------
    async def chat_json(
        self,
        user_prompt: str,
        schema: dict[str, Any],
        *,
        system_prompt: str = _SYSTEM_PROMPT,
        temperature: float = TEMPERATURE,
        seed: Optional[int] = None,
    ) -> dict[str, Any]:
        """One structured completion, repaired up to MAX_ATTEMPTS times."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        last_error: Optional[Exception] = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            content = await self._chat_completion(
                messages=messages,
                schema=schema,
                max_tokens=MAX_TOKENS_ONE,
                temperature=temperature + 0.1 * (attempt - 1),
                seed=seed if seed is not None else random.randint(0, 2**31 - 1),
            )
            try:
                return _extract_json(content)
            except LlamaCppError as exc:
                last_error = exc
                messages = messages + [
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": (
                            f"That response was rejected: {exc}. "
                            "Reply again with ONE valid JSON object only, "
                            "matching the JSON Schema exactly."
                        ),
                    },
                ]
        raise LlamaCppError(f"structured decoding failed after {MAX_ATTEMPTS} attempts: {last_error}")

    # -- embeddings --------------------------------------------------------------
    async def embed_optional(self, text: str) -> list[float]:
        """Best-effort embedding with a deterministic dummy-vector fallback.

        The generative llama-server tiers run WITHOUT --embeddings/--pooling
        (see run.sh), so by default no HTTP call is attempted at all — the
        documented n-gram speculative setup cannot serve embeddings, and the
        old behaviour burned a doomed HTTP 500 per chunk before falling back.
        Every failure mode (disabled here, HTTP 500 on a server without
        ``--embeddings``/``--pooling``, oversized input, timeout, OOM) returns
        a deterministic placeholder vector instead of raising, so a long
        ingestion job is never interrupted.
        """
        if not EMBEDDINGS_ENABLED:
            return _dummy_embedding(text)
        try:
            return await self.embed(text)
        except Exception as exc:  # noqa: BLE001 - embeddings are optional
            logger.warning(
                "embedding failed for '%s...': %s (using deterministic fallback vector)",
                (text or "")[:40],
                exc,
            )
            return _dummy_embedding(text)

    async def embed(self, text: str) -> list[float]:
        """Embedding via /v1/embeddings (needs a server started with --embeddings).

        Chunk bodies run up to MAX_SOURCE_CHARS (~2.5k tokens) which exceeds
        this build's embedding physical-batch budget (500: "input is too
        large to process. increase the physical batch size"). The text is
        embedded in ~1500-char slices and mean-pooled client-side, which
        stays within budget regardless of the server's -ub.
        """
        snippet = (text or "").strip()
        if not snippet:
            raise LlamaCppError("cannot embed empty text")
        # ~1500 chars ≈ 350-400 tokens — safely under a 1024 physical batch.
        slice_size = int(os.getenv("LLAMA_EMBED_SLICE_CHARS", "1500"))
        slices = [snippet[i : i + slice_size] for i in range(0, len(snippet), slice_size)]
        if len(slices) == 1:
            vectors = [await self._embed_one(slices[0])]
        else:
            vectors = list(
                await asyncio.gather(*(self._embed_one(part) for part in slices))
            )
        # Mean-pool the slice vectors at the model's native width, then
        # truncate/pad to the schema's EMBEDDING_DIM (vector(1536) column).
        native_dim = len(vectors[0])
        pooled = [sum(v[i] for v in vectors) / len(vectors) for i in range(native_dim)]
        return _fit_dim(pooled)  # -> exactly EMBEDDING_DIM, L2-normalised

    async def _embed_one(self, snippet: str) -> list[float]:
        """One /v1/embeddings call for a short snippet; returns the raw vector."""
        data = await self._post_json(
            "/v1/embeddings",
            {"model": self.embed_model, "input": snippet},
            EMBED_TIMEOUT,
        )
        try:
            vector = data["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError):
            vector = None
        if not isinstance(vector, list) or not vector:
            raise LlamaCppError("embedding model returned no vector")
        return vector

    # -- single challenge (fallback path) -----------------------------------------
    async def generate_one(
        self,
        challenge_type: str,
        node_title: str,
        content: str,
        *,
        temperature: float = TEMPERATURE,
        seed: Optional[int] = None,
        variant: int = 0,
    ) -> GeneratedChallenge:
        """Generate + validate one challenge, re-prompting on semantic errors."""
        if challenge_type not in PAYLOAD_MODEL_BY_TYPE:
            raise LlamaCppError(f"unsupported challenge type {challenge_type}")
        schema = build_schema(challenge_type)
        brief = _TYPE_BRIEFS[challenge_type]
        if variant > 0:
            brief = (
                f"{brief} This is variation #{variant + 1}: pick a DIFFERENT term, "
                "sentence, or angle from the lecture than any previous variation — "
                "do not rephrase an earlier item."
            )
        base_prompt = _USER_TEMPLATE.format(
            title=node_title,
            content=content[:MAX_SOURCE_CHARS],
            brief=brief,
        )
        prompt = base_prompt
        last_error: Optional[Exception] = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            raw = await self.chat_json(
                prompt, schema, temperature=temperature + 0.1 * (attempt - 1), seed=seed
            )
            raw.setdefault("type", challenge_type)
            if challenge_type in CLOZE_LIKE_TYPES:
                _normalize_cloze_blank(raw)
                _repair_cloze_blank(raw)
            try:
                payload = validate_payload(challenge_type, raw)
            except (ValidationError, ValueError) as exc:
                last_error = exc
                logger.warning(
                    "%s rejected on attempt %d/%d: %s",
                    challenge_type,
                    attempt,
                    MAX_ATTEMPTS,
                    _describe_validation_error(exc),
                )
                prompt = (
                    f"{base_prompt}\n\n"
                    f"Your previous answer was rejected by the validator: "
                    f"{_describe_validation_error(exc)}.\n"
                    f"Previous answer: {json.dumps(raw, ensure_ascii=False)[:1200]}\n"
                    "Return the complete corrected JSON object. Fix only that problem "
                    "and keep every other constraint satisfied."
                )
                continue
            return GeneratedChallenge(
                challenge_type=challenge_type,
                difficulty=_estimate_difficulty(challenge_type, payload.model_dump()),
                payload=payload,
            )

        raise LlamaCppError(
            f"{challenge_type} could not satisfy its constraints after {MAX_ATTEMPTS} attempts: "
            f"{_describe_validation_error(last_error) if last_error else 'unknown'}"
        )

    async def generate_for_node(
        self,
        node_title: str,
        content: str,
        *,
        challenge_types: Sequence[str] = ALL_CHALLENGE_TYPES,
        variants: Optional[int] = None,
        target_challenges: Optional[int] = None,
        temperature: float = TEMPERATURE,
        concurrency: int = 4,
    ) -> tuple[list[GeneratedChallenge], list[str]]:
        """Generate every requested challenge type for a node in parallel.

        The default (``None`` variants) is CONTENT-ADAPTIVE: the total is
        derived from the chunk's density (``target_challenges`` or
        :func:`_target_challenge_count`) and spread across the requested types,
        so a short summary yields ~6-10 challenges and a dense technical part
        ~16-24 — no rigid per-type quota. Passing an explicit ``variants``
        restores a fixed count per type. A failure in one type never kills the
        others; partial results plus the error strings are returned so
        ingestion can still persist the node.

        Defaults to the four high-yield pedagogical variants
        (:data:`GENERATION_CHALLENGE_TYPES`); the retired INTERACTIVE_STORY
        chat type is never generated.
        """
        types = [challenge_type for challenge_type in challenge_types] or list(
            GENERATION_CHALLENGE_TYPES
        )
        if variants is None:
            total = (
                target_challenges
                if target_challenges is not None
                else _target_challenge_count(len(content or ""))
            )
            counts = _allocate_variants(total, len(types))
        else:
            counts = [max(1, variants)] * len(types)
        jobs = [
            (challenge_type, index)
            for challenge_type, count in zip(types, counts)
            for index in range(count)
        ]
        limit = asyncio.Semaphore(max(1, min(concurrency, len(jobs))))
        base_seed = random.randint(0, 2**31 - 1)

        async def _run(challenge_type: str, index: int) -> GeneratedChallenge | Exception:
            async with limit:
                try:
                    return await self.generate_one(
                        challenge_type,
                        node_title,
                        content,
                        temperature=temperature + 0.1 * (index % 4),
                        seed=(base_seed + index * 7919) % (2**31),
                        variant=index,
                    )
                except Exception as exc:  # noqa: BLE001 - reported per type
                    return exc

        results = list(await asyncio.gather(*(_run(t, i) for t, i in jobs)))

        # A failed variant would leave the node short of its content-adaptive
        # challenge target, so give every failure one more shot with a fresh seed.
        retries = [
            (job, result) for job, result in zip(jobs, results)
            if isinstance(result, Exception)
        ]
        if retries:
            logger.warning("retrying %d failed challenge variant(s)", len(retries))
            retry_results = await asyncio.gather(
                *(_run(tier, index + 16) for (tier, index), _ in retries)
            )
            for (job, _), retry in zip(retries, retry_results):
                results[jobs.index(job)] = retry

        challenges: list[GeneratedChallenge] = []
        errors: list[str] = []
        for (challenge_type, index), result in zip(jobs, results):
            label = challenge_type if index == 0 else f"{challenge_type}#{index + 1}"
            if isinstance(result, Exception):
                errors.append(f"{label}: {result}")
            else:
                challenges.append(result)
        return challenges, errors


_clients: dict[str, LlamaCppClient] = {}


def get_client(tier: Optional[str] = None) -> LlamaCppClient:
    """Return the cached client for a tier's llama-server (one per tier).

    Every selected tier runs its own llama-server on its own port (800M ->
    8081 ... 9B -> 8084), so each tier gets a dedicated client with its own
    persistent httpx pool — warm sockets for that tier, no cross-talk. Unknown
    spellings are normalised via :func:`normalize_tier`; unknown ids fall back
    to the default tier. ``None`` resolves to :data:`DEFAULT_TIER`.
    """
    canonical = normalize_tier(tier) or (
        DEFAULT_TIER if DEFAULT_TIER in MODEL_TIERS else "4B"
    )
    client = _clients.get(canonical)
    if client is None:
        client = LlamaCppClient(base_url=_base_url_for(MODEL_TIERS[canonical]), tier=canonical)
        _clients[canonical] = client
    return client


async def get_active_client(
    preferred: Optional[str] = None,
) -> tuple["LlamaCppClient", str]:
    """Return a healthy client, preferring ``preferred`` and falling back.

    Probes each tier's llama-server ``/health`` endpoint in order: the
    preferred tier first (default: the configured default tier), then every
    other tier, and finally the preferred/default client itself so a caller
    always receives a usable client even when every tier is down (the caller
    handles the resulting :class:`LlamaCppError`). Health probes are short
    (2s connect / 4s total) so a dead port never stalls a request.

    Returns ``(client, tier_id)``.
    """
    preferred_id = normalize_tier(preferred) or (
        DEFAULT_TIER if DEFAULT_TIER in MODEL_TIERS else "4B"
    )
    ordered = [preferred_id] + [tier for tier in MODEL_TIERS if tier != preferred_id]
    for tier_id in ordered:
        client = get_client(tier_id)
        try:
            health = await client.health()
        except Exception:  # noqa: BLE001 - a probe failure is just "unhealthy"
            health = {"ok": False}
        if health.get("ok"):
            return client, tier_id
    # Nothing is healthy: hand back the preferred client so the caller's error
    # message names the tier the user actually asked for.
    return get_client(preferred_id), preferred_id


async def aclose_all_clients() -> None:
    """Close every cached per-tier client (used at app shutdown)."""
    for client in _clients.values():
        await client.aclose()
    _clients.clear()


async def generate_bundles_for_chunks(
    chunks: Sequence,
    client: "LlamaCppClient",
    *,
    model_name: str = DEFAULT_BUNDLE_MODEL,
    concurrency: int = 2,
    challenge_types: Optional[Sequence[str]] = None,
) -> list[tuple]:
    """Generate + embed every chunk concurrently; results stay input-ordered.

    Each item is ``(chunk, bundle | None, embedding | None, errors)``. A bundle
    that fails the schema-constrained path is retried through the per-type
    CONTENT-ADAPTIVE fallback generator (challenge count scales with the chunk's
    density, not a fixed quota) so callers get the same payloads they always
    did — just overlapped on the GPU instead of one after another. With ``-np 2``
    on llama-server a fan-out of 2 saturates the two parallel slots; higher
    values help only until the GPU runs out of KV cache.

    ``challenge_types`` restricts which challenge kinds are generated (default:
    the four high-yield pedagogical variants). The single-shot bundle path is
    skipped when a subset is chosen — its prompt hard-codes full coverage of
    the four types.
    """
    selected_types = list(challenge_types) if challenge_types else list(
        GENERATION_CHALLENGE_TYPES
    )
    concurrency = max(1, concurrency)
    semaphore = asyncio.Semaphore(concurrency)

    async def _one(chunk) -> tuple:
        async with semaphore:
            bundle: Optional[dict] = None
            errors: list[str] = []
            tag = f"chunk {chunk.order_index}"
            if BUNDLE_ATTEMPTS > 0 and selected_types == list(GENERATION_CHALLENGE_TYPES):
                try:
                    bundle = await generate_learning_bundle(chunk.content, model_name=model_name)
                except Exception as exc:  # noqa: BLE001 - fallback below
                    errors.append(f"{tag} bundle: {exc}")
            if bundle is None:
                try:
                    fallback, fallback_errors = await client.generate_for_node(
                        chunk.title, chunk.content, challenge_types=selected_types
                    )
                    errors.extend(f"{tag} {message}" for message in fallback_errors)
                    if fallback:
                        bundle = {
                            "unit_id": f"fallback-{chunk.order_index}-{uuid.uuid4().hex[:8]}",
                            "concept_title": chunk.title,
                            "difficulty_level": 3,
                            "challenges": [
                                item.payload.model_dump(mode="json") for item in fallback
                            ],
                        }
                except Exception as fallback_exc:  # noqa: BLE001
                    errors.append(f"{tag} fallback: {fallback_exc}")

            embedding: Optional[list[float]] = None
            if bundle:
                # Embeddings are optional: embed_optional returns a deterministic
                # fallback vector (and skips HTTP entirely by default), so a long
                # ingestion job is never stalled or aborted.
                embedding = await client.embed_optional(f"{chunk.title}\n\n{chunk.content}")
            return chunk, bundle, embedding, errors

    return list(await asyncio.gather(*(_one(chunk) for chunk in chunks)))


async def generate_learning_bundle(
    lecture_text: str,
    model_name: str = DEFAULT_BUNDLE_MODEL,
) -> dict[str, Any]:
    """Generate one validated :class:`LearningUnitBundle` for a lecture chunk.

    Issues a structured JSON request to ``/v1/chat/completions`` constrained by
    ``LearningUnitBundle.model_json_schema()`` and yields the validated bundle
    as a plain dict, ready for :func:`database.save_pdf_lecture`. The bundle
    covers all six challenge types, sized to the material's density rather than
    a fixed quota (roughly 6-10 for a short summary, 16-24 for a dense part).
    """
    return await get_client().generate_bundle(lecture_text, model_name=model_name)


def _empty_chunk_result(chunk: Any) -> tuple:
    """The ``(chunk, None, None, errors)`` failure shape shared by both paths."""
    return chunk, None, None, []


async def iter_generated_bundles_for_chunks(
    chunks: Sequence,
    client: "LlamaCppClient",
    *,
    model_name: str = DEFAULT_BUNDLE_MODEL,
    concurrency: int = 2,
    challenge_types: Optional[Sequence[str]] = None,
):
    """Yield ``(chunk, bundle | None, embedding | None, errors)`` AS EACH chunk
    finishes — the streaming companion to :func:`generate_bundles_for_chunks`.

    Semantics are identical (same semaphore, same schema-constrained primary
    path, same per-type CONTENT-ADAPTIVE fallback, input order irrelevant for
    correctness because the caller keys results off ``chunk.order_index``), but
    nothing awaits the whole deck: the FIRST completed chunk is handed to the
    caller while the remaining inference still runs, so a commit can land and
    Part 1 becomes playable within seconds of the upload.
    """
    selected_types = list(challenge_types) if challenge_types else list(
        GENERATION_CHALLENGE_TYPES
    )
    concurrency = max(1, concurrency)
    semaphore = asyncio.Semaphore(concurrency)

    async def _one(chunk) -> tuple:
        async with semaphore:
            bundle: Optional[dict] = None
            errors: list[str] = []
            tag = f"chunk {chunk.order_index}"
            if BUNDLE_ATTEMPTS > 0 and selected_types == list(GENERATION_CHALLENGE_TYPES):
                try:
                    bundle = await generate_learning_bundle(chunk.content, model_name=model_name)
                except Exception as exc:  # noqa: BLE001 - fallback below
                    errors.append(f"{tag} bundle: {exc}")
            if bundle is None:
                try:
                    fallback, fallback_errors = await client.generate_for_node(
                        chunk.title, chunk.content, challenge_types=selected_types
                    )
                    errors.extend(f"{tag} {message}" for message in fallback_errors)
                    if fallback:
                        bundle = {
                            "unit_id": f"fallback-{chunk.order_index}-{uuid.uuid4().hex[:8]}",
                            "concept_title": chunk.title,
                            "difficulty_level": 3,
                            "challenges": [
                                item.payload.model_dump(mode="json") for item in fallback
                            ],
                        }
                except Exception as fallback_exc:  # noqa: BLE001
                    errors.append(f"{tag} fallback: {fallback_exc}")

            embedding: Optional[list[float]] = None
            if bundle:
                # Same optional-embedding policy as the batch path: never stall
                # or abort a long ingestion job on the embedding endpoint.
                embedding = await client.embed_optional(f"{chunk.title}\n\n{chunk.content}")
            return chunk, bundle, embedding, errors

    tasks = [asyncio.ensure_future(_one(chunk)) for chunk in chunks]
    try:
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    # _one catches its own inference errors; an escaping
                    # exception means a bug — degrade to an empty result so one
                    # bad chunk cannot abort the streaming of the rest.
                    chunk = getattr(task, "_chunk_ref", None)
                    if chunk is None:
                        continue
                    yield _empty_chunk_result(chunk)
                else:
                    yield task.result()
    finally:
        # The caller may stop consuming early (job cancelled, lecture deleted):
        # release the GPU slots instead of leaving orphaned inference running.
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
