"""Pydantic v2 contracts for the CampusLingo core-ai service.

Two families of models live here:

* The four ``*Challenge`` payloads plus :class:`LearningUnitBundle` — these are
  what the model is constrained to emit (``LearningUnitBundle.model_json_schema()``
  is handed to llama-server as ``response_format.json_schema``), what is stored verbatim in
  ``challenges.payload_json``, and what the Next.js components render.
* ``*Request`` / ``*Response`` models — the HTTP API contract.

Field names here are the single source of truth: ``apps/web/lib/api.ts`` mirrors
them one-to-one.
"""

from __future__ import annotations

import copy
import logging
import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

logger = logging.getLogger("campuslingo.schema")

# ---------------------------------------------------------------------------
# Challenge type registry
# ---------------------------------------------------------------------------

ChallengeType = Literal[
    "MATCH_MADNESS",
    "SPOT_MISTAKE",
    "CONTEXTUAL_CLOZE",
    "MCQ_QUIZ",
    "TRUE_FALSE",
    "SWIPE_BOOLEAN",
    "SPOT_IMPOSTER",
    "TAP_TO_FILL",
    "BOSS_FIGHT",
    "DIAGRAM_QA",
]

ALL_CHALLENGE_TYPES: tuple[str, ...] = (
    "MATCH_MADNESS",
    "SPOT_MISTAKE",
    "CONTEXTUAL_CLOZE",
    "MCQ_QUIZ",
    "TRUE_FALSE",
    "SWIPE_BOOLEAN",
    "SPOT_IMPOSTER",
    "TAP_TO_FILL",
    "BOSS_FIGHT",
    "DIAGRAM_QA",
)

# The high-yield generation surface: the four classic pedagogical variants
# PLUS the micro-dopamine games built for short attention spans. The former
# INTERACTIVE_STORY/INTERACTIVE_CHAT (chat-style branching) types have been
# fully retired and TRUE_FALSE is legacy-only: all stay renderable for
# pre-existing rows but the chat types are not even renderable anymore.
GENERATION_CHALLENGE_TYPES: tuple[str, ...] = (
    "MCQ_QUIZ",
    "SPOT_MISTAKE",
    "CONTEXTUAL_CLOZE",
    "MATCH_MADNESS",
    "SWIPE_BOOLEAN",
    "SPOT_IMPOSTER",
    "TAP_TO_FILL",
)

BLANK_PLACEHOLDER = "_____"


# ---------------------------------------------------------------------------
# 1. MATCH_MADNESS — 4-6 term/definition pairs, 60 second drill
# ---------------------------------------------------------------------------


def _one_liner_field(description: str) -> Any:
    """Shared optional ``one_liner_rationale`` field for classic payloads.

    The micro-dopamine spec wants punchy one-sentence mistake feedback
    everywhere; legacy rows persisted before the field existed simply lack the
    key, so it defaults to empty and the UI falls back to ``explanation``.
    """
    return Field("", max_length=300, description=description)


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
    one_liner_rationale: str = _one_liner_field(
        "Punchy single-sentence takeaway shown after the drill"
    )

    @model_validator(mode="after")
    def _unique_sides(self) -> "MatchMadnessChallenge":
        terms = [pair.term.strip().lower() for pair in self.pairs]
        definitions = [pair.definition.strip().lower() for pair in self.pairs]
        if len(set(terms)) != len(terms):
            raise ValueError("MATCH_MADNESS: duplicate 'term' entries")
        if len(set(definitions)) != len(definitions):
            raise ValueError("MATCH_MADNESS: duplicate 'definition' entries")
        return self


def _one_liner_field(description: str) -> Any:
    """Shared optional ``one_liner_rationale`` field for classic payloads.

    The micro-dopamine spec wants punchy one-sentence mistake feedback
    everywhere; legacy rows persisted before the field existed simply lack the
    key, so it defaults to empty and the UI falls back to ``explanation``.
    """
    return Field("", max_length=300, description=description)


# ---------------------------------------------------------------------------
# 2. SPOT_MISTAKE — click the wrong token inside a sentence or code block
# ---------------------------------------------------------------------------

# Punctuation stripped when comparing a ``mistake_token`` against the words of
# ``code_or_text`` (models routinely include/omit a trailing comma).
_TOKEN_PUNCT = ".,;:!?()[]{}\"'`«»…"


