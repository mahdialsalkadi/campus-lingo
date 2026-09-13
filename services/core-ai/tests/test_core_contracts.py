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
    InteractiveStoryChallenge,
    LearningUnitBundle,
    MatchMadnessChallenge,
    ReviewSubmission,
    SpotTheMistakeChallenge,
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

STORY = {
    "type": "INTERACTIVE_STORY",
    "instructions": "اختر القرار الصحيح",
    "scenario_title": "المناوبة الليلية على الجسر",
    "dialogue": [
        {"speaker": "الربان", "text": "الضباب يزداد كثافة أمامنا.",
         "action_prompt": "ما أول إجراء تتخذه؟",
         "options": [
             {"text": "تقليل السرعة وإطلاق إشارات الضباب", "correct": True,
              "feedback": "أحسنت، هذا متوافق مع القاعدة 19."},
             {"text": "الحفاظ على السرعة الكاملة", "correct": False,
              "feedback": "غير آمن في الرؤية المحدودة."},
         ]},
        {"speaker": "الربان", "text": "ظهر هدف على الرادار على مسافة ميلين.",
         "action_prompt": "ما إجراؤك؟",
         "options": [
             {"text": "رسم الهدف وتقييم خطر التصادم", "correct": True, "feedback": "ممتاز."},
             {"text": "افتراض أنه سيمر بسلام", "correct": False,
              "feedback": "لا يجوز الافتراض بمعلومات ناقصة."},
         ]},
    ],
}

ALL_PAYLOADS = {"MATCH_MADNESS": MATCH, "SPOT_MISTAKE": SPOT,
                "CONTEXTUAL_CLOZE": CLOZE, "INTERACTIVE_STORY": STORY}


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
def test_spot_mistake_requires_token_to_appear_exactly_once(text: str, token: str):
    with pytest.raises(Exception):
        validate_payload("SPOT_MISTAKE", {**SPOT, "code_or_text": text, "mistake_token": token})


def test_cloze_word_bank_is_answer_plus_distractors():
    payload = validate_payload("CONTEXTUAL_CLOZE", CLOZE)
    assert isinstance(payload, ContextualClozeChallenge)
    assert payload.options == ["speed", "depth", "heading", "mass"]


def test_cloze_rejects_answer_hidden_in_distractors_and_missing_blank():
    with pytest.raises(Exception):
        validate_payload("CONTEXTUAL_CLOZE", {**CLOZE, "distractors": ["speed", "depth", "mass"]})
    with pytest.raises(Exception):
        validate_payload("CONTEXTUAL_CLOZE", {**CLOZE, "sentence_with_blank": "no blank here"})


def test_story_dialogue_requires_exactly_one_correct_option():
    payload = validate_payload("INTERACTIVE_STORY", STORY)
    assert isinstance(payload, InteractiveStoryChallenge)
    assert len(payload.dialogue) == 2

    both_correct = {**STORY, "dialogue": [
        {**STORY["dialogue"][0],
         "options": [dict(STORY["dialogue"][0]["options"][0], correct=True),
                     dict(STORY["dialogue"][0]["options"][1], correct=True)]},
        STORY["dialogue"][1],
    ]}
    with pytest.raises(Exception):
        validate_payload("INTERACTIVE_STORY", both_correct)


def test_unknown_challenge_type_is_rejected():
    with pytest.raises(ValueError):
        validate_payload("CROSSWORD", MATCH)


# ---------------------------------------------------------------------------
# 4. LearningUnitBundle + the JSON Schema handed to Ollama
# ---------------------------------------------------------------------------

BUNDLE = {
    "unit_id": "unit-1",
    "concept_title": "قواعد المناورة الآمنة (Safe Speed)",
    "difficulty_level": 3,
    "challenges": [MATCH, SPOT, CLOZE, STORY],
}


def test_bundle_validates_and_keeps_type_order():
    unit = LearningUnitBundle.model_validate(BUNDLE)
    assert [challenge.type for challenge in unit.challenges] == [
        "MATCH_MADNESS", "SPOT_MISTAKE", "CONTEXTUAL_CLOZE", "INTERACTIVE_STORY"
    ]
    assert [kind for kind, _ in unit.challenge_payloads()] == [
        "MATCH_MADNESS", "SPOT_MISTAKE", "CONTEXTUAL_CLOZE", "INTERACTIVE_STORY"
    ]


def test_bundle_generates_a_unit_id_when_omitted():
    unit = LearningUnitBundle.model_validate({k: v for k, v in BUNDLE.items() if k != "unit_id"})
    assert unit.unit_id


def test_bundle_rejects_duplicate_challenge_types():
    with pytest.raises(Exception):
        LearningUnitBundle.model_validate({**BUNDLE, "challenges": [MATCH, MATCH]})


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


def test_parse_pdf_returns_title_and_chunks(sample_pdf_bytes: bytes):
    parsed = parse_pdf(sample_pdf_bytes, "Lecture 4_Navigation Rules.pdf")
    assert "Navigation" in parsed.title
    assert parsed.chunk_count >= 1


def test_parse_pdf_rejects_non_pdf_input():
    with pytest.raises(Exception):
        parse_pdf(b"not a pdf at all", "broken.pdf")
