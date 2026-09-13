"""Structured challenge generation with a local Qwen model served by Ollama.

Two entry points:

* :func:`generate_learning_bundle` — the primary path. One ``/api/generate``
  call constrained by ``LearningUnitBundle.model_json_schema()`` produces a whole
  learning unit (concept title, difficulty level, 2-8 challenges) for a lecture
  chunk, under an Arabic academic system prompt.
* :meth:`OllamaClient.generate_one` / :meth:`generate_for_node` — the fallback
  path: one constrained call per challenge type, run in parallel, each with its
  own repair loop. Used when the single bundle call fails validation.

Schema-constrained decoding guarantees well-formed JSON; business rules that no
JSON Schema can express (a "wrong" token that occurs exactly once, one correct
option per dialogue node, no duplicate types in a unit) are enforced by Pydantic
and fed back to the model as a repair turn before a challenge is dropped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
from typing import Any, Optional, Sequence

import requests
from pydantic import ValidationError

from schema import (
    ALL_CHALLENGE_TYPES,
    PAYLOAD_MODEL_BY_TYPE,
    GeneratedChallenge,
    LearningUnitBundle,
    json_schema_for,
    validate_payload,
)

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")

logger = logging.getLogger("campuslingo.llm")

EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1536"))

REQUEST_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "300"))
EMBED_TIMEOUT = float(os.getenv("OLLAMA_EMBED_TIMEOUT", "120"))
MAX_ATTEMPTS = int(os.getenv("OLLAMA_MAX_ATTEMPTS", "3"))
MAX_SOURCE_CHARS = int(os.getenv("OLLAMA_MAX_SOURCE_CHARS", "7000"))
# Reasoning traces are dead weight when the answer is schema-constrained: they
# multiply latency without improving constraint satisfaction.
OLLAMA_THINK = os.getenv("OLLAMA_THINK", "false").strip().lower() in ("1", "true", "yes")

DEFAULT_BUNDLE_MODEL = os.getenv("OLLAMA_BUNDLE_MODEL", "qwen2.5:7b")

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

BUNDLE_SYSTEM_PROMPT = """أنت أستاذ جامعي خبير في تصميم المحتوى التعليمي التفاعلي، وتعمل على منصة \
تعلّم مصغّر (micro-learning) لطلبة الجامعات.

مهمتك: تحويل نص المحاضرة المعطى إلى وحدة تعلّم واحدة متكاملة تحتوي على تحديات تعليمية ممتعة.

القواعد الصارمة:
1. الدقة العلمية أولاً: لا تختلق أي معلومة غير موجودة في النص، ولا تستخدم مصطلحاً خارج النص.
2. اختر أفكاراً مركزية من النص، لا تفاصيل هامشية.
3. اكتب بالعربية الفصحى المبسطة، مع إبقاء المصطلحات التقنية والرموز والوحدات بالإنجليزية كما وردت في النص.
4. إذا كان نص المحاضرة بالإنجليزية فاجعل الشروحات بالعربية والمصطلحات بالإنجليزية.
5. أضف روح الدعابة الأكاديمية اللطيفة في التغذية الراجعة (feedback) وفي الشرح — تعليق ذكي أو مقارنة طريفة — \
لكن دون أي تهوين من قيمة المعلومة العلمية أو السخرية من الطالب.
6. يجب أن يحتوي حقل mistake_token على الجزء الخاطئ حرفياً كما يظهر في code_or_text، \
ويظهر مرة واحدة فقط في النص.
7. يجب أن يحتوي sentence_with_blank على الفراغ بالصيغة _____ بالضبط.
8. لكل عقدة حوار (dialogue node) خيار صحيح واحد فقط.
9. لا تكرر نوع التحدي نفسه مرتين داخل الوحدة.

أجب بكائن JSON واحد فقط مطابق للمخطط (schema) المعطى — دون أي شرح إضافي أو علامات markdown."""

BUNDLE_PROMPT_TEMPLATE = """نص المحاضرة:

--- BEGIN LECTURE TEXT ---
{lecture_text}
--- END LECTURE TEXT ---

المطلوب:
- concept_title: عنوان المفهوم الأساسي في هذا النص (بالعربية مع المصطلح الإنجليزي بين قوسين عند الحاجة).
- difficulty_level: مستوى الصعوبة من 1 (تمهيدي) إلى 5 (متقدم) بحسب عمق النص.
- challenges: من 2 إلى 8 تحديات متنوعة من الأنواع الأربعة (مطابقة، اكتشاف الخطأ، إكمال الفراغ، قصة تفاعلية).

