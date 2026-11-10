"""Exam Crunch engine + DIAGRAM_QA synthesis (pure, no database, no network).

The mock-exam paper decides a student's diagnostic, and the crunch sheet has to
work even when every model tier is down — both are exercised directly here.
"""

from __future__ import annotations

from llm.crunch import (
    EXAM_TARGET_MAX,
    EXAM_TARGET_MIN,
    assemble_mock_exam,
    exam_target,
    fallback_cheatsheet,
    grade_exam,
    letter_grade,
    public_exam_questions,
    top_struggling_concepts,
)
from llm.diagrams import fallback_diagram_challenge
from schema import DiagramQaChallenge


def _mcq(challenge_id: str, chapter: str, correct_index: int = 0) -> dict:
    options = [{"text": f"option-{index}", "correct": index == correct_index} for index in range(4)]
    return {
        "challenge_id": challenge_id,
        "type": "MCQ_QUIZ",
        "payload_json": {
            "question": f"Question for {chapter} ({challenge_id})",
            "options": options,
            "explanation": f"Because of {chapter}.",
        },
        "node_id": f"node-{chapter}",
        "node_title": f"Concept {chapter}",
        "chapter": chapter,
    }


def _cloze(challenge_id: str, chapter: str) -> dict:
    return {
        "challenge_id": challenge_id,
        "type": "CONTEXTUAL_CLOZE",
        "payload_json": {
            "sentence_with_blank": "Rule 6 requires a safe _____ at all times.",
            "correct_answer": "speed",
            "distractors": ["depth", "heading"],
            "explanation": "Safe speed.",
        },
        "node_id": f"node-{chapter}",
        "node_title": f"Concept {chapter}",
        "chapter": chapter,
    }


# ---------------------------------------------------------------------------
# sizing
# ---------------------------------------------------------------------------


def test_exam_target_scales_with_the_material():
    assert exam_target(0) == 0
    assert exam_target(5) == 5                      # smaller than the minimum
    assert exam_target(EXAM_TARGET_MIN) == EXAM_TARGET_MIN
    assert exam_target(10_000) == EXAM_TARGET_MAX
    # Monotonic growth toward the max.
    sizes = [exam_target(n) for n in (25, 50, 90, 130, 200)]
    assert sizes == sorted(sizes)
    assert all(EXAM_TARGET_MIN <= size <= EXAM_TARGET_MAX for size in sizes[1:])


def test_letter_grade_bands():
    assert letter_grade(0.95) == "A"
    assert letter_grade(0.90) == "A"
    assert letter_grade(0.80) == "B"
    assert letter_grade(0.70) == "C"
    assert letter_grade(0.60) == "D"
    assert letter_grade(0.59) == "F"


# ---------------------------------------------------------------------------
# paper assembly
# ---------------------------------------------------------------------------


def test_assemble_spreads_questions_across_chapters_and_skips_ungradable():
    candidates = (
        [_mcq(f"a{i}", "Chapter A") for i in range(6)]
        + [_mcq(f"b{i}", "Chapter B") for i in range(3)]
        + [_cloze("c0", "Chapter C")]
        + [
            # Non-gradable types must be dropped, not crash the assembly.
            {
                "challenge_id": "match0",
                "type": "MATCH_MADNESS",
                "payload_json": {"pairs": [{"term": "x", "definition": "y"}]},
                "node_id": "node-C",
                "node_title": "Concept C",
                "chapter": "Chapter C",
            },
            {
                "challenge_id": "story0",
                "type": "INTERACTIVE_STORY",
                "payload_json": {"dialogue": []},
                "node_id": "node-C",
                "node_title": "Concept C",
                "chapter": "Chapter C",
            },
        ]
    )
    exam = assemble_mock_exam(candidates, target=6)
    assert len(exam) == 6
    chapters = [question["chapter"] for question in exam]
    # Round-robin: every chapter is drawn from before any repeats, so chapter C
    # (a single gradable question) is never starved by chapter A's larger pool.
    assert len(set(chapters[:3])) == 3
    assert set(chapters[:3]) == {"Chapter A", "Chapter B", "Chapter C"}
    assert chapters.count("Chapter C") == 1
    assert chapters.count("Chapter A") == 3  # only chapter A has spares left
    assert all(question["options"] for question in exam)
    assert all(isinstance(question["answer_index"], int) for question in exam)


def test_assemble_returns_every_candidate_when_the_pool_is_small():
    exam = assemble_mock_exam([_mcq("only", "Solo")], target=None)
    assert len(exam) == 1
    assert exam_target(1) == 1


def test_assemble_deduplicates_repeated_challenge_ids():
    duplicate = _mcq("same", "Chapter A")
    exam = assemble_mock_exam([duplicate, duplicate], target=5)
    assert len(exam) == 1


def test_public_questions_never_leak_the_answer_key():
    exam = assemble_mock_exam([_mcq("a", "Chapter A")], target=1)
    public = public_exam_questions(exam)
    assert len(public) == 1
    payload = public[0]
    assert set(payload) == {"index", "challenge_id", "chapter", "node_title", "type", "prompt", "options"}
    assert "answer_index" not in payload
    assert isinstance(payload["options"], list)


# ---------------------------------------------------------------------------
# grading
# ---------------------------------------------------------------------------


def _exam_of_five() -> list[dict]:
    return assemble_mock_exam(
        [_mcq(f"q{i}", "Chapter A" if i < 3 else "Chapter B") for i in range(5)],
        target=5,
    )


