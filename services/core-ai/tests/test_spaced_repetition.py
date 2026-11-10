"""Ebbinghaus forgetting-curve engine + Daily Memory Warm-up queue (pure).

Covers the spec'd verification without a database:

* card creation at Day 0 / Day 1 / Day 3 / Day 7 — the engine must identify
  due cards from timestamps *and* the 0.85 retrievability threshold,
* the daily-queue partition into Yesterday's Recap / Mid-Term Memory /
  Critical Decay,
* completing a review advances the repetition level and schedules the next
  Ebbinghaus review date (Day 1 -> Day 3 -> Day 7),
* the folder Memory-Health meter and its green/amber/red bands.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from models import (
    FsrsCardView,
    MemoryHealth,
    card_facts_from_row,
    memory_health,
    next_card_state,
    queue_partition,
)
from spaced_repetition.engine import (
    BUCKET_CRITICAL,
    BUCKET_MIDTERM,
    BUCKET_YESTERDAY,
    CRITICAL_RETENTION,
    EBBINGHAUS_INTERVAL_DAYS,
    RETENTION_THRESHOLD,
    WARMUP_XP_MULTIPLIER,
    CardFacts,
    bucket_for_card,
    effective_retrievability,
    estimated_minutes,
    interval_days_for_level,
    is_due,
    next_repetition_level,
    repetition_level_for_interval,
    retrievability,
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def _view(
    *,
    last_reviewed_at: Optional[datetime] = None,
    due_at: Optional[datetime] = None,
    stability: float = 0.0,
    repetition_level: int = 0,
    retrievability_score: float = 0.0,
    reps: int = 0,
    lapses: int = 0,
) -> FsrsCardView:
    return FsrsCardView(
        card_id="00000000-0000-0000-0000-000000000001",
        challenge_id="00000000-0000-0000-0000-000000000002",
        user_id="00000000-0000-0000-0000-000000000003",
        last_reviewed_at=last_reviewed_at,
        due_at=due_at,
        repetition_level=repetition_level,
        retrievability_score=retrievability_score,
        stability=stability,
        difficulty=5.0,
        state=3,
        reps=reps,
        lapses=lapses,
    )


# ---------------------------------------------------------------------------
# The Ebbinghaus ladder
# ---------------------------------------------------------------------------


def test_ebbinghaus_ladder_is_canonical():
    assert EBBINGHAUS_INTERVAL_DAYS == (1.0, 3.0, 7.0, 14.0, 30.0)
    assert interval_days_for_level(1) == 1
    assert interval_days_for_level(2) == 3
    assert interval_days_for_level(3) == 7
    assert interval_days_for_level(4) == 14
    assert interval_days_for_level(5) == 30
    # Out-of-range levels clamp, never crash.
    assert interval_days_for_level(0) == 1
    assert interval_days_for_level(9) == 30


def test_repetition_level_maps_intervals_onto_rungs():
    assert repetition_level_for_interval(0.5) == 0  # minutes-step: not on the ladder
    assert repetition_level_for_interval(1.0) == 1
    assert repetition_level_for_interval(2.9) == 1
    assert repetition_level_for_interval(3.0) == 2
    assert repetition_level_for_interval(7.0) == 3
    assert repetition_level_for_interval(14.0) == 4
    assert repetition_level_for_interval(30.0) == 5
    assert repetition_level_for_interval(365.0) == 5


def test_rating_again_resets_to_day_one_and_recall_climbs_the_ladder():
    # A lapse (Again) restarts consolidation at the Day-1 rung...
    assert next_repetition_level(1, lapsed=True) == 1
    assert next_repetition_level(5, lapsed=True) == 1
    # ...while every completed recall advances one rung (top rung absorbs):
    assert next_repetition_level(0, lapsed=False) == 1
    assert next_repetition_level(1, lapsed=False) == 2
    assert next_repetition_level(2, lapsed=False) == 3
    assert next_repetition_level(4, lapsed=False) == 5
    assert next_repetition_level(5, lapsed=False) == 5
    assert next_repetition_level(99, lapsed=False) == 5


def test_due_at_for_level_schedules_the_ladder():
    from spaced_repetition.engine import due_at_for_level

    assert due_at_for_level(2, now=NOW) - NOW == timedelta(days=3)
    assert due_at_for_level(3, now=NOW) - NOW == timedelta(days=7)


# ---------------------------------------------------------------------------
# FSRS retrievability parity
# ---------------------------------------------------------------------------


def test_retrievability_matches_fsrs_scheduler_formula():
    from fsrs.scheduler import retrievability as fsrs_retrievability

    for stability in (0.5, 1.0, 3.1262, 15.0, 120.0):
        for elapsed in (0.0, 0.5, 1.0, 3.0, 7.0, 30.0):
            assert retrievability(stability, elapsed) == pytest.approx(
                fsrs_retrievability(stability, elapsed), abs=1e-9
            )


def test_retrievability_decays_monotonically_and_clamps():
    assert retrievability(3.0, 0.0) == 1.0
    assert 0.0 < retrievability(3.0, 1.0) < 1.0
    assert retrievability(3.0, 1.0) > retrievability(3.0, 3.0) > retrievability(3.0, 7.0)
    assert retrievability(0.0, 5.0) == 0.0


# ---------------------------------------------------------------------------
# Day 0 / Day 1 / Day 3 / Day 7 due detection (spec scenario)
# ---------------------------------------------------------------------------


def test_day0_card_is_due_immediately():
    # Learned "today" (last reviewed 2h ago, day-1 interval): not yet overdue by
    # timestamp, but a fresh card has no stability -> recall probability is 0.
    card = _view(
        last_reviewed_at=NOW - timedelta(hours=2),
        due_at=NOW + timedelta(days=1),
        stability=0.0,
        retrievability_score=0.0,
        reps=1,
    )
    assert card.is_due(now=NOW) is True


def test_day1_card_is_due_by_timestamp():
    # Learned yesterday at the same hour: due_at == NOW exactly.
    card = _view(
        last_reviewed_at=NOW - timedelta(days=1),
        due_at=NOW,
        stability=1.0,
        retrievability_score=0.9,
        reps=1,
    )
    assert card.is_due(now=NOW) is True


def test_day3_card_is_due_when_overdue():
    card = _view(
        last_reviewed_at=NOW - timedelta(days=4),
        due_at=NOW - timedelta(days=1),
        stability=3.0,
        retrievability_score=0.7,
        reps=2,
    )
    assert card.is_due(now=NOW) is True


def test_day7_card_not_due_while_recall_probability_stays_high():
    # Reviewed 1 day ago with 7-day stability: R is still ~0.94 and due_at is
    # in the future -> stays out of the queue.
    card = _view(
        last_reviewed_at=NOW - timedelta(days=1),
        due_at=NOW + timedelta(days=6),
        stability=7.0,
        retrievability_score=0.94,
        reps=3,
    )
    assert card.is_due(now=NOW) is False


def test_card_due_on_retrievability_threshold_alone():
    # due_at in the future but recall probability decayed below 0.85: the
    # forgetting curve pulls the card into today's queue anyway. (Live R after
    # 6 days at S=3 is ~0.825 — the view re-derives it from stability.)
    card = _view(
        last_reviewed_at=NOW - timedelta(days=6),
        due_at=NOW + timedelta(days=1),
        stability=3.0,
        retrievability_score=RETENTION_THRESHOLD - 0.01,
        reps=2,
    )
    assert card.is_due(now=NOW) is True
    # Exactly at the threshold is still "held" (strictly-below comparison).
    assert is_due(
        due_at=card.due_at,
        retrievability_score=RETENTION_THRESHOLD,
        now=NOW,
    ) is False


# ---------------------------------------------------------------------------
# Daily-queue partitioning
# ---------------------------------------------------------------------------


def test_queue_partitions_into_three_pedagogical_buckets():
    cards = [
        # Learned 30h ago at S=1d (live R≈0.88), overdue by 6h, inside the
        # 48h window -> yesterday's recap.
        _view(
            last_reviewed_at=NOW - timedelta(hours=30),
            due_at=NOW - timedelta(hours=6),
            stability=1.0,
            retrievability_score=0.88,
            reps=1,
        ),
        # Day-3 rung, live R≈0.75 after 10 days at S=3 (past the 48h window,
        # above the critical floor) -> midterm.
        _view(
            last_reviewed_at=NOW - timedelta(days=10),
            due_at=NOW - timedelta(days=1),
            stability=3.0,
            retrievability_score=0.75,
            reps=2,
        ),
        # Live R≈0.58 after 25 days at S=3 — below the 0.60 critical floor
        # -> critical.
        _view(
            last_reviewed_at=NOW - timedelta(days=25),
            due_at=NOW - timedelta(days=7),
            stability=3.0,
            retrievability_score=0.58,
            reps=2,
        ),
        # Healthy, not due (R≈0.98 after 1 day at S=7) -> nowhere.
        _view(
            last_reviewed_at=NOW - timedelta(days=1),
            due_at=NOW + timedelta(days=6),
            stability=7.0,
            retrievability_score=0.94,
            reps=3,
        ),
    ]
    buckets = queue_partition(cards, now=NOW)
    assert buckets[BUCKET_YESTERDAY] and len(buckets[BUCKET_YESTERDAY]) == 1
    assert len(buckets[BUCKET_MIDTERM]) == 1
    assert len(buckets[BUCKET_CRITICAL]) == 1


def test_repeated_lapses_land_in_critical_even_with_decent_recall():
    card = _view(
        last_reviewed_at=NOW - timedelta(hours=30),
        due_at=NOW - timedelta(hours=2),
        stability=1.5,
        retrievability_score=0.8,
        reps=5,
        lapses=2,
    )
    assert bucket_for_card(card.to_facts(), now=NOW) == BUCKET_CRITICAL


def test_within_bucket_ordering_serves_most_decayed_first():
    # Same last-review time, different stability: the S=2 card has decayed
    # further (live R≈0.79) than the S=3 card (live R≈0.85) and is served first.
    cards = [
        _view(
            last_reviewed_at=NOW - timedelta(days=5),
            due_at=NOW - timedelta(days=1),
            stability=3.0,
            reps=2,
        ),
        _view(
            last_reviewed_at=NOW - timedelta(days=5),
            due_at=NOW - timedelta(days=1),
            stability=2.0,
            reps=2,
        ),
    ]
    buckets = queue_partition(cards, now=NOW)
    ordered = buckets[BUCKET_MIDTERM]
    assert effective_retrievability(ordered[0].to_facts(), now=NOW) < (
        effective_retrievability(ordered[1].to_facts(), now=NOW)
    )
    assert ordered[0].stability == 2.0


def test_never_reviewed_card_counts_as_yesterdays_recap():
    card = _view(due_at=NOW - timedelta(hours=1), stability=0.0, retrievability_score=0.0)
    assert bucket_for_card(card.to_facts(), now=NOW) == BUCKET_YESTERDAY


# ---------------------------------------------------------------------------
# Warm-up envelope
# ---------------------------------------------------------------------------


def test_estimated_minutes_matches_the_three_minute_promise():
    assert estimated_minutes(0) == 1  # never zero: an empty queue still "1 min"
    assert estimated_minutes(8) == 3  # 8 cards * 25s ~= 3.3 -> 3 minutes
    assert estimated_minutes(24) == 10


def test_warmup_multiplier_is_the_spec_value():
    assert WARMUP_XP_MULTIPLIER == 1.5


# ---------------------------------------------------------------------------
# next_card_state: Day 1 -> Day 3 -> Day 7 progression (spec scenario)
# ---------------------------------------------------------------------------


class _Row:
    """Stand-in for a database.FsrsCard ORM row (no ORM needed)."""

    def __init__(
        self,
        *,
        repetition_level: int = 0,
        stability: float = 0.0,
    ) -> None:
        self.repetition_level = repetition_level
        self.stability = stability


def test_first_good_review_starts_the_ladder_at_day_one():
    derived = next_card_state(
        _Row(repetition_level=0, stability=0.0),
        rating=3,
        new_stability=1.0,
        new_difficulty=5.0,
        next_due=NOW + timedelta(days=1),
        now=NOW,
    )
    assert derived["repetition_level"] == 1
    assert derived["due_at"] - NOW == timedelta(days=1)
    assert derived["retrievability_score"] == 1.0


def test_review_progression_day1_to_day3_to_day7():
    row = _Row(repetition_level=0, stability=0.0)
    state = None
    for expected_level, days in ((1, 1.0), (2, 3.0), (3, 7.0)):
        moment = NOW + timedelta(days=days)
        state = next_card_state(
            row,
            rating=3,
            new_stability=days,
            new_difficulty=5.0,
            next_due=moment + timedelta(days=days),
            now=moment,
        )
        row.repetition_level = state["repetition_level"]
        row.stability = days
        assert row.repetition_level == expected_level
        assert state["due_at"] - moment == timedelta(days=days)
    # ...and the fourth success lands on the day-14 rung.
    moment = NOW + timedelta(days=14)
    state = next_card_state(
        row,
        rating=3,
        new_stability=14.0,
        new_difficulty=5.0,
        next_due=moment + timedelta(days=14),
        now=moment,
    )
    assert state["repetition_level"] == 4


def test_fsrs_good_chain_walks_the_ladder():
    """Driving the real FSRS scheduler at each due date advances the rungs."""
    from fsrs.scheduler import FSRSScheduler

    model = FSRSScheduler(fuzz=False)
    stability, difficulty, elapsed = 0.0, 0.0, 0.0
    levels = []
    for _ in range(5):
        stability, difficulty, interval = model.update_card(
            3, stability, difficulty, elapsed
        )
        level = next_repetition_level(levels[-1] if levels else 0, lapsed=False)
        levels.append(level)
        elapsed = float(interval)  # the next review happens at the due date
    # Successive on-schedule Good recalls consolidate: 1, 2, 3, 4, 5 — and FSRS
    # keeps growing the real interval underneath (stability > interval rung).
    assert levels == [1, 2, 3, 4, 5]
    assert stability > interval_days_for_level(5)


def test_lapse_resets_level_but_keeps_fsrs_maths_in_charge():
    derived = next_card_state(
        _Row(repetition_level=3, stability=7.0),
        rating=1,
        new_stability=0.8,
        new_difficulty=8.0,
        next_due=NOW + timedelta(minutes=10),
        now=NOW,
    )
    assert derived["repetition_level"] == 1
    assert derived["retrievability_score"] == 0.0


def test_hard_rating_still_advances_consolidation():
    # A Hard recall is still a successful recall: the rung advances (FSRS just
    # schedules a longer interval underneath).
    derived = next_card_state(
        _Row(repetition_level=3, stability=7.0),
        rating=2,
        new_stability=7.5,
        new_difficulty=6.0,
        next_due=NOW + timedelta(days=3),
        now=NOW,
    )
    assert derived["repetition_level"] == 4


# ---------------------------------------------------------------------------
# Memory Health meter
# ---------------------------------------------------------------------------


def test_memory_health_score_and_bands():
    healthy = memory_health(
        [
            _view(
                last_reviewed_at=NOW - timedelta(days=1),
                due_at=NOW + timedelta(days=6),
                stability=7.0,
                retrievability_score=0.95,
                reps=3,
            )
        ],
        now=NOW,
    )
    assert isinstance(healthy, MemoryHealth)
    assert healthy.total == 1 and healthy.due == 0
    assert healthy.band == "green"
    # Live recall probability (1 day elapsed at S=7) — fresh, not literally 1.
    assert healthy.score >= 0.90

    decaying = memory_health(
        [
            _view(
                last_reviewed_at=NOW - timedelta(days=10),
                due_at=NOW + timedelta(days=1),
                stability=3.0,
                retrievability_score=0.75,
                reps=2,
            )
        ],
        now=NOW,
    )
    assert decaying.band == "amber"

    critical = memory_health(
        [
            _view(
                last_reviewed_at=NOW - timedelta(days=25),
                due_at=NOW - timedelta(days=7),
                stability=3.0,
                retrievability_score=0.5,
                reps=2,
            )
        ],
        now=NOW,
    )
    assert critical.band == "red"
    assert critical.due == 1


def test_memory_health_of_an_empty_scope_is_perfect():
    assert memory_health([], now=NOW).score == 1.0
    assert memory_health([], now=NOW).band == "green"


def test_card_facts_from_row_derives_score_for_legacy_rows():
    class _LegacyRow:
        id = "00000000-0000-0000-0000-000000000001"
        challenge_id = "00000000-0000-0000-0000-000000000002"
        user_id = "00000000-0000-0000-0000-000000000003"
        state = 3
        stability = 3.1262
        difficulty = 5.0
        reps = 2
        lapses = 0
        repetition_level = 0  # pre-column rows have no rung yet
        retrievability_score = 0.0
        last_review = NOW - timedelta(days=1)
        due_date = NOW + timedelta(days=2)

    view = card_facts_from_row(_LegacyRow())
    assert view.repetition_level == 0
    # Score was re-derived from stability + elapsed time (1 day at S=3.1262).
    assert 0.85 <= view.retrievability_score < 1.0
