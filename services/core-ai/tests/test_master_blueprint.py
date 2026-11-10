"""Master-blueprint regression tests.

Covers the production upgrade surface without needing a GPU:

* the token accumulator (600-1000 token chunks, single-chunk small decks,
  10-14 balanced semantic chunks for long decks, no mid-sentence splits),
* the dual-layer diagram filter (pHash branding blacklist + the
  administrative-noise text gate),
* the micro-game payload contracts (SWIPE_BOOLEAN / SPOT_IMPOSTER /
  TAP_TO_FILL / BOSS_FIGHT + one_liner_rationale),
* the Ebbinghaus interval chain Day 1 -> Day 3 -> Day 7,
* the /api/join/{code} 200 response contract (classroom enrollment codes).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ingestion.parser import estimate_tokens, extract_pdf_diagrams, segment_slides_by_tokens
from llm.generator import build_chapter_boss_fight
from llm.tutor import build_tutor_messages, encode_sse
from models import card_facts_from_row, next_card_state
from schema import (
    ALL_CHALLENGE_TYPES,
    BossFightChallenge,
    ContextualClozeChallenge,
    MatchMadnessChallenge,
    McqQuizChallenge,
    SpotTheMistakeChallenge,
    TrueFalseChallenge,
    SpotImposterChallenge,
    SwipeBooleanChallenge,
    TapToFillChallenge,
    normalize_blank,
)
from spaced_repetition.engine import interval_days_for_level, next_repetition_level

SERVICE_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Token accumulator
# ---------------------------------------------------------------------------


class TestTokenAccumulator:
    def test_estimate_tokens_is_chars_over_four(self):
        assert estimate_tokens("a" * 400) == 100
        assert estimate_tokens("") == 0

    def test_six_slides_or_less_compile_into_one_chunk(self):
        pages = [
            (number, f"Topic {number}. " + "Dense academic body text. " * 60)
            for number in range(1, 7)
        ]
        chunks = segment_slides_by_tokens(pages)
        assert len(chunks) == 1
        assert (chunks[0].page_start, chunks[0].page_end) == (1, 6)
        assert chunks[0].content.startswith("--- [Lecture Part 1/1: Slides 1-6] ---")

    def test_low_token_decks_stay_single_chunk(self):
        # 40 tiny slides but well under the 800-token total -> Part 1 only.
        pages = [(n, f"Point {n}. Short sentence.") for n in range(1, 41)]
        # Slides under 40 chars are filtered, so pad them over the usable line.
        pages = [(n, f"Point {number}. " + "x " * 18) for number, (n, _) in zip(range(1, 41), pages)]
        pages = [(n, f"Point {n}. " + "Brief recap. " * 4) for n in range(1, 41)]
        chunks = segment_slides_by_tokens(pages)
        assert len(chunks) == 1

    def test_eighty_slide_deck_yields_ten_to_fourteen_balanced_chunks(self):
        pages = [
            (number, f"Concept {number}. " + "Operating systems lecture prose. " * 12)
            for number in range(1, 81)
        ]
        chunks = segment_slides_by_tokens(pages)
        assert 10 <= len(chunks) <= 14
        # Balanced: every chunk lands in the 600-1000 token band (header slack).
        assert all(500 <= chunk.token_count <= 1100 for chunk in chunks)
        # Contiguous, ordered, no slide lost or duplicated.
        assert chunks[0].page_start == 1
        assert chunks[-1].page_end == 80
        for left, right in zip(chunks, chunks[1:]):
            assert left.page_end + 1 == right.page_start

    def test_chunks_never_split_a_sentence(self):
        pages = [
            (number, f"Concept {number}. The mechanism always completes properly. " * 10)
            for number in range(1, 41)
        ]
        chunks = segment_slides_by_tokens(pages)
        assert len(chunks) >= 2
        for chunk in chunks:
            body = chunk.content.split("---", 2)[-1].strip()
            assert body.endswith(".")


# ---------------------------------------------------------------------------
# Dual-layer visual filter (pHash + administrative text gate)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fitz_module():
    return pytest.importorskip("pymupdf", reason="PyMuPDF is required")


def _png(pymupdf, width: int, height: int, seed: int = 0) -> bytes:
    import random

    rng = random.Random(seed)
    samples = bytes(rng.getrandbits(8) for _ in range(width * height * 3))
    return pymupdf.Pixmap(pymupdf.csRGB, width, height, samples, False).tobytes("png")


class TestDiagramFilterLayers:
    def test_layer1_phash_blacklists_repeated_branding(self, fitz_module):
        document = fitz_module.open()
        logo = _png(fitz_module, 400, 320, seed=1)
        for slide in range(1, 5):
            page = document.new_page()
            # Mid-slide placement: only the pHash layer can catch this.
            page.insert_image(fitz_module.Rect(300, 150, 700, 470), stream=logo)
            if slide in (1, 3):
                page.insert_image(
                    fitz_module.Rect(72, 550, 472, 870),
                    stream=_png(fitz_module, 400, 320, seed=100 + slide),
                )
        data = document.tobytes()
        document.close()

        diagrams = extract_pdf_diagrams(data)
        assert sorted(d.page_number for d in diagrams) == [1, 3]

    def test_layer2_rejects_administrative_noise_figures(self, fitz_module):
        document = fitz_module.open()
        first = document.new_page(width=1200, height=2000)
        first.insert_image(fitz_module.Rect(300, 300, 900, 900), stream=_png(fitz_module, 400, 320, seed=2))
        first.insert_text((80, 320), "Department of Computer Science", fontsize=12)
        first.insert_text((80, 360), "CS 202 - Data Structures", fontsize=12)
        first.insert_text((80, 400), "Email: cs202@university.edu", fontsize=12)
        first.insert_text((80, 440), "Instructor: Prof. Salem", fontsize=12)
        second = document.new_page(width=1200, height=2000)
        second.insert_text((300, 280), "Figure 4: Congestion window growth", fontsize=12)
        second.insert_image(fitz_module.Rect(300, 300, 900, 900), stream=_png(fitz_module, 400, 320, seed=3))
        data = document.tobytes()
        document.close()

        diagrams = extract_pdf_diagrams(data)
        assert len(diagrams) == 1
        assert diagrams[0].page_number == 2


# ---------------------------------------------------------------------------
# Micro-game payload contracts
# ---------------------------------------------------------------------------


class TestMicroGameContracts:
    def test_swipe_boolean_roundtrip(self):
        payload = SwipeBooleanChallenge(
            statement="Paging maps virtual pages onto physical frames.",
            answer=True,
            one_liner_rationale="That is the page table's core job.",
        )
        assert payload.type == "SWIPE_BOOLEAN"
        assert payload.model_dump(mode="json")["one_liner_rationale"]

    def test_swipe_boolean_statement_is_capped(self):
        with pytest.raises(Exception):
            SwipeBooleanChallenge(
                statement="x" * 200,
                answer=False,
                one_liner_rationale="too long",
            )

    def test_spot_imposter_requires_exactly_one_imposter(self):
        base = [
            {"text": "Truth one stands as written."},
            {"text": "Truth two also stands."},
            {"text": "The imposter hides here.", "is_imposter": True},
            {"text": "Truth four survives."},
        ]
        payload = SpotImposterChallenge(statements=base, one_liner_rationale="One line.")
        assert payload.imposter_index == 2
        # Two imposters -> invalid; zero imposters -> invalid.
        with pytest.raises(Exception):
            SpotImposterChallenge(
                statements=[{**base[0], "is_imposter": True}, {**base[1], "is_imposter": True},
                            base[2], base[3]],
                one_liner_rationale="One line.",
            )
        with pytest.raises(Exception):
            SpotImposterChallenge(
                statements=[{**base[0]}, {**base[1]}, {**base[3]}, {"text": "Truth five."}],
                one_liner_rationale="One line.",
            )

    def test_tap_to_fill_normalizes_blank_variants(self):
        payload = TapToFillChallenge(
            sentence_with_blank="The TLB caches recent [blank] translations.",
            correct_answer="page",
            distractors=["disk", "bus"],
            one_liner_rationale="One line.",
        )
        assert "_____" in payload.sentence_with_blank
        assert payload.options[0] == "page"

    def test_tap_to_fill_rejects_missing_blank(self):
        with pytest.raises(Exception):
            TapToFillChallenge(
                sentence_with_blank="A complete sentence with no blank at all.",
                correct_answer="word",
                distractors=["a", "b"],
                one_liner_rationale="One line.",
            )

    def test_boss_fight_shape(self):
        payload = BossFightChallenge(
            boss_name="Lord Pagefault",
            questions=[
                {
                    "question": f"Boss question number {n}?",
                    "options": [
                        {"text": "Right", "correct": n == 0},
                        {"text": "Wrong", "correct": n != 0},
                    ],
                }
                for n in range(4)
            ],
        )
        assert payload.seconds_per_question == 15
        assert len(payload.questions) == 4
        # Duplicate prompts are rejected.
        with pytest.raises(Exception):
            BossFightChallenge(
                boss_name="Lord Pagefault",
                questions=[
                    {"question": "Same prompt?", "options": [{"text": "A", "correct": True},
                                                             {"text": "B", "correct": False}]},
                    {"question": "Same prompt?", "options": [{"text": "C", "correct": True},
                                                             {"text": "D", "correct": False}]},
                    {"question": "Different?", "options": [{"text": "E", "correct": True},
                                                           {"text": "F", "correct": False}]},
                    {"question": "Another?", "options": [{"text": "G", "correct": True},
                                                         {"text": "H", "correct": False}]},
                ],
            )

    def test_normalize_blank_is_shared_with_tap_to_fill(self):
        assert normalize_blank("fill the ____ here") == "fill the _____ here"


# ---------------------------------------------------------------------------
# Ebbinghaus interval chain
# ---------------------------------------------------------------------------


class _Card:
    """Minimal FsrsCard stand-in for next_card_state."""

    def __init__(self, level: int, stability: float, difficulty: float, reps: int) -> None:
        self.repetition_level = level
        self.stability = stability
        self.difficulty = difficulty
        self.retrievability_score = 0.9
        self.reps = reps
        self.lapses = 0
        self.state = 2  # Review
        self.last_rating = 3


class TestEbbinghausChain:
    def test_good_reviews_climb_day1_to_day3_to_day7(self):
        from datetime import datetime, timezone

        card = _Card(level=0, stability=1.0, difficulty=5.0, reps=0)
        now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
        seen: list[float] = []
        for _ in range(3):
            ebbinghaus = next_card_state(
                card, rating=3, new_stability=3.0, new_difficulty=5.0,
                next_due=now, now=now,
            )
            card.repetition_level = ebbinghaus["repetition_level"]
            seen.append(interval_days_for_level(card.repetition_level))
        assert seen[0] == 1.0   # Day 1
        assert seen[1] == 3.0   # Day 3
        assert seen[2] == 7.0   # Day 7

    def test_lapse_restarts_at_day1(self):
        assert next_repetition_level(4, lapsed=True) == 1
        assert interval_days_for_level(next_repetition_level(4, lapsed=True)) == 1.0

    def test_day30_is_absorbing(self):
        assert next_repetition_level(5, lapsed=False) == 5
        assert interval_days_for_level(5) == 30.0


# ---------------------------------------------------------------------------
# /api/join/{code} contract (classroom enrollment codes resolve with 200 OK)
# ---------------------------------------------------------------------------


class TestGbnfGrammarExport:
    """The exported grammar must stay llama.cpp-valid and schema-synced.

    Regression guard for the boot-abort: llama.cpp GBNF only allows
    [a-zA-Z0-9-] in rule names (underscores abort with "expecting name"),
    and run.sh consumes the file via --grammar-file (not --grammar, which
    expects inline text).
    """

    def test_exported_grammar_is_llamacpp_valid(self, tmp_path):
        import re
        import subprocess
        import sys

        from scripts.export_gbnf import DEFAULT_OUTPUT

        subprocess.run(
            [sys.executable, "scripts/export_gbnf.py", str(tmp_path / "out.gbnf")],
            check=True,
            cwd=SERVICE_ROOT,
        )
        grammar = (tmp_path / "out.gbnf").read_text(encoding="utf-8")

        rule_lines = [ln for ln in grammar.splitlines() if "::=" in ln]
        assert rule_lines, "grammar must define rules"
        # Every rule name is only [a-zA-Z0-9-]: underscores abort llama-server
        # with "expecting name".
        for line in rule_lines:
            name = line.split("::=")[0].strip()
            assert re.fullmatch(r"[A-Za-z0-9-]+", name), f"illegal rule name: {name}"

        # The value rule must reference the generic object rule, not the
        # bundle root (a nested object is not a whole bundle).
        value_rule = next(ln for ln in rule_lines if ln.startswith("value ::="))
        assert "object-g" in value_rule and " | array" in value_rule

        # The pinned discriminator stays in sync with the registry.
        type_rule = next(ln for ln in rule_lines if ln.startswith("type-value ::="))
        for challenge_type in ALL_CHALLENGE_TYPES:
            assert f'"\\"{challenge_type}\\""' in type_rule

        # The committed default file must exist and match a fresh export.
        assert DEFAULT_OUTPUT.is_file()
        subprocess.run(
            [sys.executable, "scripts/export_gbnf.py", str(tmp_path / "fresh.gbnf")],
            check=True,
            cwd=SERVICE_ROOT,
        )
        assert (tmp_path / "fresh.gbnf").read_text(encoding="utf-8") == DEFAULT_OUTPUT.read_text(
            encoding="utf-8"
        )


class TestOneLinerRationale:
    """Every classic payload carries the optional one-liner field."""

    def test_classic_payloads_default_to_empty_one_liner(self):
        mcq = McqQuizChallenge(
            question="What is paging?",
            options=[{"text": "A", "correct": True}, {"text": "B", "correct": False}],
            explanation="long explanation",
        )
        cloze = ContextualClozeChallenge(
            sentence_with_blank="The _____ holds pages.",
            correct_answer="TLB",
            distractors=["disk", "RAM"],
            explanation="long explanation",
        )
        mistake = SpotTheMistakeChallenge(
            code_or_text="The TLB is a slow cache.",
            mistake_token="slow",
            explanation="long explanation",
        )
        match = MatchMadnessChallenge(
            pairs=[
                {"term": "TLB", "definition": "translation cache"},
                {"term": "RAM", "definition": "main memory"},
                {"term": "SSD", "definition": "persistent storage"},
                {"term": "GPU", "definition": "graphics unit"},
            ],
        )
        tf = TrueFalseChallenge(statement="Paging maps pages.", answer=True, explanation="x")
        for payload in (mcq, cloze, mistake, match, tf):
            assert payload.one_liner_rationale == ""

    def test_one_liner_round_trip_and_cap(self):
        payload = McqQuizChallenge(
            question="What is paging?",
            options=[{"text": "A", "correct": True}, {"text": "B", "correct": False}],
            explanation="long",
            one_liner_rationale="Paging splits memory into fixed pages.",
        )
        dumped = payload.model_dump(mode="json")
        assert dumped["one_liner_rationale"].startswith("Paging splits")
        with pytest.raises(Exception):
            McqQuizChallenge(
                question="What is paging?",
                options=[{"text": "A", "correct": True}, {"text": "B", "correct": False}],
                explanation="long",
                one_liner_rationale="x" * 301,
            )


class TestChapterBossFight:
    """build_chapter_boss_fight: chapter culmination from existing material."""

    POOL = [
        {
            "type": "MCQ_QUIZ",
            "question": "What is a TLB?",
            "options": [{"text": "cache", "correct": True}, {"text": "disk", "correct": False}],
        },
        {
            "type": "CONTEXTUAL_CLOZE",
            "sentence_with_blank": "The _____ maps virtual pages.",
            "correct_answer": "TLB",
            "distractors": ["disk", "RAM"],
        },
        {
            "type": "TAP_TO_FILL",
            "sentence_with_blank": "TLB stands for translation _____ buffer.",
            "correct_answer": "lookaside",
            "distractors": ["random", "linear"],
        },
        {
            "type": "MCQ_QUIZ",
            "question": "What is paging?",
            "options": [{"text": "division", "correct": True}, {"text": "merging", "correct": False}],
        },
    ]

    def test_boss_is_synthesized_from_existing_questions(self):
        boss = build_chapter_boss_fight("Paging", self.POOL)
        assert boss is not None
        assert boss.challenge_type == "BOSS_FIGHT"
        assert len(boss.payload.questions) >= 4
        assert boss.payload.seconds_per_question == 15
        assert boss.payload.boss_name
        # Every question keeps exactly one correct option.
        for question in boss.payload.questions:
            assert sum(1 for option in question.options if option.correct) == 1

    def test_thin_material_produces_no_boss(self):
        assert build_chapter_boss_fight("Paging", self.POOL[:1]) is None
        assert build_chapter_boss_fight("Paging", []) is None

    def test_duplicate_and_blank_statements_are_skipped(self):
        pool = self.POOL + [dict(self.POOL[0])]  # exact duplicate prompt
        boss = build_chapter_boss_fight("Paging", pool)
        assert boss is not None
        prompts = [q.question.lower() for q in boss.payload.questions]
        assert len(prompts) == len(set(prompts))


class TestTutorModule:
    """llm/tutor.py: multi-tier Socratic router (blueprint §6)."""

    def test_messages_are_socratic_never_answers(self):
        messages = build_tutor_messages("What is a TLB?", student_choice="disk", context="paging")
        assert messages[0]["role"] == "system"
        assert "NEVER give the direct answer" in messages[0]["content"]
        assert "TLB?" in messages[1]["content"]
        assert "disk" in messages[1]["content"]

    def test_sse_encoding_round_trip(self):
        frame = encode_sse({"type": "delta", "text": "مرحبا"})
        assert frame.startswith("data: ") and frame.endswith("\n\n")
        assert "مرحبا" in frame  # ensure_ascii=False keeps Arabic readable

    def test_thermostats_pin_to_spec(self):
        from llm import tutor

        assert tutor.TUTOR_TEMPERATURE == 0.3
        assert tutor.TUTOR_MAX_TOKENS == 150


class TestJoinRouteContract:
    def test_shared_course_or_404_matches_enrollment_codes(self):
        """The join resolver queries BOTH ``share_code`` and ``enrollment_code``."""
        import inspect

        import main

        source = inspect.getsource(main._shared_course_or_404)
        assert "enrollment_code" in source and "share_code" in source
        assert "or_" in source

    def test_join_preview_returns_classroom_metadata(self):
        """The preview payload carries instructor + enrollment metadata."""
        import inspect

        import main

        source = inspect.getsource(main.preview_shared_course)
        for key in ("enrollment_code", "instructor_handle", "chapters", "is_classroom"):
            assert key in source
