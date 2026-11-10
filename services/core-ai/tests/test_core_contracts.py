"""Unit contracts: FSRS-4.5, challenge payloads, the Ollama JSON Schema, ingestion.

No database, no LLM server: everything here is pure and fast, so it runs on every
commit. PostgreSQL-backed behaviour lives in ``test_api_e2e.py``.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from fsrs.scheduler import (
    STATE_LEARNING,
    STATE_NEW,
    STATE_RELEARNING,
    STATE_REVIEW,
    CardSnapshot,
    FSRS4_5,
    FSRSScheduler,
    rating_from_result,
    retrievability,
)
from ingestion.parser import (
    chunk_text,
    clean_text,
    extract_pdf_content,
    extract_pdf_pages_meta,
    fix_kerning_spaces,
    iterate_chunks,
    parse_pdf,
)
from llm.generator import (
    _estimate_difficulty,
    _extract_json,
    _fit_dim,
    build_bundle_schema,
    build_schema,
)
from schema import (
    ContextualClozeChallenge,
    LearningUnitBundle,
    MatchMadnessChallenge,
    McqQuizChallenge,
    ReviewSubmission,
    SpotTheMistakeChallenge,
    TrueFalseChallenge,
    json_schema_for,
    validate_payload,
)

NOW = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. FSRS-4.5 core equations
# ---------------------------------------------------------------------------


def test_new_card_good_grades_to_review_with_w2_stability():
    result = FSRS4_5().review(CardSnapshot(), 3, NOW)
    assert result.state == STATE_REVIEW
    assert result.stability == pytest.approx(3.1262, abs=1e-6)


def test_new_card_again_enters_learning_with_one_minute_step():
    result = FSRS4_5().review(CardSnapshot(), 1, NOW)
    assert result.state == STATE_LEARNING
    assert result.interval_days * 1440 == pytest.approx(1.0, abs=1e-3)


@pytest.mark.parametrize("rating", [1, 2, 3, 4])
def test_every_rating_is_accepted_and_bounded(rating: int):
    scheduler = FSRS4_5()
    if rating == 1:
        result = scheduler.review(CardSnapshot(state=STATE_REVIEW, stability=10, difficulty=5,
                                                last_review=NOW - timedelta(days=5)),
                                  rating, NOW)
        assert result.state == STATE_RELEARNING
    else:
        result = scheduler.review(CardSnapshot(), rating, NOW)
        assert result.state == STATE_REVIEW
    assert 1.0 <= result.difficulty <= 10.0
    assert result.due_date > NOW


def test_rating_preference_ordering_easy_gt_good_gt_hard():
    scheduler = FSRS4_5()
    intervals = {rating: scheduler.review(CardSnapshot(), rating, NOW).interval_days
                 for rating in (2, 3, 4)}
    assert intervals[4] > intervals[3] > intervals[2]


def test_on_time_recall_grows_stability_and_reports_retrievability():
    scheduler = FSRS4_5()
    first = scheduler.review(CardSnapshot(), 3, NOW)
    second = scheduler.review(
        CardSnapshot(state=first.state, stability=first.stability, difficulty=first.difficulty,
                     due_date=first.due_date, last_review=first.last_review, reps=1),
        3, first.due_date,
    )
    assert second.stability > first.stability
    assert second.interval_days > first.interval_days
    assert 0.85 < (second.retrievability or 0) < 0.95


def test_lapse_reduces_stability_and_never_increases_it():
    scheduler = FSRS4_5()
    card = CardSnapshot(state=STATE_REVIEW, stability=10.0, difficulty=5.0,
                        last_review=NOW - timedelta(days=4), reps=3)
    result = scheduler.review(card, 1, NOW)
    assert result.state == STATE_RELEARNING
    assert result.stability < card.stability
    assert result.lapses == 1


def test_retrievability_decays_monotonically():
    values = [retrievability(10.0, days) for days in (0.1, 1, 7, 30, 365)]
    assert values == sorted(values, reverse=True)
    assert 0.0 <= values[-1] <= values[0] <= 1.0


def test_late_review_sees_lower_retrievability_than_on_time():
    scheduler = FSRS4_5()
    card = CardSnapshot(state=STATE_REVIEW, stability=5.0, difficulty=5.0,
                        last_review=NOW - timedelta(days=1), reps=2)
    on_time = scheduler.review(card, 3, NOW)
    late = scheduler.review(card, 3, NOW + timedelta(days=30))
    assert (late.retrievability or 0) < (on_time.retrievability or 0)


def test_preview_returns_all_four_outcomes():
    assert len(FSRS4_5().preview(CardSnapshot(), NOW)) == 4


def test_wrong_weight_count_is_rejected():
    with pytest.raises(ValueError):
        FSRS4_5(parameters=(1.0, 2.0))


def test_invalid_rating_is_rejected():
    with pytest.raises(ValueError):
        FSRS4_5().review(CardSnapshot(), 9, NOW)


# ---------------------------------------------------------------------------
# 2. FSRSScheduler facade (the surface the API calls)
# ---------------------------------------------------------------------------


def test_facade_retrievability_signature_and_bounds():
    scheduler = FSRSScheduler()
    assert scheduler.retrievability(0.0, 10.0) == pytest.approx(1.0)
    assert scheduler.retrievability(30, 10) < scheduler.retrievability(1, 10)


def test_facade_next_interval_is_whole_days_after_stability():
    scheduler = FSRSScheduler()
    assert scheduler.next_interval(3.1262) == 3


def test_facade_update_card_first_review_math():
    new_s, new_d, interval = FSRSScheduler().update_card(3, 0.0, 7.0, 0.0)
    assert new_s == pytest.approx(3.1262, abs=1e-6)
    assert 1.0 <= new_d <= 10.0
    assert interval == 3


def test_facade_update_card_grows_on_easy_and_deflates_on_again():
    scheduler = FSRSScheduler()
    new_s, new_d, interval = scheduler.update_card(3, 0.0, 7.0, 0.0)
    easy_s, _, easy_interval = scheduler.update_card(4, new_s, new_d, 3.0)
    again_s, _, again_interval = scheduler.update_card(1, new_s, new_d, 3.0)

    assert easy_s > new_s and easy_interval > interval
    assert again_interval == 0  # relearning step, measured in minutes
    assert again_s < new_s


def test_facade_rejects_out_of_range_rating():
    with pytest.raises(ValueError):
        FSRSScheduler().update_card(9, 1.0, 5.0, 1.0)


def test_facade_due_date_for_uses_minutes_when_interval_is_zero():
    scheduler = FSRSScheduler()
    assert scheduler.due_date_for(3, NOW) - NOW == timedelta(days=3)
    assert scheduler.due_date_for(0, NOW) - NOW < timedelta(hours=1)


@pytest.mark.parametrize(
    ("correct", "elapsed_ms", "hints", "expected"),
    [
        (False, 5_000, 0, 1),
        (True, 4_200, 0, 4),
        (True, 20_000, 0, 3),
        (True, 20_000, 1, 2),
        (True, 60_000, 0, 2),
    ],
)
def test_rating_from_result_maps_attempts_to_fsrs_scale(correct, elapsed_ms, hints, expected):
    assert rating_from_result(correct, elapsed_ms, hints) == expected


# ---------------------------------------------------------------------------
# 3. Challenge payload contracts
# ---------------------------------------------------------------------------

MATCH = {
    "type": "MATCH_MADNESS",
    "instructions": "طابق كل مصطلح مع تعريفه",
    "seconds": 60,
    "pairs": [
        {"term": "COLREGs", "definition": "International collision avoidance rules"},
        {"term": "RADAR", "definition": "Radio detection and ranging sensor"},
        {"term": "Draft", "definition": "Vertical distance from keel to waterline"},
        {"term": "Beam", "definition": "Maximum width of the vessel"},
    ],
}

SPOT = {
    "type": "SPOT_MISTAKE",
    "instructions": "اضغط على الكلمة الخاطئة",
    "code_or_text": "Rule 7 requires every vessel to maintain a minimum speed of 20 knots in fog.",
    "mistake_token": "20 knots",
    "explanation": "Rule 7 concerns risk of collision; there is no 20 knot minimum in fog.",
}

CLOZE = {
    "type": "CONTEXTUAL_CLOZE",
    "instructions": "أكمل الفراغ",
    "sentence_with_blank": "Rule 6 requires every vessel to proceed at a safe _____ at all times.",
    "correct_answer": "speed",
    "distractors": ["depth", "heading", "mass"],
    "explanation": "Rule 6 is the safe speed rule.",
}

MCQ = {
    "type": "MCQ_QUIZ",
    "instructions": "اختر الإجابة الصحيحة",
    "question": "What does COLREGs stand for?",
    "options": [
        {"text": "Convention on the International Regulations for Preventing Collisions at Sea", "correct": True},
        {"text": "Coastal Line Regulations for Global Emergency Situations", "correct": False},
        {"text": "Collision Rules for Global Encounters at Sea", "correct": False},
    ],
    "explanation": "COLREGs is the collision-avoidance convention.",
}

TF_TRUE = {
    "type": "TRUE_FALSE",
    "instructions": "هل العبارة صحيحة أم خاطئة؟",
    "statement": "Rule 6 concerns safe speed.",
    "answer": True,
    "explanation": "Rule 6 is the safe-speed rule.",
}

TF_FALSE = {
    **TF_TRUE,
    "statement": "Rule 6 concerns navigation lights.",
    "answer": False,
    "explanation": "Navigation lights are Rule 23, not Rule 6.",
}

DIAGRAM = {
    "type": "DIAGRAM_QA",
    "instructions": "تأمّل الرسم ثم أجب",
    "image_url": "/static/uploads/diagrams/c1/page-3-0.png",
    "diagram_ref": "page-3-0",
    "caption": "Figure 3: Safe-speed decision flow",
    "questions": [
        {
            "question": "What does the first step of this flow chart require?",
            "focus": "data_flow",
            "options": [
                {"text": "Assess the risk of collision", "correct": True},
                {"text": "Sound one prolonged blast", "correct": False},
                {"text": "Reduce speed to 5 knots", "correct": False},
            ],
        }
    ],
    "explanation": "The flow starts from Rule 7 risk assessment.",
}

ALL_PAYLOADS = {"MATCH_MADNESS": MATCH, "SPOT_MISTAKE": SPOT,
                "CONTEXTUAL_CLOZE": CLOZE, "MCQ_QUIZ": MCQ,
                "TRUE_FALSE": TF_TRUE, "DIAGRAM_QA": DIAGRAM}

# Second variants used to build an 8-item bundle (two items per type).
MATCH2 = {
    **MATCH,
    "instructions": "طابق كل مصطلح بحري مع معناه",
    "pairs": [
        {"term": "Abaft", "definition": "Toward the stern"},
        {"term": "Abeam", "definition": "At right angles to the keel"},
        {"term": "Astern", "definition": "Behind the vessel"},
        {"term": "Aloft", "definition": "Above the deck, up the mast"},
    ],
}

SPOT2 = {
    **SPOT,
    "code_or_text": "Rule 8 requires any alteration of course to be a slow, barely noticeable change.",
    "mistake_token": "barely noticeable",
    "explanation": "Rule 8 demands positive, readily apparent action in ample time.",
}

CLOZE2 = {
    **CLOZE,
    "sentence_with_blank": "Every vessel shall use all available means to determine if risk of _____ exists.",
    "correct_answer": "collision",
    "distractors": ["grounding", "flooding", "listing"],
    "explanation": "Rule 7 concerns risk of collision.",
}


@pytest.mark.parametrize("challenge_type", sorted(ALL_PAYLOADS))
def test_each_payload_validates_against_its_model(challenge_type: str):
    payload = validate_payload(challenge_type, ALL_PAYLOADS[challenge_type])
    assert payload.type == challenge_type


def test_match_madness_keeps_term_and_definition_fields():
    payload = validate_payload("MATCH_MADNESS", MATCH)
    assert isinstance(payload, MatchMadnessChallenge)
    assert payload.pairs[0].term == "COLREGs"
    assert payload.pairs[0].definition.startswith("International")


def test_match_madness_rejects_duplicates_and_short_lists():
    with pytest.raises(Exception):
        validate_payload("MATCH_MADNESS", {**MATCH, "pairs": MATCH["pairs"] + [MATCH["pairs"][0]]})
    with pytest.raises(Exception):
        validate_payload("MATCH_MADNESS", {**MATCH, "pairs": MATCH["pairs"][:3]})


def test_spot_mistake_maps_multi_word_token_onto_clickable_tokens():
    payload = validate_payload("SPOT_MISTAKE", SPOT)
    assert isinstance(payload, SpotTheMistakeChallenge)
    assert payload.mistake_token_indices() == [11, 12]
    assert payload.token_index_of_mistake() == 11


def test_spot_mistake_single_word_token_index():
    payload = validate_payload(
        "SPOT_MISTAKE",
        {**SPOT, "code_or_text": "A vessel shall proceed at a safe speed in fog.",
         "mistake_token": "speed"},
    )
    assert payload.mistake_token_indices() == [7]


@pytest.mark.parametrize(
    ("text", "token"),
    [
        ("Rule 7 states that a minimum speed applies.", "not present"),
        ("20 knots applies and 20 knots applies again.", "20 knots"),
    ],
)
def test_spot_mistake_is_repaired_instead_of_rejected(text: str, token: str):
    """A missing or duplicated ``mistake_token`` must never raise — the schema
    repairs it (or accepts a duplicate) so generation stays on attempt 1/3."""
    payload = validate_payload(
        "SPOT_MISTAKE", {**SPOT, "code_or_text": text, "mistake_token": token}
    )
    assert isinstance(payload, SpotTheMistakeChallenge)
    # The stored token is always a real substring with a clickable target.
    assert payload.mistake_token in payload.code_or_text
    assert payload.mistake_token_indices(), payload.mistake_token


def test_spot_mistake_repairs_paraphrased_token_to_closest_word():
    """A paraphrased token (case/punctuation drift) is repaired deterministically
    instead of triggering the 3-attempt rejection loop."""
    from schema import closest_mistake_token

    text = "Rule 6 requires every vessel to proceed at a safe Speed, at all times."
    payload = validate_payload(
        "SPOT_MISTAKE", {**SPOT, "code_or_text": text, "mistake_token": "SPEED"}
    )
    # Case-insensitive matching already finds "Speed".
    assert payload.mistake_token in payload.code_or_text

    # Nothing close? The helper still returns a real word from the text.
    repaired = closest_mistake_token(text, "velocity")
    assert repaired in text
    assert repaired.strip()


def test_cloze_word_bank_is_answer_plus_distractors():
    payload = validate_payload("CONTEXTUAL_CLOZE", CLOZE)
    assert isinstance(payload, ContextualClozeChallenge)
    assert payload.options == ["speed", "depth", "heading", "mass"]


def test_cloze_rejects_answer_hidden_in_distractors_and_missing_blank():
    with pytest.raises(Exception):
        validate_payload("CONTEXTUAL_CLOZE", {**CLOZE, "distractors": ["speed", "depth", "mass"]})
    with pytest.raises(Exception):
        validate_payload("CONTEXTUAL_CLOZE", {**CLOZE, "sentence_with_blank": "no blank here"})


def test_mcq_validates_and_exposes_the_correct_answer():
    payload = validate_payload("MCQ_QUIZ", MCQ)
    assert isinstance(payload, McqQuizChallenge)
    assert payload.correct_answer.startswith("Convention on the International")


@pytest.mark.parametrize(
    "options",
    [
        # two corrects
        [
            {"text": "A", "correct": True},
            {"text": "B", "correct": True},
            {"text": "C", "correct": False},
        ],
        # none correct
        [
            {"text": "A", "correct": False},
            {"text": "B", "correct": False},
        ],
        # duplicate texts
        [
            {"text": "A", "correct": True},
            {"text": "A", "correct": False},
        ],
    ],
)
def test_mcq_rejects_broken_answer_keys(options):
    with pytest.raises(Exception):
        validate_payload("MCQ_QUIZ", {**MCQ, "options": options})


def test_true_false_accepts_both_verdicts():
    for raw in (TF_TRUE, TF_FALSE):
        payload = validate_payload("TRUE_FALSE", raw)
        assert isinstance(payload, TrueFalseChallenge)
        assert payload.answer is raw["answer"]


def test_unknown_challenge_type_is_rejected():
    with pytest.raises(ValueError):
        validate_payload("CROSSWORD", MATCH)


# ---------------------------------------------------------------------------
# 4. LearningUnitBundle + the JSON Schema handed to Ollama
# ---------------------------------------------------------------------------

# Two items per type: the bundle contract is 8-10 challenges covering every
# type (enforced by LearningUnitBundle).
BUNDLE = {
    "unit_id": "unit-1",
    "concept_title": "قواعد المناورة الآمنة (Safe Speed)",
    "difficulty_level": 3,
    "challenges": [MATCH, SPOT, CLOZE, MCQ, MATCH2, SPOT2, CLOZE2, TF_TRUE],
}


def test_bundle_validates_and_keeps_type_order():
    unit = LearningUnitBundle.model_validate(BUNDLE)
    assert [challenge.type for challenge in unit.challenges] == [
        "MATCH_MADNESS", "SPOT_MISTAKE", "CONTEXTUAL_CLOZE", "MCQ_QUIZ",
        "MATCH_MADNESS", "SPOT_MISTAKE", "CONTEXTUAL_CLOZE", "TRUE_FALSE",
    ]
    assert [kind for kind, _ in unit.challenge_payloads()] == [
        "MATCH_MADNESS", "SPOT_MISTAKE", "CONTEXTUAL_CLOZE", "MCQ_QUIZ",
        "MATCH_MADNESS", "SPOT_MISTAKE", "CONTEXTUAL_CLOZE", "TRUE_FALSE",
    ]


def test_bundle_generates_a_unit_id_when_omitted():
    unit = LearningUnitBundle.model_validate({k: v for k, v in BUNDLE.items() if k != "unit_id"})
    assert unit.unit_id


def test_bundle_missing_challenge_type_is_soft_but_reported():
    # A type failing all its generation attempts must degrade the node's
    # variety, never fail the whole save — coverage gaps are informational.
    challenges = [c for c in BUNDLE["challenges"] if c["type"] != "TRUE_FALSE"]
    assert len(challenges) == 7
    unit = LearningUnitBundle.model_validate({**BUNDLE, "challenges": challenges})
    # Every type the bundle does not cover is reported, sorted by name —
    # including the micro-game types and the ingestion-only DIAGRAM_QA.
    assert unit.missing_challenge_types == [
        "BOSS_FIGHT",
        "DIAGRAM_QA",
        "SPOT_IMPOSTER",
        "SWIPE_BOOLEAN",
        "TAP_TO_FILL",
        "TRUE_FALSE",
    ]
    # A bundle covering every type reports no gaps.
    full = {
        **BUNDLE,
        "challenges": [
            *BUNDLE["challenges"],
            {**MCQ},
            {**TF_TRUE},
            {**DIAGRAM},
            {
                "type": "SWIPE_BOOLEAN",
                "statement": "Paging maps virtual pages onto physical frames.",
                "answer": True,
                "one_liner_rationale": "That is the core job of the page table.",
            },
            {
                "type": "SPOT_IMPOSTER",
                "statements": [
                    {"text": "A page fault triggers loading from disk."},
                    {"text": "TLB caches recent page translations."},
                    {"text": "Internal fragmentation affects fixed partitions.", "is_imposter": True},
                    {"text": "Thrashing collapses useful CPU work."},
                ],
                "one_liner_rationale": "Internal fragmentation belongs to fixed paging, not partitions.",
            },
            {
                "type": "TAP_TO_FILL",
                "sentence_with_blank": "The _____ keeps the most-used translations handy.",
                "correct_answer": "TLB",
                "distractors": ["RAM", "disk"],
                "one_liner_rationale": "The TLB is the translation cache.",
            },
            {
                "type": "BOSS_FIGHT",
                "boss_name": "Lord Pagefault",
                "questions": [
                    {
                        "question": "Which unit maps virtual to physical addresses?",
                        "options": [
                            {"text": "MMU", "correct": True},
                            {"text": "ALU", "correct": False},
                        ],
                    },
                    {
                        "question": "What does thrashing describe?",
                        "options": [
                            {"text": "Constant paging with no useful work", "correct": True},
                            {"text": "A CPU cache hit", "correct": False},
                        ],
                    },
                    {
                        "question": "Which policy evicts the least-recently-used page?",
                        "options": [
                            {"text": "LRU", "correct": True},
                            {"text": "FIFO", "correct": False},
                        ],
                    },
                    {
                        "question": "Where are page tables usually stored?",
                        "options": [
                            {"text": "Main memory", "correct": True},
                            {"text": "GPU registers", "correct": False},
                        ],
                    },
                ],
            },
        ],
    }
    assert LearningUnitBundle.model_validate(full).missing_challenge_types == []
    # Depth-scaled bundles are uncapped (well past the old 8-item limit).
    big = {**BUNDLE, "challenges": [*BUNDLE["challenges"], *BUNDLE["challenges"]]}
    assert LearningUnitBundle.model_validate(big)  # 16 items accepted


def test_bundle_still_rejects_structural_violations():
    # Unknown challenge types are always invalid.
    bogus = [{**BUNDLE["challenges"][0], "type": "QUIZ_ROYALE"}]
    with pytest.raises(Exception):
        LearningUnitBundle.model_validate({**BUNDLE, "challenges": bogus})
    # And below the size bound fails outright.
    with pytest.raises(Exception):
        LearningUnitBundle.model_validate({**BUNDLE, "challenges": BUNDLE["challenges"][:1]})


@pytest.mark.parametrize("difficulty_level", [0, 6])
def test_bundle_difficulty_level_is_bounded(difficulty_level: int):
    with pytest.raises(Exception):
        LearningUnitBundle.model_validate({**BUNDLE, "difficulty_level": difficulty_level})


def test_bundle_schema_is_ref_free_closed_and_discriminated():
    schema = build_bundle_schema()
    text = json.dumps(schema)

    assert "$ref" not in text and "$defs" not in text
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"concept_title", "difficulty_level", "challenges"}
    assert "unit_id" in schema["properties"] and "unit_id" not in schema["required"]
    assert schema["properties"]["difficulty_level"]["minimum"] == 1
    assert schema["properties"]["difficulty_level"]["maximum"] == 5
    for challenge_type in ALL_PAYLOADS:
        assert challenge_type in text


@pytest.mark.parametrize("challenge_type", sorted(ALL_PAYLOADS))
def test_single_challenge_schema_pins_the_discriminator(challenge_type: str):
    schema = build_schema(challenge_type)
    assert schema["properties"]["type"]["const"] == challenge_type
    assert schema["additionalProperties"] is False
    assert "$ref" not in json.dumps(schema)
    assert set(schema["required"]) == set(schema["properties"])


def test_json_schema_for_drops_pydantic_titles():
    assert "title" not in json_schema_for(LearningUnitBundle)


# ---------------------------------------------------------------------------
# 5. LLM decoding helpers
# ---------------------------------------------------------------------------


def test_extract_json_accepts_fenced_and_prose_wrapped_objects():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('Sure!\n{"a": 2}\nHope that helps') == {"a": 2}


def test_extract_json_rejects_prose_only():
    with pytest.raises(Exception):
        _extract_json("no json at all")


def test_fit_dim_pads_truncates_and_normalises():
    padded = _fit_dim([1.0, 2.0, 2.0], 5)
    assert len(padded) == 5 and padded[3] == 0.0
    assert sum(value * value for value in padded) == pytest.approx(1.0)
    assert len(_fit_dim(list(range(3000)), 1536)) == 1536


def test_estimate_difficulty_stays_within_bounds():
    for challenge_type, payload in ALL_PAYLOADS.items():
        assert 0.0 <= _estimate_difficulty(challenge_type, payload) <= 1.0


# ---------------------------------------------------------------------------
# 6. API contract models
# ---------------------------------------------------------------------------


def test_review_submission_accepts_uuid_strings():
    submission = ReviewSubmission.model_validate(
        {"user_id": str(uuid.uuid4()), "challenge_id": str(uuid.uuid4()), "rating": 3}
    )
    assert submission.rating == 3


def test_review_submission_accepts_warmup_context():
    """The Daily Warm-up arena tags its reviews with ``context: "warmup"``."""
    submission = ReviewSubmission.model_validate(
        {
            "user_id": str(uuid.uuid4()),
            "challenge_id": str(uuid.uuid4()),
            "rating": 3,
            "context": "warmup",
        }
    )
    assert submission.context == "warmup"
    # A lesson review omits the field entirely (it defaults to None).
    plain = ReviewSubmission.model_validate(
        {
            "user_id": str(uuid.uuid4()),
            "challenge_id": str(uuid.uuid4()),
            "rating": 3,
        }
    )
    assert plain.context is None


@pytest.mark.parametrize(
    "payload",
    [
        {"user_id": "not-a-uuid", "challenge_id": str(uuid.uuid4()), "rating": 3},
        {"user_id": str(uuid.uuid4()), "challenge_id": str(uuid.uuid4()), "rating": 7},
    ],
)
def test_review_submission_rejects_bad_input(payload: dict):
    with pytest.raises(Exception):
        ReviewSubmission.model_validate(payload)


# ---------------------------------------------------------------------------
# 7. Ingestion
# ---------------------------------------------------------------------------


def test_extract_pdf_content_emits_one_header_per_page(sample_lecture_text: str):
    assert sample_lecture_text.count("=== PAGE ") == 3
    assert "=== PAGE 1/3 |" in sample_lecture_text
    assert "chars=" in sample_lecture_text and "| heading=" in sample_lecture_text
    assert "boilerplate=" in sample_lecture_text


def test_extract_pdf_content_strips_running_header_and_detects_headings(sample_lecture_text: str):
    assert 'heading="Rule 6: Navigation and Safety"' in sample_lecture_text
    body = sample_lecture_text.split("===")[2]
    assert "MARITIME SAFETY 2026 - LECTURE 4" not in body


def test_extract_pdf_pages_meta_reports_text_per_page(sample_pdf_bytes: bytes):
    meta = extract_pdf_pages_meta(sample_pdf_bytes)
    assert len(meta) == 3
    assert all(entry["has_text"] for entry in meta)
    assert [entry["page_number"] for entry in meta] == [1, 2, 3]


def test_iterate_chunks_returns_clean_blueprints(sample_lecture_text: str):
    chunks = iterate_chunks(sample_lecture_text)
    assert chunks
    assert chunks[0].order_index == 0
    assert len(chunks[0].content) > 100
    assert "=== PAGE" not in chunks[0].content


def test_chunk_text_splits_long_prose_with_overlap(long_lecture_text: str):
    chunks = chunk_text(long_lecture_text, target_chars=1200, overlap_chars=200, min_chars=300)
    assert len(chunks) >= 4
    assert all(chunk.strip() for chunk in chunks)
    assert all(len(chunk) <= 1500 for chunk in chunks)
    overlapping = any(
        set(chunks[i].split()[-6:]) & set(chunks[i + 1].split()[:6])
        for i in range(len(chunks) - 1)
    )
    assert overlapping


def test_clean_text_repairs_hyphenation_and_ligatures():
    assert "manoeuvrability" in clean_text("manoeuvra-\nbility of the vessel")
    assert "fi" in clean_text("test \ufb01le")


def test_fix_kerning_spaces_recombines_letter_spaced_titles():
    """Kerned PDF titles ("C o u r s e") come back as whole words, while numeric
    data such as "1.2 3.4" is left byte-for-byte intact."""
    assert fix_kerning_spaces("C o u r s e") == "Course"
    assert fix_kerning_spaces("C o u r s e L e c t u r e") == "CourseLecture"
    assert fix_kerning_spaces("I n t r o d u c t i o n") == "Introduction"
    # Ordinary prose and numbers are never touched.
    assert fix_kerning_spaces("Operating Systems") == "Operating Systems"
    assert fix_kerning_spaces("More numbers 1.2 3.4") == "More numbers 1.2 3.4"
    assert fix_kerning_spaces("0.30 0.45 accuracy") == "0.30 0.45 accuracy"


def test_parse_pdf_returns_title_and_chunks(sample_pdf_bytes: bytes):
    parsed = parse_pdf(sample_pdf_bytes, "Lecture 4_Navigation Rules.pdf")
    assert "Navigation" in parsed.title
    assert parsed.chunk_count >= 1
    # Slide-aligned chunks carry a structural context header with slide range.
    assert parsed.chunk_count == 1  # tiny 3-slide deck stays one part
    chunk = parsed.chunks[0]
    assert chunk.content.startswith("--- [Lecture Part 1/1: Slides 1-3] ---")
    assert chunk.page_start == 1 and chunk.page_end == 3


def test_segment_slides_balances_long_decks():
    """80 dense slides -> 10-14 balanced semantic chunks, split at slide
    boundaries (the production token-accumulator spec). The ~1200-token slides
    here exceed the ceiling alone, so each chunk carries one slide plus slack."""
    from ingestion.parser import segment_slides

    pages = [
        (number, f"Slide heading {number}. " + "Body content for the slide. " * 40)
        for number in range(1, 81)
    ]
    chunks = segment_slides(pages)
    assert 10 <= len(chunks) <= 14
    assert chunks[0].content.startswith("--- [Lecture Part 1/")
    assert chunks[-1].content.startswith("--- [Lecture Part")
    # No page is lost or duplicated across parts.
    assert sum(c.page_end - c.page_start + 1 for c in chunks) == 80
    # Order is preserved, page ranges are contiguous, titles are readable.
    assert [c.order_index for c in chunks] == list(range(len(chunks)))
    assert all(
        chunks[i].page_end + 1 == chunks[i + 1].page_start for i in range(len(chunks) - 1)
    )
    assert all(not c.title.startswith("---") for c in chunks)


def test_segment_slides_mid_deck_gets_two_parts():
    """60 dense slides -> chunks inside the 600-1000 token band (never one
    giant blob; "Lost in the Middle" guard)."""
    from ingestion.parser import segment_slides

    pages = [
        (number, f"Concept {number}. " + "Detailed body text. " * 30)
        for number in range(1, 61)
    ]
    chunks = segment_slides(pages)
    assert len(chunks) >= 2
    assert all(500 <= c.token_count <= 1100 for c in chunks)
    assert (chunks[0].page_start, chunks[0].page_end) == (1, chunks[0].page_end)
    assert chunks[-1].page_end == 60
    assert sum(c.page_end - c.page_start + 1 for c in chunks) == 60


def test_segment_slides_small_deck_stays_whole():
    """A 20-slide deck under the 800-token total stays ONE chunk; a single
    slide is trivially one chunk."""
    from ingestion.parser import segment_slides

    # 20 slides x ~190 tokens would split; keep each slide under the cap so
    # the whole deck lands below the 800-token single-chunk threshold.
    lean = [
        (number, f"Topic {number}. " + "Brief lecture prose. " * 5)
        for number in range(1, 21)
    ]
    assert len(segment_slides(lean)) == 1
    tiny = [(1, "A single short slide with a complete sentence of body text.")]
    assert len(segment_slides(tiny)) == 1


def test_short_summary_deck_stays_one_part():
    """A 10-slide summary is ONE learning unit, so the content-adaptive target
    yields ~6-10 focused challenges instead of being split into redundant parts."""
    from ingestion.parser import segment_slides

    deck = [
        (number, f"Summary point {number}. " + "Short recap sentence. " * 6)
        for number in range(1, 11)
    ]
    assert sum(len(body) for _, body in deck) < 3500
    assert len(segment_slides(deck)) == 1


def test_token_accumulator_single_chunk_for_small_decks():
    """Spec: <= 6 slides compiles into 1 single chunk (Part 1), even when the
    slides are long enough to breach the 1000-token ceiling several times over."""
    from ingestion.parser import segment_slides_by_tokens

    long_slides = [
        (number, f"Deep topic {number}. " + "Dense academic body text. " * 60)
        for number in range(1, 7)
    ]
    assert sum(len(body) for _, body in long_slides) > 800 * 6
    chunks = segment_slides_by_tokens(long_slides)
    assert len(chunks) == 1
    assert chunks[0].page_start == 1 and chunks[0].page_end == 6
    assert chunks[0].content.startswith("--- [Lecture Part 1/1: Slides 1-6] ---")


def test_token_accumulator_keeps_sentences_whole():
    """The sliding buffer flushes at slide boundaries only, so a chunk's body
    always ends on a complete sentence (no mid-thought splits)."""
    from ingestion.parser import segment_slides_by_tokens

    pages = [
        (number, f"Concept {number}. The mechanism completes properly. " * 12)
        for number in range(1, 51)
    ]
    chunks = segment_slides_by_tokens(pages)
    assert len(chunks) >= 2
    for chunk in chunks:
        body = chunk.content.split("---", 2)[-1].strip()
        assert body.endswith("."), body[-40:]


def test_parse_pdf_rejects_non_pdf_input():
    with pytest.raises(Exception):
        parse_pdf(b"not a pdf at all", "broken.pdf")


def test_parse_pdf_titles_skip_tables_and_numeric_axes(fitz_module):
    """A deck whose slides open with TABLE headers / numeric axes still yields
    purely semantic part titles (no "TABLE I", no graph coordinates)."""
    import re

    document = fitz_module.open()
    for lines in (
        ["TABLE I", "0.30 0.45 0.60 0.90", "Benchmark accuracy for the model"],
        ["Figure 2", "Model BLEU score comparison", "Continued pre-training wins"],
    ):
        page = document.new_page()
        y = 60
        for line in lines:
            page.insert_text((72, y), line, fontsize=12)
            y += 20
    pdf_bytes = document.tobytes()
    document.close()

    parsed = parse_pdf(pdf_bytes, "Neural Machine Translation 2024-09-19.pdf")
    assert parsed.chunk_count >= 1
    for chunk in parsed.chunks:
        title = chunk.title
        assert not title.upper().startswith("TABLE"), title
        assert not title.upper().startswith("FIGURE"), title
        assert not re.search(r"\d+\.\d+\s+\d+\.\d+", title), title
    # The folder title avoids table/figure labels too.
    assert "TABLE" not in parsed.title.upper()


def test_filename_title_strips_timestamp_and_download_noise():
    from ingestion.parser import _title_from_filename

    assert _title_from_filename("Operating Systems 2024-09-19 (1).pdf") == (
        "Chapter: Operating Systems"
    )
    assert _title_from_filename("Networks_20240919_120000.pdf") == "Chapter: Networks"
    assert _title_from_filename("Distributed Systems v2.pdf") == "Chapter: Distributed Systems"


def test_folder_title_falls_back_to_filename_without_generic_placeholders():
    """Folder naming: clean slide title first, else the file name with a
    chapter marker — never "Lecture 1", "Node" or a truncated string."""
    from ingestion.parser import derive_lecture_title, _title_from_filename

    assert _title_from_filename("Operating Systems Ch3.pdf") == (
        "Chapter: Operating Systems Ch3"
    )
    # A filename that already carries a marker is not double-prefixed.
    assert _title_from_filename("Chapter 4 - Memory.pdf") == "Chapter 4 Memory"
    # Generic filenames degrade to the explicit fallback, not a placeholder.
    assert _title_from_filename("Lecture 1.pdf") == "Untitled Lecture"
    assert _title_from_filename("Node.pdf") == "Untitled Lecture"
    # From extract_pdf_content output: the first slide heading wins.
    page_text = (
        '=== PAGE 1/3 | chars=40 | heading="Operating Systems Scheduling" | boilerplate=0 ===\n'
        "Operating Systems Scheduling\nbody"
    )
    assert derive_lecture_title(page_text, "lecture 1.pdf") == (
        "Operating Systems Scheduling"
    )


def test_junk_titles_are_never_used_as_concepts():
    """Table headers, figure captions, raw numbers, graph axes and percentages
    must never become node titles — they are replaced with real topics."""
    from ingestion.parser import sanitize_concept_title, chunk_title

    # A real conceptual topic passes through untouched.
    assert sanitize_concept_title("Pre-training Methodology") == "Pre-training Methodology"
    assert sanitize_concept_title("Maritime Domain Adaptation") == "Maritime Domain Adaptation"

    # A bare table/figure label is replaced by a context-derived topic.
    assert (
        sanitize_concept_title("TABLE I", context="benchmark accuracy results")
        == "Empirical Evaluation"
    )
    assert (
        sanitize_concept_title("Figure 3", context="evaluation metrics on the dataset")
        == "Empirical Evaluation"
    )
    # A caption still yields the topic it describes.
    assert (
        sanitize_concept_title("Table 2: Accuracy on the benchmark")
        == "Accuracy on the benchmark"
    )
    # Graph-axis coordinates / raw percentages are junk, never titles.
    assert sanitize_concept_title("0.30 0.45 accuracy", context="accuracy results") == (
        "Empirical Evaluation"
    )
    assert sanitize_concept_title("92.5%", context="benchmark accuracy") == (
        "Empirical Evaluation"
    )

    # chunk_title derives a topic even when the section opens with a table.
    body = (
        "TABLE I: Accuracy on the benchmark\n"
        "0.30 0.45 0.60 0.90\n"
        "More numbers 1.2 3.4"
    )
    assert chunk_title(body, 0, "Neural Machine Translation") == "Accuracy on the benchmark"
    # No table, just a description -> the descriptive line is used.
    assert chunk_title("Evaluation & Results\nbody text", 0, "X") == "Evaluation & Results"


def test_part_titles_are_clean_concepts_without_noise():
    """Part titles read "Part N: Concept" with no raw filename, brackets or
    duplicate markers."""
    from database import node_title_with_number

    assert node_title_with_number(
        "Chapter 5: CPU Scheduling Algorithms", "Chapter 5: OS", 0
    ) == "Part 1: CPU Scheduling Algorithms"
    assert node_title_with_number(
        "Deadlock Prevention /", "Operating Systems Ch3", 1
    ) == "Part 2: Deadlock Prevention"
    assert node_title_with_number(
        "Operating Systems Ch3.pdf", "Operating Systems Ch3", 2
    ) == "Part 3: Operating Systems Ch3"


def test_node_titles_use_part_n_concept_format():
    """Predictable sub-topic naming: 'Part {i}: {Core Concept}' — never
    '3.1 X' or generic 'Node 1' placeholders."""
    from database import node_title_with_number

    assert (
        node_title_with_number("Paging Mechanics", "Chapter 3: Memory", 0)
        == "Part 1: Paging Mechanics"
    )
    assert (
        node_title_with_number("Virtual Memory", "Chapter 3: Memory", 1)
        == "Part 2: Virtual Memory"
    )


def test_node_titles_drop_generic_and_duplicated_headings():
    from database import node_title_with_number

    # A heading that is only a part marker gets replaced by the lecture title.
    assert node_title_with_number("Part 2", "OSI Model", 1) == "Part 2: OSI Model"
    # A duplicated lecture-title prefix is stripped, not doubled.
    assert (
        node_title_with_number("Chapter 1: Intro - Part 1", "Chapter 1: Intro", 0)
        == "Part 1: Intro"
    )
    # Empty heading falls back to the lecture title.
    assert node_title_with_number("", "Networks", 2) == "Part 3: Networks"


def test_challenge_target_grows_with_chunk_density():
    """Content-adaptive sizing: a short summary lands at 6-10 focused
    challenges, a detailed technical part at 16-24 in-depth ones — no rigid
    18/18 quota."""
    from llm.generator import _allocate_variants, _target_challenge_count

    assert _target_challenge_count(900) == 6       # tiny summary
    assert 6 <= _target_challenge_count(3000) <= 10  # 10-slide summary deck
    assert 16 <= _target_challenge_count(9000) <= 24  # dense technical deck
    # The 24 ceiling matches LearningUnitBundle.challenges max_length.
    assert _target_challenge_count(50_000) == 24

    # Targets are spread across the requested types and never exceed the target.
    assert sum(_allocate_variants(10, 6)) == 10
    assert sum(_allocate_variants(24, 6)) == 24
    assert _allocate_variants(6, 6) == [1, 1, 1, 1, 1, 1]
    assert _allocate_variants(7, 6) == [2, 1, 1, 1, 1, 1]


def test_cloze_blank_variants_are_normalized_to_canonical_placeholder():
    """____, [blank], (___), <blank> all become _____ BEFORE validation —
    no more 3-attempt repair loops on cloze challenges."""
    from schema import ContextualClozeChallenge, normalize_blank

    base = {"correct_answer": "speed", "distractors": ["depth", "mass"],
            "explanation": "safe speed rule"}
    for sentence in (
        "Proceed at a safe ____ at all times.",          # 4 underscores
        "Proceed at a safe __ at all times.",            # 2 underscores
        "Proceed at a safe [blank] at all times.",       # bracketed
        "Proceed at a safe (___) at all times.",         # bracketed underscores
        "Proceed at a safe <blank> at all times.",       # angle placeholder
        "Proceed at a safe {blank} at all times.",       # braces
        "Proceed at a safe ________ at all times.",      # long run
        "Proceed at a safe ... at all times.",           # ellipsis
        "Proceed at a safe … at all times.",             # unicode ellipsis
        "Proceed at a safe [...] at all times.",         # bracketed ellipsis
        "Proceed at a safe [???] at all times.",         # bracketed question marks
    ):
        assert normalize_blank(sentence).count("_____") == 1
        challenge = ContextualClozeChallenge(
            sentence_with_blank=sentence,
            instructions="أكمل الفراغ",
            **base,
        )
        assert "_____" in challenge.sentence_with_blank


def test_cloze_rejects_only_when_no_placeholder_exists():
    from pydantic import ValidationError

    from schema import ContextualClozeChallenge

    base = {"correct_answer": "speed", "distractors": ["depth", "mass"],
            "explanation": "safe speed rule"}
    with pytest.raises(ValidationError):
        ContextualClozeChallenge(
            sentence_with_blank="Proceed at a safe speed at all times.",
            instructions="أكمل الفراغ",
            **base,
        )


def test_tap_to_fill_blank_variants_normalize_without_any_retry():
    """TAP_TO_FILL shares the cloze normalisation path: every readable
    placeholder variant is rewritten to the canonical ``_____`` BEFORE
    validation, so ingestion never enters the 3x rejection loop."""
    from llm.generator import (
        CLOZE_LIKE_TYPES,
        _normalize_cloze_blank,
        _repair_cloze_blank,
    )
    from schema import validate_payload

    assert "TAP_TO_FILL" in CLOZE_LIKE_TYPES
    assert "CONTEXTUAL_CLOZE" in CLOZE_LIKE_TYPES

    def payload(sentence: str) -> dict:
        return {
            "type": "TAP_TO_FILL",
            "instructions": "اضغط على الكلمة الناقصة",
            "sentence_with_blank": sentence,
            "correct_answer": "recent",
            "distractors": ["stale", "future"],
            "one_liner_rationale": "recent translations stay cached",
        }

    for sentence in (
        "The TLB caches ____ translations.",       # 4 underscores
        "The TLB caches [blank] translations.",    # bracketed word
        "The TLB caches <blank> translations.",    # angle placeholder
        "The TLB caches (...) translations.",      # bracketed ellipsis
        "The TLB caches (blank) translations.",    # parenthesised word
        "The TLB caches (___) translations.",      # parenthesised underscores
        "The TLB caches … translations.",          # unicode ellipsis
    ):
        raw = payload(sentence)
        _normalize_cloze_blank(raw)
        assert raw["sentence_with_blank"].count("_____") == 1, sentence
        challenge = validate_payload("TAP_TO_FILL", raw)
        assert "_____" in challenge.sentence_with_blank

    # A sentence with NO blank at all is repaired from correct_answer instead of
    # being rejected three times.
    raw = payload("The TLB caches recent translations.")
    _normalize_cloze_blank(raw)
    _repair_cloze_blank(raw)
    assert raw["sentence_with_blank"].count("_____") == 1
    repaired = validate_payload("TAP_TO_FILL", raw)
    assert repaired.sentence_with_blank.count("_____") == 1
