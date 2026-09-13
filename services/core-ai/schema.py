"""Pydantic v2 contracts for the CampusLingo core-ai service.

Two families of models live here:

* The four ``*Challenge`` payloads plus :class:`LearningUnitBundle` — these are
  what the Qwen model is constrained to emit (``LearningUnitBundle.model_json_schema()``
  is handed to Ollama as ``format``), what is stored verbatim in
  ``challenges.payload_json``, and what the Next.js components render.
* ``*Request`` / ``*Response`` models — the HTTP API contract.

Field names here are the single source of truth: ``apps/web/lib/api.ts`` mirrors
them one-to-one.
"""

from __future__ import annotations

import copy
import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# ---------------------------------------------------------------------------
# Challenge type registry
# ---------------------------------------------------------------------------

ChallengeType = Literal[
    "MATCH_MADNESS",
    "SPOT_MISTAKE",
    "CONTEXTUAL_CLOZE",
    "INTERACTIVE_STORY",
]

ALL_CHALLENGE_TYPES: tuple[str, ...] = (
    "MATCH_MADNESS",
    "SPOT_MISTAKE",
    "CONTEXTUAL_CLOZE",
    "INTERACTIVE_STORY",
)

BLANK_PLACEHOLDER = "_____"


# ---------------------------------------------------------------------------
# 1. MATCH_MADNESS — 4-6 term/definition pairs, 60 second drill
# ---------------------------------------------------------------------------


class MatchPair(BaseModel):
    """One tap-to-pair item: a term and the definition that means it."""

    term: str = Field(..., min_length=1, max_length=160)
    definition: str = Field(..., min_length=1, max_length=240)


class MatchMadnessChallenge(BaseModel):
    type: Literal["MATCH_MADNESS"] = "MATCH_MADNESS"
    instructions: str = Field(
        "طابق كل مصطلح مع تعريفه الصحيح قبل انتهاء الوقت",
        min_length=1,
        max_length=240,
    )
    pairs: list[MatchPair] = Field(..., min_length=4, max_length=6)
    seconds: int = Field(60, ge=30, le=120, description="Total countdown for the drill")

    @model_validator(mode="after")
    def _unique_sides(self) -> "MatchMadnessChallenge":
        terms = [pair.term.strip().lower() for pair in self.pairs]
        definitions = [pair.definition.strip().lower() for pair in self.pairs]
        if len(set(terms)) != len(terms):
            raise ValueError("MATCH_MADNESS: duplicate 'term' entries")
        if len(set(definitions)) != len(definitions):
            raise ValueError("MATCH_MADNESS: duplicate 'definition' entries")
        return self


# ---------------------------------------------------------------------------
# 2. SPOT_MISTAKE — click the wrong token inside a sentence or code block
# ---------------------------------------------------------------------------


class SpotTheMistakeChallenge(BaseModel):
    type: Literal["SPOT_MISTAKE"] = "SPOT_MISTAKE"
    instructions: str = Field(
        "اضغط على الكلمة أو الرمز الخاطئ في النص",
        min_length=1,
        max_length=240,
    )
    code_or_text: str = Field(
        ...,
        min_length=10,
        max_length=1200,
        description="Sentence, paragraph or short code block containing ONE deliberate error",
    )
    mistake_token: str = Field(
        ...,
        min_length=1,
        max_length=80,
        description="The exact substring of code_or_text that is wrong",
    )
    explanation: str = Field(
        ..., min_length=1, max_length=500, description="Why it is wrong and what it should be"
    )

    @field_validator("mistake_token")
    @classmethod
    def _token_is_clickable(cls, value: str) -> str:
        token = value.strip()
        if not token:
            raise ValueError("mistake_token cannot be blank")
        if not re.search(r"[\w\u0600-\u06FF]", token):
            raise ValueError("mistake_token must contain at least one word character")
        return token

    @model_validator(mode="after")
    def _token_occurs_exactly_once(self) -> "SpotTheMistakeChallenge":
        haystack = self.code_or_text.lower()
        needle = self.mistake_token.strip().lower()
        occurrences = haystack.count(needle)
        if occurrences != 1:
            raise ValueError(
                f"mistake_token must appear exactly once in code_or_text "
                f"(found {occurrences})"
            )
        return self

    @property
    def tokens(self) -> list[str]:
        """Whitespace-preserving tokenisation used by the click handler."""
        return re.findall(r"\S+", self.code_or_text)

    def mistake_token_indices(self) -> list[int]:
        """Every token index covered by ``mistake_token``.

        The mistake is usually one word, but a model may legitimately flag a
        phrase ("20 knots"). Matching whole tokens by substring would miss those,
        so tokens are matched by character-span overlap instead.
        """
        needle = self.mistake_token.strip()
        if not needle:
            return []
        start = self.code_or_text.lower().find(needle.lower())
        if start < 0:
            return []
        end = start + len(needle)
        return [
            index
            for index, match in enumerate(re.finditer(r"\S+", self.code_or_text))
            if match.start() < end and match.end() > start
        ]

    def token_index_of_mistake(self) -> int:
        """First clickable token index holding the mistake (-1 if none)."""
        indices = self.mistake_token_indices()
        return indices[0] if indices else -1


