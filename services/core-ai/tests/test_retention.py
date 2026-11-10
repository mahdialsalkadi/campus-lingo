"""Retention gating + slide-diagram extraction (pure, no database, no network).

The retention loop must never spam: the streak-saver only fires after the local
8 PM cutoff, the decay alert only when enough cards have actually gone fragile,
and every delivered alert is recorded as a durable in-app notification first.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ingestion.parser import extract_pdf_diagrams, save_diagrams
from retention import (
    FSRS_DECAY_MIN_CARDS,
    STREAK_SAVER,
    STREAK_SAVER_HOUR,
    fading_card_count,
    streak_saver_due,
)


class _Card:
    """Minimal stand-in for ``database.FsrsCard`` (no ORM needed here)."""

    def __init__(self, stability: float, due_date: datetime) -> None:
        self.stability = stability
        self.due_date = due_date


# ---------------------------------------------------------------------------
# streak saver
# ---------------------------------------------------------------------------


def test_streak_saver_waits_for_the_evening_cutoff():
    morning = datetime(2026, 9, 19, STREAK_SAVER_HOUR - 1, tzinfo=timezone.utc)
    assert streak_saver_due(morning, None) is False

    evening = datetime(2026, 9, 19, STREAK_SAVER_HOUR, tzinfo=timezone.utc)
    assert streak_saver_due(evening, None) is True


def test_streak_saver_skips_students_who_already_studied_today():
    now = datetime(2026, 9, 19, STREAK_SAVER_HOUR + 2, tzinfo=timezone.utc)
    studied_today = datetime(2026, 9, 19, 9, 30, tzinfo=timezone.utc)
    studied_yesterday = datetime(2026, 9, 18, 22, 0, tzinfo=timezone.utc)

    assert streak_saver_due(now, studied_today) is False
    assert streak_saver_due(now, studied_yesterday) is True


def test_streak_saver_treats_naive_timestamps_as_utc():
    now = datetime(2026, 9, 19, STREAK_SAVER_HOUR + 1, tzinfo=timezone.utc)
    naive_yesterday = datetime(2026, 9, 18, 23, 0)
    assert streak_saver_due(now, naive_yesterday) is True


def test_streak_saver_hour_is_configurable():
    early = datetime(2026, 9, 19, 6, 0, tzinfo=timezone.utc)
    assert streak_saver_due(early, None, hour=5) is True


def test_decay_min_cards_threshold_is_sane():
    assert FSRS_DECAY_MIN_CARDS >= 1


# ---------------------------------------------------------------------------
# FSRS decay
# ---------------------------------------------------------------------------


def test_fading_cards_are_due_and_fragile():
    now = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    cards = [
        _Card(0.5, now - timedelta(hours=1)),   # due + fragile  -> counted
        _Card(0.5, now + timedelta(days=3)),    # not due         -> skipped
        _Card(40.0, now - timedelta(hours=2)),  # due but stable  -> skipped
        _Card(1.0, now),                        # due + fragile   -> counted
    ]
    assert fading_card_count(cards, now=now) == 2


def test_decay_counts_naive_due_dates():
    now = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    naive = datetime(2026, 9, 19, 11, 0)
    assert fading_card_count([_Card(0.2, naive)], now=now) == 1


def test_no_fading_cards_on_a_healthy_deck():
    now = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    assert fading_card_count([], now=now) == 0
    assert fading_card_count([_Card(30.0, now + timedelta(days=10))], now=now) == 0


def test_streak_saver_kind_is_stable():
    # The client filters the inbox on this string, so it must not drift.
    assert STREAK_SAVER == "streak_saver"


# ---------------------------------------------------------------------------
# diagram extraction
# ---------------------------------------------------------------------------


def _png(pymupdf, width: int, height: int) -> bytes:
    """A large noisy PNG of the requested size (drawn, not a stub).

    Random samples keep the compressed payload above the 20KB file-size floor,
    mirroring a real slide figure instead of a flat, tiny glyph.
    """
    import random

    samples = bytes(random.getrandbits(8) for _ in range(width * height * 3))
    return pymupdf.Pixmap(
        pymupdf.csRGB, width, height, samples, False
    ).tobytes("png")


@pytest.fixture(scope="module")
def diagram_pdf(fitz_module):
    """Four slides: one real figure and three decorative graphics.

    The decorative junk covers each filter: a tiny icon (dimension floor), a
    wide header strip (aspect ratio) and a header logo placed inside the top
    10% band (position).
    """
    document = fitz_module.open()
    # 1) A genuine mid-slide figure -> kept.
    first = document.new_page()
    first.insert_text((72, 60), "Figure 3: TCP handshake", fontsize=12)
    first.insert_image(fitz_module.Rect(72, 200, 472, 520), stream=_png(fitz_module, 400, 320))
    # 2) Tiny decorative icon -> rejected on the dimension floor.
    second = document.new_page()
    second.insert_text((72, 60), "Appendix", fontsize=12)
    second.insert_image(fitz_module.Rect(72, 100, 88, 116), stream=_png(fitz_module, 16, 16))
    # 3) Wide header strip 1200x300 (aspect 4.0) -> rejected on aspect ratio.
    third = document.new_page()
    third.insert_image(fitz_module.Rect(72, 200, 672, 350), stream=_png(fitz_module, 1200, 300))
    # 4) Header logo 400x300 inside the top 10% band of a tall page -> rejected.
    fourth = document.new_page(width=1200, height=3000)
    fourth.insert_image(fitz_module.Rect(100, 20, 500, 290), stream=_png(fitz_module, 400, 300))
    data = document.tobytes()
    document.close()
    return data


def test_extraction_keeps_real_figures_and_drops_decorative_icons(diagram_pdf):
    diagrams = extract_pdf_diagrams(diagram_pdf)
    assert len(diagrams) == 1
    diagram = diagrams[0]
    assert diagram.page_number == 1
    assert diagram.ref == "page-1-0"
    assert diagram.width == 400 and diagram.height == 320
    assert diagram.pixel_count == 128_000
    assert diagram.data[:8] == b"\x89PNG\r\n\x1a\n"
    # The wide strip and header logo never make it through.
    assert all(d.page_number == 1 for d in diagrams)


def test_extraction_is_defensive_on_broken_input():
    assert extract_pdf_diagrams(b"") == []
    assert extract_pdf_diagrams(b"definitely not a pdf") == []


def test_extraction_respects_the_per_deck_cap(diagram_pdf):
    assert extract_pdf_diagrams(diagram_pdf, max_diagrams=0) == []


def test_saved_diagrams_expose_a_browser_url(diagram_pdf, tmp_path):
    diagrams = extract_pdf_diagrams(diagram_pdf)
    written = save_diagrams(
        diagrams, str(tmp_path / "course-1"), url_prefix="/static/uploads/diagrams/course-1"
    )
    assert len(written) == 1
    asset = written[0]
    assert asset["url"] == "/static/uploads/diagrams/course-1/page-1-0.png"
    assert asset["page"] == 1


# ---------------------------------------------------------------------------
# Dual-layer visual filter: Layer 1 (pHash) + Layer 2 (rect text / OCR gate)
# ---------------------------------------------------------------------------


imagehash = pytest.importorskip("imagehash", reason="imagehash is required for the pHash layer")


def _unique_png(pymupdf, width: int, height: int, seed: int) -> bytes:
    """A large noisy PNG whose pixels depend on ``seed`` (visually distinct)."""
    import random

    rng = random.Random(seed)
    samples = bytes(rng.getrandbits(8) for _ in range(width * height * 3))
    return pymupdf.Pixmap(
        pymupdf.csRGB, width, height, samples, False
    ).tobytes("png")


def test_repeated_logo_across_slides_is_blacklisted_but_unique_diagrams_pass(fitz_module):
    """Layer 1: a figure repeated on >= 2 distinct slides is branding -> dropped;
    every visually unique figure passes."""
    document = fitz_module.open()
    logo = _png(fitz_module, 400, 320)  # one shared bitmap on every slide
    for slide in range(1, 5):
        page = document.new_page()
        page.insert_image(
            fitz_module.Rect(300, 150, 700, 470), stream=logo
        )  # mid-slide placement: not caught by the position filter
        if slide in (1, 3):
            # Unique figures interleaved: the real diagrams that must survive.
            page.insert_image(
                fitz_module.Rect(72, 550, 472, 870),
                stream=_unique_png(fitz_module, 400, 320, seed=slide),
            )
    data = document.tobytes()
    document.close()

    diagrams = extract_pdf_diagrams(data)
    pages = sorted(d.page_number for d in diagrams)
    assert pages == [1, 3], f"expected only the unique figures, got pages {pages}"


def test_administrative_text_block_rejects_branded_figure(fitz_module):
    """Layer 2: a large mid-slide figure flanked by a pure letterhead block
    (department + email + course code) is rejected; a captioned diagram passes."""
    document = fitz_module.open()
    # Slide 1: junk image with a wide administrative letterhead beside it.
    first = document.new_page(width=1200, height=2000)
    first.insert_image(fitz_module.Rect(300, 300, 900, 900), stream=_png(fitz_module, 400, 320))
    first.insert_text((80, 320), "Department of Computer Science", fontsize=12)
    first.insert_text((80, 360), "CS 202 - Data Structures", fontsize=12)
    first.insert_text((80, 400), "Email: cs202@university.edu", fontsize=12)
    first.insert_text((80, 440), "Instructor: Prof. Salem", fontsize=12)
    # Slide 2: a genuine figure with a short technical caption.
    second = document.new_page(width=1200, height=2000)
    second.insert_text((300, 280), "Figure 4: Congestion window growth", fontsize=12)
    second.insert_image(fitz_module.Rect(300, 300, 900, 900), stream=_png(fitz_module, 400, 320))
    data = document.tobytes()
    document.close()

    diagrams = extract_pdf_diagrams(data)
    assert len(diagrams) == 1
    assert diagrams[0].page_number == 2