أعد كائن JSON واحداً فقط."""

_TYPE_BRIEFS: dict[str, str] = {
    "MATCH_MADNESS": (
        "Build a tap-to-pair drill. Each pair maps a key TERM from the lecture to the ONE definition "
        "that means it. Terms must be distinct, definitions must be distinct and self-contained, and a "
        "definition must never repeat its own term. Use 4 to 6 pairs."
    ),
    "SPOT_MISTAKE": (
        "Write ONE sentence, formula or short code block about the lecture that contains exactly one "
        "factual error (a swapped definition, an inverted sign, a wrong cause/effect, a wrong value). "
        "'code_or_text' holds the full sentence; 'mistake_token' is the exact wrong substring as it "
        "appears in that text (must appear exactly once); 'explanation' says why it is wrong and what "
        "the correct version is."
    ),
    "CONTEXTUAL_CLOZE": (
        "Write ONE sentence taken from the lecture with a key term removed and replaced by '_____'. "
        "'correct_answer' is the removed term, and provide 2 to 4 plausible distractors from the same "
        "topic area that are NOT the answer. Add a short 'explanation'."
    ),
    "INTERACTIVE_STORY": (
        "Write a branching dialogue (2 to 3 nodes) in which the student plays an engineer/researcher "
        "applying this lecture. Each node has a 'speaker', their 'text' (the situation), an "
        "'action_prompt' (the decision question), and 2 to 3 'options' with 'text', 'correct' and "
        "'feedback' (the character's reaction). Exactly ONE option per node is correct."
    ),
}

_SYSTEM_PROMPT = (
    BUNDLE_SYSTEM_PROMPT
    + "\n\nYou are generating a SINGLE challenge object for one of the four challenge types below."
)

_USER_TEMPLATE = """Lecture node: {title}

--- BEGIN LECTURE TEXT ---
{content}
--- END LECTURE TEXT ---

Task: {brief}

Return ONLY the JSON object for this task."""


class OllamaError(RuntimeError):
    """Any transport, HTTP, or decoding failure talking to Ollama."""


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
    return schema


def _extract_json(text: str) -> dict[str, Any]:
    """Parse a model reply into a dict, tolerating stray fences/prose."""
    if not text or not text.strip():
        raise OllamaError("model returned an empty response")
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
            raise OllamaError(f"no JSON object in model response: {text[:200]!r}")
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise OllamaError(f"malformed JSON from model: {exc}") from exc
    if not isinstance(parsed, dict):
        raise OllamaError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def _fit_dim(vector: Sequence[float], dim: int = EMBEDDING_DIM) -> list[float]:
    """Truncate/zero-pad an embedding to the schema's fixed vector(1536)."""
    values = [float(v) for v in vector]
    if not values:
        raise OllamaError("embedding model returned an empty vector")
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