# ---------------------------------------------------------------------------
# 3. CONTEXTUAL_CLOZE — one blank, a word bank, instant evaluation
# ---------------------------------------------------------------------------


class ContextualClozeChallenge(BaseModel):
    type: Literal["CONTEXTUAL_CLOZE"] = "CONTEXTUAL_CLOZE"
    instructions: str = Field(
        "اختر الكلمة المناسبة لإكمال الفراغ",
        min_length=1,
        max_length=240,
    )
    sentence_with_blank: str = Field(
        ...,
        min_length=10,
        max_length=600,
        description=f"Sentence containing exactly one '{BLANK_PLACEHOLDER}' placeholder",
    )
    correct_answer: str = Field(..., min_length=1, max_length=120)
    distractors: list[str] = Field(..., min_length=2, max_length=4)
    explanation: str = Field(..., min_length=1, max_length=500)

    @field_validator("sentence_with_blank")
    @classmethod
    def _has_placeholder(cls, value: str) -> str:
        if BLANK_PLACEHOLDER not in value and "____" not in value:
            raise ValueError(f"sentence_with_blank must contain '{BLANK_PLACEHOLDER}'")
        return value

    @model_validator(mode="after")
    def _options_are_sane(self) -> "ContextualClozeChallenge":
        answer = self.correct_answer.strip().lower()
        options = [item.strip().lower() for item in self.distractors]
        if any(not item for item in options):
            raise ValueError("distractors cannot be blank")
        if len(set(options)) != len(options):
            raise ValueError("distractors must be distinct")
        if answer in options:
            raise ValueError("correct_answer must not also appear in distractors")
        return self

    @property
    def options(self) -> list[str]:
        """Word bank: the correct answer plus the distractors (unshuffled)."""
        return [self.correct_answer, *self.distractors]


# ---------------------------------------------------------------------------
# 4. INTERACTIVE_STORY — chat-style branching scenario
# ---------------------------------------------------------------------------


class StoryOption(BaseModel):
    text: str = Field(..., min_length=1, max_length=300, description="What the learner says or does")
    correct: bool
    feedback: str = Field(..., min_length=1, max_length=400, description="Character reaction")


class StoryNode(BaseModel):
    speaker: str = Field(..., min_length=1, max_length=80)
    text: str = Field(..., min_length=1, max_length=900)
    action_prompt: str = Field(..., min_length=1, max_length=300)
    options: list[StoryOption] = Field(..., min_length=2, max_length=4)

    @model_validator(mode="after")
    def _exactly_one_correct(self) -> "StoryNode":
        correct = [option for option in self.options if option.correct]
        if len(correct) != 1:
            raise ValueError("each dialogue node must have exactly one correct option")
        return self


class InteractiveStoryChallenge(BaseModel):
    type: Literal["INTERACTIVE_STORY"] = "INTERACTIVE_STORY"
    instructions: str = Field(
        "اختر القرار الصحيح في كل خطوة من الحوار",
        min_length=1,
        max_length=240,
    )
    scenario_title: str = Field(..., min_length=1, max_length=160)
    dialogue: list[StoryNode] = Field(..., min_length=2, max_length=6)


ChallengePayload = Annotated[
    Union[
        MatchMadnessChallenge,
        SpotTheMistakeChallenge,
        ContextualClozeChallenge,
        InteractiveStoryChallenge,
    ],
    Field(discriminator="type"),
]