def test_perfect_exam_gets_an_a_and_no_weak_spots():
    exam = _exam_of_five()
    answers = {question["challenge_id"]: question["answer_index"] for question in exam}
    result = grade_exam(exam, answers)
    assert result["score"] == 5 and result["total"] == 5
    assert result["grade"] == "A"
    assert result["weak_spots"] == []
    assert all(chapter["risk"] == "low" for chapter in result["chapters"])
    assert 0.0 <= result["readiness"] <= 1.0


def test_failed_exam_reports_heatmap_and_drillable_weak_spots():
    exam = _exam_of_five()
    # Answer everything wrong (shift by one; the payloads have 4 options).
    answers = {
        question["challenge_id"]: (question["answer_index"] + 1) % 4 for question in exam
    }
    result = grade_exam(exam, answers)
    assert result["score"] == 0
    assert result["grade"] == "F"
    assert len(result["weak_spots"]) == 5
    spot = result["weak_spots"][0]
    assert spot["challenge_id"] and spot["chapter"]
    assert spot["correct_index"] != spot["selected_index"]
    assert all(chapter["risk"] == "high" for chapter in result["chapters"])
    # Weakest chapter first so the heatmap reads top-down.
    assert result["chapters"] == sorted(result["chapters"], key=lambda row: row["accuracy"])


def test_unanswered_questions_count_as_wrong_but_do_not_crash():
    exam = _exam_of_five()
    result = grade_exam(exam, {})
    assert result["score"] == 0
    assert all(spot["selected_index"] is None for spot in result["weak_spots"])

    # A garbage answer value (string, out of range) is treated as unanswered.
    result = grade_exam(exam, {exam[0]["challenge_id"]: "not-an-index"})
    assert result["score"] == 0


def test_readiness_discounts_a_lucky_short_paper():
    exam = _exam_of_five()
    perfect_short = grade_exam(
        exam, {question["challenge_id"]: question["answer_index"] for question in exam}
    )
    long_paper = assemble_mock_exam(
        [_mcq(f"q{i}", "Chapter A") for i in range(EXAM_TARGET_MIN)], target=EXAM_TARGET_MIN
    )
    perfect_long = grade_exam(
        long_paper, {question["challenge_id"]: question["answer_index"] for question in long_paper}
    )
    assert perfect_short["readiness"] <= perfect_long["readiness"] <= 1.0


def test_grading_an_empty_exam_is_safe():
    result = grade_exam([], {})
    assert result == {
        "score": 0,
        "total": 0,
        "percent": 0.0,
        "grade": "F",
        "readiness": 0.0,
        "chapters": [],
        "weak_spots": [],
        "review": [],
    }


# ---------------------------------------------------------------------------
# cheatsheet fallback + instructor insight ranking
# ---------------------------------------------------------------------------


def test_fallback_cheatsheet_has_every_required_section():
    markdown = fallback_cheatsheet(
        [
            "Rule 6: safe speed = 2.5 knots in fog.\nLook-out is continuous.",
            "Risk of collision: every vessel shall determine risk.",
        ],
        "Maritime Safety",
    )
    for heading in ("# Exam Crunch", "## Key Formulas", "## Core Definitions",
                    "## Common Exam Traps", "## 60-Second Recap"):
        assert heading in markdown
    assert "Maritime Safety" in markdown


def test_fallback_cheatsheet_handles_empty_input():
    markdown = fallback_cheatsheet([], "Empty Course")
    assert "## Core Definitions" in markdown
    assert "no definitions detected" in markdown


def test_top_struggling_concepts_ranks_by_failures():
    ranked = top_struggling_concepts(
        [
            {"node_title": "Paging", "correct": False},
            {"node_title": "Paging", "correct": False},
            {"node_title": "Paging", "correct": True},
            {"node_title": "Deadlock", "correct": False},
            {"node_title": "Scheduling", "correct": True},
        ],
        limit=2,
    )
    assert [row["concept"] for row in ranked] == ["Paging", "Deadlock"]
    assert ranked[0]["failures"] == 2


# ---------------------------------------------------------------------------
# DIAGRAM_QA fallback payload
# ---------------------------------------------------------------------------


def test_fallback_diagram_challenge_is_a_valid_payload():
    payload = fallback_diagram_challenge(
        image_url="/static/uploads/diagrams/c1/page-3-0.png",
        diagram_ref="page-3-0",
        caption="Figure 3: TCP three-way handshake",
        context="The client sends SYN, the server replies SYN-ACK.",
        distractors=["UDP datagram layout", "TLS certificate chain"],
    )
    validated = DiagramQaChallenge.model_validate(payload)
    assert validated.image_url.endswith("page-3-0.png")
    assert validated.diagram_ref == "page-3-0"
    options = validated.questions[0].options
    assert sum(1 for option in options if option.correct) == 1
    assert len({option.text for option in options}) == len(options)
    # The caption becomes the answer when there is no better signal.
    assert "handshake" in validated.correct_answer.lower()


def test_fallback_diagram_pads_options_when_no_distractors_exist():
    payload = fallback_diagram_challenge(
        image_url="/static/x.png",
        diagram_ref="page-1-0",
        caption="",
        context="Some chapter text about paging.",
        distractors=[],
    )
    validated = DiagramQaChallenge.model_validate(payload)
    assert len(validated.questions[0].options) >= 3