def _estimate_difficulty(challenge_type: str, payload: dict[str, Any]) -> float:
    """Heuristic 0..1 difficulty from payload breadth (cold-start prior)."""
    if challenge_type == "MATCH_MADNESS":
        raw = (len(payload.get("pairs", [])) - 4) / 6.0 + 0.15
    elif challenge_type == "SPOT_MISTAKE":
        raw = 0.3 + min(0.35, len(payload.get("code_or_text", "")) / 2400.0)
    elif challenge_type == "CONTEXTUAL_CLOZE":
        raw = 0.2 + len(payload.get("distractors", [])) / 12.0
    elif challenge_type == "INTERACTIVE_STORY":
        raw = 0.45 + (len(payload.get("dialogue", [])) - 2) / 8.0
    else:
        raw = 0.3
    return round(min(1.0, max(0.05, raw)), 3)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class OllamaClient:
    """Thin async wrapper over the Ollama HTTP API."""

    def __init__(
        self,
        host: str = OLLAMA_HOST,
        model: str = OLLAMA_MODEL,
        embed_model: str = OLLAMA_EMBED_MODEL,
        timeout: float = REQUEST_TIMEOUT,
        think: bool = OLLAMA_THINK,
    ) -> None:
        self.host = host.rstrip("/")
        self.model = model
        self.embed_model = embed_model
        self.timeout = timeout
        self.think = think
        self._session = requests.Session()

    # -- transport ---------------------------------------------------------
    def _post(self, path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        url = f"{self.host}{path}"
        try:
            response = self._session.post(url, json=payload, timeout=timeout)
        except requests.exceptions.ConnectionError as exc:
            raise OllamaError(
                f"cannot reach Ollama at {self.host} - is `ollama serve` running? ({exc})"
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise OllamaError(f"Ollama timed out after {timeout:.0f}s on {path}") from exc
        if response.status_code >= 400:
            raise OllamaError(
                f"Ollama HTTP {response.status_code} on {path}: {response.text[:300]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise OllamaError(f"Ollama returned non-JSON on {path}") from exc

    def _get(self, path: str, timeout: float = 15.0) -> dict[str, Any]:
        url = f"{self.host}{path}"
        try:
            response = self._session.get(url, timeout=timeout)
        except requests.exceptions.RequestException as exc:
            raise OllamaError(f"cannot reach Ollama at {self.host}: {exc}") from exc
        if response.status_code >= 400:
            raise OllamaError(f"Ollama HTTP {response.status_code} on {path}")
        return response.json()

    async def list_models(self) -> list[str]:
        data = await asyncio.to_thread(self._get, "/api/tags")
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]

    async def health(self) -> dict[str, Any]:
        try:
            models = await self.list_models()
        except OllamaError as exc:
            return {"ok": False, "host": self.host, "error": str(exc), "models": []}

        def _present(name: str) -> bool:
            return any(m == name or m.split(":")[0] == name.split(":")[0] for m in models)

        return {
            "ok": True,
            "host": self.host,
            "models": models,
            "chat_model": self.model,
            "chat_model_present": _present(self.model),
            "bundle_model": DEFAULT_BUNDLE_MODEL,
            "embed_model": self.embed_model,
            "embed_model_present": _present(self.embed_model),
        }

    # -- learning bundle (primary path) ------------------------------------
    async def generate_bundle(
        self,
        lecture_text: str,
        model_name: Optional[str] = None,
        *,
        temperature: float = 0.5,
        seed: Optional[int] = None,
        max_chars: int = MAX_SOURCE_CHARS,
    ) -> dict[str, Any]:
        """One constrained ``/api/generate`` call -> validated LearningUnitBundle."""
        model = model_name or self.model
        schema = build_bundle_schema()
        text = (lecture_text or "").strip()
        if len(text) < 80:
            raise OllamaError("lecture text is too short to build a learning unit")
        prompt = BUNDLE_PROMPT_TEMPLATE.format(lecture_text=text[:max_chars])
        last_error: Optional[Exception] = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            payload = {
                "model": model,
                "prompt": prompt,
                "system": BUNDLE_SYSTEM_PROMPT,
                "stream": False,
                "think": self.think,
                "format": schema,
                "options": {
                    "temperature": max(0.0, temperature + 0.15 * (attempt - 1)),
                    "top_p": 0.9,
                    "repeat_penalty": 1.05,
                    "num_predict": 3072,
                    "num_ctx": 8192,
                    "seed": seed if seed is not None else random.randint(0, 2**31 - 1),
                },
            }
            data = await asyncio.to_thread(self._post, "/api/generate", payload, self.timeout)
            raw_text = data.get("response", "")
            try:
                raw = _extract_json(raw_text)
                unit = LearningUnitBundle.model_validate(raw)
            except (OllamaError, ValidationError, ValueError) as exc:
                last_error = exc
                logger.warning(
                    "learning bundle rejected on attempt %d/%d: %s",
                    attempt,
                    MAX_ATTEMPTS,
                    _describe_validation_error(exc),
                )
                prompt = (
                    BUNDLE_PROMPT_TEMPLATE.format(lecture_text=text[:max_chars])
                    + f"\n\nYour previous answer was rejected by the validator: "
                    f"{_describe_validation_error(exc)}.\n"
                    f"Previous answer: {raw_text[:1200]}\n"
                    "Return the complete corrected JSON object. Fix only that problem "
                    "and keep every other constraint satisfied."
                )
                continue
            return unit.model_dump(mode="json")

        raise OllamaError(
            f"learning bundle failed validation after {MAX_ATTEMPTS} attempts: "
            f"{_describe_validation_error(last_error) if last_error else 'unknown'}"
        )

    # -- chat (fallback path) ----------------------------------------------
    async def chat_json(
        self,
        user_prompt: str,
        schema: dict[str, Any],
        *,
        system_prompt: str = _SYSTEM_PROMPT,
        temperature: float = 0.4,
        seed: Optional[int] = None,
    ) -> dict[str, Any]:
        """One structured completion, repaired up to MAX_ATTEMPTS times."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        last_error: Optional[Exception] = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "think": self.think,
                "format": schema,
                "options": {
                    "temperature": max(0.0, temperature + 0.15 * (attempt - 1)),
                    "top_p": 0.9,
                    "repeat_penalty": 1.05,
                    "num_predict": 2048,
                    "num_ctx": 8192,
                    "seed": seed if seed is not None else random.randint(0, 2**31 - 1),
                },
            }
            data = await asyncio.to_thread(self._post, "/api/chat", payload, self.timeout)
            content = (data.get("message") or {}).get("content", "")
            try:
                return _extract_json(content)
            except OllamaError as exc:
                last_error = exc
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"That response was rejected: {exc}. "
                            "Reply again with ONE valid JSON object only, "
                            "matching the JSON Schema exactly."
                        ),
                    }
                )
        raise OllamaError(f"structured decoding failed after {MAX_ATTEMPTS} attempts: {last_error}")

    # -- embeddings --------------------------------------------------------
    async def embed(self, text: str) -> list[float]:
        """Embedding as a 1536-dim L2-normalised vector."""
        snippet = (text or "").strip()[:6000]
        if not snippet:
            raise OllamaError("cannot embed empty text")
        payload = {"model": self.embed_model, "prompt": snippet}
        try:
            data = await asyncio.to_thread(self._post, "/api/embeddings", payload, EMBED_TIMEOUT)
            vector = data.get("embedding")
        except OllamaError:
            # Ollama >= 0.3.x exposes /api/embed with a different body shape.
            data = await asyncio.to_thread(
                self._post, "/api/embed", {"model": self.embed_model, "input": snippet}, EMBED_TIMEOUT
            )
            embeddings = data.get("embeddings") or []
            vector = embeddings[0] if embeddings else None
        if not isinstance(vector, list) or not vector:
            raise OllamaError("embedding model returned no vector")
        return _fit_dim(vector)

    # -- single challenge (fallback path) ----------------------------------
    async def generate_one(
        self,
        challenge_type: str,
        node_title: str,
        content: str,
        *,
        temperature: float = 0.4,
        seed: Optional[int] = None,
    ) -> GeneratedChallenge:
        """Generate + validate one challenge, re-prompting on semantic errors."""
        if challenge_type not in PAYLOAD_MODEL_BY_TYPE:
            raise OllamaError(f"unsupported challenge type {challenge_type}")
        schema = build_schema(challenge_type)
        base_prompt = _USER_TEMPLATE.format(
            title=node_title,
            content=content[:MAX_SOURCE_CHARS],
            brief=_TYPE_BRIEFS[challenge_type],
        )
        prompt = base_prompt
        last_error: Optional[Exception] = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            raw = await self.chat_json(
                prompt, schema, temperature=temperature + 0.15 * (attempt - 1), seed=seed
            )
            raw.setdefault("type", challenge_type)
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

        raise OllamaError(
            f"{challenge_type} could not satisfy its constraints after {MAX_ATTEMPTS} attempts: "
            f"{_describe_validation_error(last_error) if last_error else 'unknown'}"
        )

    async def generate_for_node(
        self,
        node_title: str,
        content: str,
        *,
        challenge_types: Sequence[str] = ALL_CHALLENGE_TYPES,
        temperature: float = 0.4,
        concurrency: int = 4,
    ) -> tuple[list[GeneratedChallenge], list[str]]:
        """Generate every challenge type for a node in parallel.

        A failure in one type never kills the others; partial results plus the
        error strings are returned so ingestion can still persist the node.
        """
        limit = asyncio.Semaphore(max(1, min(concurrency, len(challenge_types))))
        seed = random.randint(0, 2**31 - 1)

        async def _run(challenge_type: str) -> GeneratedChallenge | Exception:
            async with limit:
                try:
                    return await self.generate_one(
                        challenge_type, node_title, content, temperature=temperature, seed=seed
                    )
                except Exception as exc:  # noqa: BLE001 - reported per type
                    return exc

        results = await asyncio.gather(*(_run(t) for t in challenge_types))
        challenges: list[GeneratedChallenge] = []
        errors: list[str] = []
        for challenge_type, result in zip(challenge_types, results):
            if isinstance(result, Exception):
                errors.append(f"{challenge_type}: {result}")
            else:
                challenges.append(result)
        return challenges, errors


_client: Optional[OllamaClient] = None


def get_client() -> OllamaClient:
    """Process-wide singleton client."""
    global _client
    if _client is None:
        _client = OllamaClient()
    return _client


async def generate_learning_bundle(
    lecture_text: str,
    model_name: str = DEFAULT_BUNDLE_MODEL,
) -> dict[str, Any]:
    """Generate one validated :class:`LearningUnitBundle` for a lecture chunk.

    Issues a structured JSON request to ``/api/generate`` constrained by
    ``LearningUnitBundle.model_json_schema()`` and returns the validated bundle
    as a plain dict, ready for :func:`database.save_lecture_and_challenges`.
    """
    return await get_client().generate_bundle(lecture_text, model_name=model_name)