PAYLOAD_MODEL_BY_TYPE: dict[str, type[BaseModel]] = {
    "MATCH_MADNESS": MatchMadnessChallenge,
    "SPOT_MISTAKE": SpotTheMistakeChallenge,
    "CONTEXTUAL_CLOZE": ContextualClozeChallenge,
    "INTERACTIVE_STORY": InteractiveStoryChallenge,
}


def validate_payload(challenge_type: str, payload: dict) -> BaseModel:
    """Validate + normalise a raw payload dict against its challenge model."""
    model = PAYLOAD_MODEL_BY_TYPE.get(challenge_type)
    if model is None:
        raise ValueError(f"unknown challenge type: {challenge_type}")
    return model.model_validate({**payload, "type": challenge_type})


def json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """``model.model_json_schema()`` with ``$defs``/``$ref`` inlined.

    Semantically identical for constrained decoding, but Ollama builds older
    than 0.5 reject ``$ref`` indirection outright, so the refs are flattened
    and the discriminator is pinned with ``const``.
    """
    raw = model.model_json_schema()
    schema = _inline_refs(raw, raw.get("$defs", {}), raw)
    schema.pop("title", None)
    schema.pop("description", None)
    _strip_discriminator(schema)
    return schema


def _strip_discriminator(node: Any, depth: int = 0) -> None:
    """Drop pydantic's tagged-union ``discriminator.mapping``.

    The mapping values are ``#/$defs/...`` pointers that survive ref-inlining as
    dangling strings, and Ollama's constrained decoder ignores the mapping
    anyway because each branch pins its own ``const`` on ``type``.
    """
    if depth > 24:
        return
    if isinstance(node, dict):
        discriminator = node.pop("discriminator", None)
        if isinstance(discriminator, dict) and "propertyName" in discriminator:
            node["discriminator"] = {"propertyName": discriminator["propertyName"]}
        for value in node.values():
            _strip_discriminator(value, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _strip_discriminator(item, depth + 1)


def _inline_refs(node: Any, defs: dict[str, Any], root: dict[str, Any], depth: int = 0) -> Any:
    if depth > 24:
        return node
    if isinstance(node, dict):
        if isinstance(node.get("$ref"), str):
            ref = node["$ref"]
            if ref.startswith("#/$defs/"):
                target = copy.deepcopy(defs.get(ref.split("/")[-1], {}))
                merged = {key: value for key, value in node.items() if key != "$ref"}
                target.update(merged)
                return _inline_refs(target, defs, root, depth + 1)
            return {"type": "object"}
        return {
            key: _inline_refs(value, defs, root, depth + 1)
            for key, value in node.items()
            if key != "$defs"
        }
    if isinstance(node, list):
        return [_inline_refs(item, defs, root, depth + 1) for item in node]
    return node


# ---------------------------------------------------------------------------
# Learning unit bundle — the single structured artefact Qwen must produce
# ---------------------------------------------------------------------------


class LearningUnitBundle(BaseModel):
    """One lecture chunk turned into a pedagogically coherent unit."""

    unit_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12], max_length=64)
    concept_title: str = Field(..., min_length=1, max_length=200)
    difficulty_level: int = Field(..., ge=1, le=5, description="1=introductory, 5=advanced")
    challenges: list[ChallengePayload] = Field(..., min_length=2, max_length=8)

    @model_validator(mode="after")
    def _distinct_types(self) -> "LearningUnitBundle":
        types = [challenge.type for challenge in self.challenges]  # type: ignore[union-attr]
        duplicates = {item for item in types if types.count(item) > 1}
        if duplicates:
            raise ValueError(f"duplicate challenge types in one unit: {sorted(duplicates)}")
        return self

    def challenge_payloads(self) -> list[tuple[str, dict]]:
        """``[(challenge_type, payload_dict), ...]`` ready for persistence."""
        return [
            (challenge.type, challenge.model_dump(mode="json"))  # type: ignore[union-attr]
            for challenge in self.challenges
        ]


# ---------------------------------------------------------------------------
# LLM-facing helpers
# ---------------------------------------------------------------------------


class GeneratedChallenge(BaseModel):
    """A single validated challenge produced by the fallback per-type path."""

    challenge_type: ChallengeType
    difficulty: float = Field(0.3, ge=0.0, le=1.0)
    payload: ChallengePayload