def _levenshtein(a: str, b: str) -> int:
    """Iterative edit distance (small inputs; O(len(a) * len(b)))."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,                  # deletion
                    current[j - 1] + 1,               # insertion
                    previous[j - 1] + (ca != cb),     # substitution
                )
            )
        previous = current
    return previous[-1]


def closest_mistake_token(text: str, token: str) -> str:
    """Nearest word in ``text`` to ``token`` (case-insensitive, then edit distance).

    Used to repair a SPOT_MISTAKE whose ``mistake_token`` is not a verbatim
    substring of ``code_or_text`` — the model paraphrased it, changed case, or
    dropped punctuation. Returns a token that IS a substring of ``text`` so the
    click handler always has a target, or ``token`` unchanged when ``text`` has
    no words at all.
    """
    words = re.findall(r"\S+", text or "")
    if not words:
        return token
    needle = (token or "").strip().lower().strip(_TOKEN_PUNCT)
    if not needle:
        return token
    for word in words:
        if word.lower().strip(_TOKEN_PUNCT) == needle:
            return word
    for word in words:
        bare = word.lower().strip(_TOKEN_PUNCT)
        if bare and (needle in bare or bare in needle):
            return word
    return min(
        words,
        key=lambda word: (
            _levenshtein(needle, word.lower().strip(_TOKEN_PUNCT)),
            -len(word),
        ),
    )


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
    one_liner_rationale: str = _one_liner_field(
        "Punchy single-sentence correction shown on a miss (falls back to explanation)"
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
    def _ensure_clickable_token(self) -> "SpotTheMistakeChallenge":
        """Leniently normalise ``mistake_token`` — never raise, never retry.

        The strict "must appear exactly once" rule burned the 3-attempt repair
        loop (~2 minutes) whenever the model duplicated a phrase or paraphrased
        it. Instead:

        * present exactly once -> unchanged;
        * present multiple times -> unchanged; the click handler targets the
          first occurrence and the UI accepts any of them as correct (a debug
          notice is logged);
        * absent entirely -> repaired to the closest word in ``code_or_text``
          (case-insensitive, then Levenshtein) so the challenge stays playable.
        """
        haystack = self.code_or_text
        token = self.mistake_token.strip()
        needle = token.lower()
        occurrences = haystack.lower().count(needle) if needle else 0
        if occurrences >= 1:
            # Case/punctuation drift: store the VERBATIM substring from the text
            # so the payload keeps the "exact substring of code_or_text" contract
            # the renderer relies on.
            if token not in haystack:
                start = haystack.lower().find(needle)
                if start >= 0:
                    self.mistake_token = haystack[start : start + len(token)]
            if occurrences > 1:
                logger.debug(
                    "SPOT_MISTAKE token %r occurs %d times; keeping the first occurrence",
                    token,
                    occurrences,
                )
            return self
        repaired = closest_mistake_token(haystack, token)
        if repaired and repaired != token:
            self.mistake_token = repaired
            logger.info(
                "SPOT_MISTAKE token %r not found in code_or_text; repaired to %r",
                token,
                repaired,
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

# Model-emitted blank variants that normalise to the canonical ``_____``:
# runs of 2+ underscores, ellipses, bracketed/placeholder-marked gaps, and
# HTML/BBCode hole markers that survive from the source slides. Order matters:
# the bracketed alternatives are tried before the bare ellipsis so ``[...]``
# collapses as one placeholder instead of leaving stray brackets.
_BLANK_VARIANTS_RE = re.compile(
    r"_{2,}"                                     # ___, ____, _____, ________
    r"|\[\s*(?:blank|gap|\.{2,}|…|\?+)?\s*\]"     # [blank] [...] [???] []
    r"|\(\s*(?:blank|gap|\.{2,}|…|\?+|_+)?\s*\)"  # (blank) (...) (___) ( )
    r"|<\s*(?:blank|gap|\.{2,}|…|\?+)\s*>"      # <blank> <gap> <...>
    r"|\{\s*blank\s*\}"                         # {blank}
    r"|\.{3,}"                                   # ... ellipsis placeholder
    r"|…",                                        # … unicode ellipsis
    re.IGNORECASE,
)


def normalize_blank(sentence: str) -> str:
    """Rewrite any blank-marker variant into the canonical ``_____``.

    The prompt asks for ``_____`` but small models love ``____``, ``[blank]``,
    ``...``, ``( )`` or ``<blank>``; rejecting those burned 3 repair turns
    (~2 minutes) per cloze challenge. Normalising is free and deterministic.
    """
    return _BLANK_VARIANTS_RE.sub(BLANK_PLACEHOLDER, sentence or "")


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
    one_liner_rationale: str = _one_liner_field(
        "Punchy single-sentence takeaway shown on a wrong tap"
    )

    @field_validator("sentence_with_blank", mode="before")
    @classmethod
    def _normalize_placeholder(cls, value: str) -> str:
        """Auto-sanitise blank markers BEFORE validation (never reject them)."""
        return normalize_blank(value)

    @field_validator("sentence_with_blank")
    @classmethod
    def _has_placeholder(cls, value: str) -> str:
        # Runs this point, normalize_blank has rewritten every variant to the
        # canonical ``_____`` — reject only when NO placeholder of any kind existed.
        if BLANK_PLACEHOLDER not in value:
            raise ValueError(
                f"sentence_with_blank must contain a blank placeholder "
                f"(____, [blank], <blank>, ...) — none found"
            )
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
# 4. MCQ_QUIZ — classic multiple-choice question with instant feedback
# ---------------------------------------------------------------------------


class McqOption(BaseModel):
    """One answer choice; exactly one option in a question is correct."""

    text: str = Field(..., min_length=1, max_length=300)
    correct: bool = False


class McqQuizChallenge(BaseModel):
    type: Literal["MCQ_QUIZ"] = "MCQ_QUIZ"
    instructions: str = Field(
        "اختر الإجابة الصحيحة",
        min_length=1,
        max_length=240,
    )
    question: str = Field(..., min_length=5, max_length=600)
    options: list[McqOption] = Field(..., min_length=2, max_length=6)
    explanation: str = Field(..., min_length=1, max_length=500)
    one_liner_rationale: str = _one_liner_field(
        "Punchy single-sentence takeaway shown on a wrong pick"
    )

    @model_validator(mode="after")
    def _options_are_sane(self) -> "McqQuizChallenge":
        texts = [option.text.strip().lower() for option in self.options]
        if any(not text for text in texts):
            raise ValueError("options cannot be blank")
        if len(set(texts)) != len(texts):
            raise ValueError("options must be distinct")
        if sum(1 for option in self.options if option.correct) != 1:
            raise ValueError("exactly one option must be correct")
        return self

    @property
    def correct_answer(self) -> str:
        """Text of the single correct option (used by the UI + stats)."""
        return next(option.text for option in self.options if option.correct)


# ---------------------------------------------------------------------------
# 5. TRUE_FALSE — one statement, judge it, see why (legacy type: rendered for
#    pre-existing rows, no longer part of the default generation surface)
# ---------------------------------------------------------------------------


class TrueFalseChallenge(BaseModel):
    type: Literal["TRUE_FALSE"] = "TRUE_FALSE"
    instructions: str = Field(
        "هل العبارة صحيحة أم خاطئة؟",
        min_length=1,
        max_length=240,
    )
    statement: str = Field(..., min_length=5, max_length=600)
    answer: bool
    explanation: str = Field(..., min_length=1, max_length=500)
    one_liner_rationale: str = _one_liner_field(
        "Punchy single-sentence verdict takeaway"
    )


# ---------------------------------------------------------------------------
# 5b. SWIPE_BOOLEAN — "Tinder for study": crisp conceptual statement, swipe
#     right (True) or left (False) in a 3-second rapid-fire loop. The legacy
#     TRUE_FALSE payload survives for old rows; SWIPE_BOOLEAN is the modern
#     micro-dopamine variant with a hard 140-char statement ceiling.
# ---------------------------------------------------------------------------


class SwipeBooleanChallenge(BaseModel):
    type: Literal["SWIPE_BOOLEAN"] = "SWIPE_BOOLEAN"
    instructions: str = Field(
        "اسحب يميناً إذا كانت العبارة صحيحة، ويساراً إذا كانت خاطئة",
        min_length=1,
        max_length=240,
    )
    statement: str = Field(
        ...,
        min_length=5,
        max_length=140,
        description="One crisp conceptual statement — short enough for a 3-second swipe",
    )
    answer: bool
    one_liner_rationale: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Punchy single-sentence explanation shown on a wrong swipe",
    )

    @field_validator("statement")
    @classmethod
    def _statement_is_a_single_crisp_line(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if len(cleaned) > 140:
            raise ValueError("statement must stay under 140 characters for the swipe loop")
        return cleaned


# ---------------------------------------------------------------------------
# 5c. SPOT_IMPOSTER — 1 false among 3 truths: four short factual statements,
#     one carries a subtly altered concept; the student taps the imposter.
# ---------------------------------------------------------------------------


class ImposterStatement(BaseModel):
    """One of the four statements; exactly one per set is the imposter."""

    text: str = Field(..., min_length=5, max_length=200)
    is_imposter: bool = False


class SpotImposterChallenge(BaseModel):
    type: Literal["SPOT_IMPOSTER"] = "SPOT_IMPOSTER"
    instructions: str = Field(
        "ثلاث عبارات صحيحة وواحدة مفبركة — اضغط على المستحيل!",
        min_length=1,
        max_length=240,
    )
    statements: list[ImposterStatement] = Field(..., min_length=4, max_length=4)
    one_liner_rationale: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Why the imposter is wrong (one punchy sentence)",
    )

    @model_validator(mode="after")
    def _exactly_one_imposter(self) -> "SpotImposterChallenge":
        texts = [item.text.strip().lower() for item in self.statements]
        if any(not text for text in texts):
            raise ValueError("SPOT_IMPOSTER: statements cannot be blank")
        if len(set(texts)) != len(texts):
            raise ValueError("SPOT_IMPOSTER: statements must be distinct")
        if sum(1 for item in self.statements if item.is_imposter) != 1:
            raise ValueError("SPOT_IMPOSTER: exactly one statement must be the imposter")
        return self

    @property
    def imposter_index(self) -> int:
        return next(i for i, item in enumerate(self.statements) if item.is_imposter)


# ---------------------------------------------------------------------------
# 5d. TAP_TO_FILL — one-touch cloze: a short sentence with one blank and three
#     pill buttons at the bottom; one tap fills the gap. A compact sibling of
#     CONTEXTUAL_CLOZE for the rapid-fire loop (explanation stays one-liner).
# ---------------------------------------------------------------------------


class TapToFillChallenge(BaseModel):
    type: Literal["TAP_TO_FILL"] = "TAP_TO_FILL"
    instructions: str = Field(
        "اضغط على الكلمة الناقصة",
        min_length=1,
        max_length=240,
    )
    sentence_with_blank: str = Field(
        ...,
        min_length=10,
        max_length=240,
        description=f"Short sentence containing exactly one '{BLANK_PLACEHOLDER}' placeholder",
    )
    correct_answer: str = Field(..., min_length=1, max_length=80)
    distractors: list[str] = Field(..., min_length=2, max_length=2)
    one_liner_rationale: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Punchy single-sentence explanation shown on a wrong tap",
    )

    @field_validator("sentence_with_blank", mode="before")
    @classmethod
    def _normalize_placeholder(cls, value: str) -> str:
        """Auto-sanitise blank markers BEFORE validation (never reject them)."""
        return normalize_blank(value)

    @field_validator("sentence_with_blank")
    @classmethod
    def _has_placeholder(cls, value: str) -> str:
        if BLANK_PLACEHOLDER not in value:
            raise ValueError(
                "sentence_with_blank must contain a blank placeholder (____)"
            )
        return value

    @model_validator(mode="after")
    def _options_are_sane(self) -> "TapToFillChallenge":
        answer = self.correct_answer.strip().lower()
        options = [item.strip().lower() for item in self.distractors]
        if any(not item for item in options):
            raise ValueError("TAP_TO_FILL: distractors cannot be blank")
        if len(set(options)) != len(options):
            raise ValueError("TAP_TO_FILL: distractors must be distinct")
        if answer in options:
            raise ValueError("TAP_TO_FILL: correct_answer must not appear in distractors")
        return self

    @property
    def options(self) -> list[str]:
        """The three pills: the correct answer plus its two distractors."""
        return [self.correct_answer, *self.distractors]


# ---------------------------------------------------------------------------
# 5e. BOSS_FIGHT — chapter culmination: ~10 rapid-fire questions synthesized
#     across the chapter, answered against a 15-second-per-question countdown
#     and a boss HP bar that depletes on every correct answer.
# ---------------------------------------------------------------------------


class BossQuestion(BaseModel):
    """One rapid-fire boss question (kept deliberately tiny)."""

    question: str = Field(..., min_length=5, max_length=240)
    options: list[McqOption] = Field(..., min_length=2, max_length=4)

    @model_validator(mode="after")
    def _one_correct_option(self) -> "BossQuestion":
        texts = [option.text.strip().lower() for option in self.options]
        if any(not text for text in texts):
            raise ValueError("BOSS_FIGHT: options cannot be blank")
        if len(set(texts)) != len(texts):
            raise ValueError("BOSS_FIGHT: options must be distinct")
        if sum(1 for option in self.options if option.correct) != 1:
            raise ValueError("BOSS_FIGHT: exactly one option must be correct")
        return self


class BossFightChallenge(BaseModel):
    type: Literal["BOSS_FIGHT"] = "BOSS_FIGHT"
    instructions: str = Field(
        "معركة الزعيم: 10 أسئلة خاطفة — أفرغ شريط الصحة قبل نهاية الوقت!",
        min_length=1,
        max_length=240,
    )
    boss_name: str = Field(..., min_length=1, max_length=80)
    boss_emoji: str = Field("🐲", min_length=1, max_length=8)
    seconds_per_question: int = Field(15, ge=5, le=60)
    questions: list[BossQuestion] = Field(..., min_length=4, max_length=12)
    one_liner_rationale: str = Field(
        "",
        max_length=200,
        description="Optional chapter-level closing line after the boss falls",
    )

    @model_validator(mode="after")
    def _questions_are_distinct(self) -> "BossFightChallenge":
        prompts = [item.question.strip().lower() for item in self.questions]
        if len(set(prompts)) != len(prompts):
            raise ValueError("BOSS_FIGHT: questions must be distinct")
        return self


# ---------------------------------------------------------------------------
# 7. DIAGRAM_QA — a slide figure plus comprehension questions about it
# ---------------------------------------------------------------------------


class DiagramQuestion(BaseModel):
    """One comprehension question about the extracted diagram."""

    question: str = Field(..., min_length=5, max_length=400)
    options: list[McqOption] = Field(..., min_length=2, max_length=6)
    # component_identification | data_flow | graph_trend | comparison
    focus: Literal[
        "component_identification", "data_flow", "graph_trend", "comparison"
    ] = "component_identification"

    @model_validator(mode="after")
    def _one_correct_option(self) -> "DiagramQuestion":
        texts = [option.text.strip().lower() for option in self.options]
        if any(not text for text in texts):
            raise ValueError("DIAGRAM_QA: options cannot be blank")
        if len(set(texts)) != len(texts):
            raise ValueError("DIAGRAM_QA: options must be distinct")
        if sum(1 for option in self.options if option.correct) != 1:
            raise ValueError("DIAGRAM_QA: exactly one option must be correct")
        return self

    @property
    def correct_answer(self) -> str:
        return next(option.text for option in self.options if option.correct)


class DiagramQaChallenge(BaseModel):
    type: Literal["DIAGRAM_QA"] = "DIAGRAM_QA"
    instructions: str = Field(
        "تأمّل الرسم ثم أجب عن السؤال",
        min_length=1,
        max_length=240,
    )
    # Public URL of the extracted figure (static/uploads/diagrams/<course>/...).
    # ``diagram_ref`` is the server-side key (page-index) used to re-link it.
    image_url: str = Field(..., min_length=4, max_length=500)
    diagram_ref: str = Field("", max_length=200)
    caption: str = Field("", max_length=300)
    questions: list[DiagramQuestion] = Field(..., min_length=1, max_length=3)
    explanation: str = Field(..., min_length=1, max_length=600)
    one_liner_rationale: str = _one_liner_field(
        "Punchy single-sentence takeaway about the figure"
    )

    @property
    def correct_answer(self) -> str:
        return self.questions[0].correct_answer


ChallengePayload = Annotated[
    Union[
        MatchMadnessChallenge,
        SpotTheMistakeChallenge,
        ContextualClozeChallenge,
        McqQuizChallenge,
        TrueFalseChallenge,
        SwipeBooleanChallenge,
        SpotImposterChallenge,
        TapToFillChallenge,
        BossFightChallenge,
        DiagramQaChallenge,
    ],
    Field(discriminator="type"),
]

PAYLOAD_MODEL_BY_TYPE: dict[str, type[BaseModel]] = {
    "MATCH_MADNESS": MatchMadnessChallenge,
    "SPOT_MISTAKE": SpotTheMistakeChallenge,
    "CONTEXTUAL_CLOZE": ContextualClozeChallenge,
    "MCQ_QUIZ": McqQuizChallenge,
    "TRUE_FALSE": TrueFalseChallenge,
    "SWIPE_BOOLEAN": SwipeBooleanChallenge,
    "SPOT_IMPOSTER": SpotImposterChallenge,
    "TAP_TO_FILL": TapToFillChallenge,
    "BOSS_FIGHT": BossFightChallenge,
    "DIAGRAM_QA": DiagramQaChallenge,
}


def validate_payload(challenge_type: str, payload: dict) -> BaseModel:
    """Validate + normalise a raw payload dict against its challenge model."""
    model = PAYLOAD_MODEL_BY_TYPE.get(challenge_type)
    if model is None:
        raise ValueError(f"unknown challenge type: {challenge_type}")
    return model.model_validate({**payload, "type": challenge_type})


def json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """``model.model_json_schema()`` with ``$defs``/``$ref`` inlined.

    llama-server's grammar converter handles ``$ref``, but inlining keeps the
    emitted GBNF smaller and the discriminator is pinned with ``const`` so the
    ``type`` field can never drift.
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
    dangling strings, and the grammar ignores the mapping anyway because each
    branch pins its own ``const`` on ``type``.
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
    # Content-adaptive challenge count: the generator grows the set with the
    # lecture's actual density (a short summary lands around 6-10, a dense part
    # around 16-24). 24 is a hard safety bound against runaway loops, not a
    # pedagogical target.
    challenges: list[ChallengePayload] = Field(..., min_length=2, max_length=24)

    @model_validator(mode="after")
    def _challenge_types_are_known(self) -> "LearningUnitBundle":
        """Every challenge type must be one of the four known kinds.

        The "cover ALL four types" pedagogical target is enforced where
        generation can still react to it (the bundle generator's validator
        loop); the persistence layer must accept whatever the fallback
        composed — one type failing all its attempts should degrade the
        node's variety, never fail the whole save.
        """
        types = [challenge.type for challenge in self.challenges]  # type: ignore[union-attr]
        unknown = sorted(set(types) - set(ALL_CHALLENGE_TYPES))
        if unknown:
            raise ValueError(f"bundle contains unknown challenge types: {unknown}")
        return self

    @property
    def missing_challenge_types(self) -> list[str]:
        """Pedagogical coverage gaps (informational — does not block saving)."""
        types = [challenge.type for challenge in self.challenges]  # type: ignore[union-attr]
        return sorted(set(ALL_CHALLENGE_TYPES) - set(types))

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
    # Where the attempt came from: "warmup" = the Daily Memory Warm-up ritual,
    # which earns the 1.5x XP bonus (``WARMUP_XP_MULTIPLIER``). Any other/absent
    # value is a plain lesson review with the usual streak-scaled XP.
    context: Optional[str] = Field(None, max_length=32)

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
    "GENERATION_CHALLENGE_TYPES",
    "BLANK_PLACEHOLDER",
    "ChallengeType",
    "ChallengePayload",
    "MatchPair",
    "MatchMadnessChallenge",
    "SpotTheMistakeChallenge",
    "ContextualClozeChallenge",
    "McqOption",
    "McqQuizChallenge",
    "TrueFalseChallenge",
    "SwipeBooleanChallenge",
    "ImposterStatement",
    "SpotImposterChallenge",
    "TapToFillChallenge",
    "BossQuestion",
    "BossFightChallenge",
    "DiagramQuestion",
    "DiagramQaChallenge",
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