class NodeBlueprint(BaseModel):
    """One chunk of lecture text after parsing, ready for generation."""

    order_index: int
    title: str
    content: str


# ---------------------------------------------------------------------------
# API request models
# ---------------------------------------------------------------------------


class ReviewSubmission(BaseModel):
    """POST /api/reviews — one graded challenge attempt."""

    user_id: str = Field(..., min_length=1, max_length=64)
    challenge_id: str = Field(..., min_length=1, max_length=64)
    rating: int = Field(..., ge=1, le=4, description="1=Again, 2=Hard, 3=Good, 4=Easy")
    elapsed_ms: int = Field(0, ge=0, le=3_600_000)
    response_json: Optional[dict] = None

    @field_validator("user_id", "challenge_id")
    @classmethod
    def _valid_uuid(cls, value: str) -> str:
        try:
            return str(uuid.UUID(value))
        except (ValueError, AttributeError) as exc:
            raise ValueError("must be a UUID string") from exc


class UserUpsertRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    league_tier: int = Field(0, ge=0, le=9)

    @field_validator("email")
    @classmethod
    def _looks_like_email(cls, value: str) -> str:
        value = value.strip().lower()
        if "@" not in value or value.startswith("@") or value.endswith("@"):
            raise ValueError("invalid email")
        return value


class CourseCreateRequest(BaseModel):
    user_id: uuid.UUID
    name: str = Field(..., min_length=1, max_length=200)
    lms_type: str = Field("manual", max_length=40)
    lms_course_id: Optional[str] = Field(None, max_length=200)
    sync_token: Optional[str] = Field(None, max_length=400)


# ---------------------------------------------------------------------------
# API response models
# ---------------------------------------------------------------------------


class ChallengeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    node_id: uuid.UUID
    type: str
    payload_json: dict
    difficulty: float


class NodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    lecture_id: uuid.UUID
    title: str
    content_summary: str
    order_index: int
    challenges: list[ChallengeOut] = []


class ProcessLectureResponse(BaseModel):
    lecture_id: uuid.UUID
    node: dict
    challenges: list[dict]
    cards_seeded: int
    embeddings_stored: int
    challenges_by_type: dict[str, int]
    generation_errors: list[str] = []
    elapsed_ms: int


class ReviewResponse(BaseModel):
    challenge_id: uuid.UUID
    user_id: uuid.UUID
    rating: int
    state: int
    state_name: str
    stability: float
    difficulty: float
    retrievability: Optional[float]
    due_date: datetime
    interval_days: int
    reps: int
    lapses: int
    xp_awarded: int
    hearts_lost: int
    streak_count: int
    gems: int
    xp_points: int
    league_tier: int


class QueueItem(BaseModel):
    card_id: uuid.UUID
    challenge_id: uuid.UUID
    node_id: uuid.UUID
    node_title: str
    lecture_id: uuid.UUID
    type: str
    payload_json: dict
    state: int
    stability: float
    difficulty: float
    due_date: datetime
    retrievability: Optional[float]
    is_mistake: bool
    lapses: int


class StatsOut(BaseModel):
    user_id: uuid.UUID
    email: str
    xp_points: int
    gems: int
    streak_count: int
    league_tier: int
    cards_total: int
    cards_due: int
    cards_relearning: int
    accuracy_last_50: float


__all__ = [
    "ALL_CHALLENGE_TYPES",
    "BLANK_PLACEHOLDER",
    "ChallengeType",
    "ChallengePayload",
    "MatchPair",
    "MatchMadnessChallenge",
    "SpotTheMistakeChallenge",
    "ContextualClozeChallenge",
    "StoryOption",
    "StoryNode",
    "InteractiveStoryChallenge",
    "LearningUnitBundle",
    "GeneratedChallenge",
    "NodeBlueprint",
    "PAYLOAD_MODEL_BY_TYPE",
    "validate_payload",
    "json_schema_for",
    "ReviewSubmission",
    "ReviewResponse",
    "UserUpsertRequest",
    "CourseCreateRequest",
    "ChallengeOut",
    "NodeOut",
    "ProcessLectureResponse",
    "QueueItem",
    "StatsOut",
    "ValidationError",
]
