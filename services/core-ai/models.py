"""Spec-facing FsrsCard model for the Ebbinghaus Daily Memory Warm-up.

``database.FsrsCard`` is the durable ORM row; this module is the
domain-language layer the daily-queue endpoint and the tests speak:

* ``last_reviewed_at`` / ``due_at`` — the spec names for the ORM's
  ``last_review`` / ``due_date`` columns,
* ``repetition_level`` — integer 1..5 onto the Ebbinghaus ladder
  (1 = Day 1, 2 = Day 3, 3 = Day 7, 4 = Day 14, 5 = Day 30),
* ``retrievability_score`` — estimated recall probability in ``0.0..1.0``.

It also derives the *next* card state after a graded review (advancing the
repetition level and scheduling the next due date on the ladder) and exposes
the card-view used to partition the daily review queue.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from spaced_repetition.engine import (
    BUCKETS,
    BUCKET_CRITICAL,
    BUCKET_MIDTERM,
    BUCKET_YESTERDAY,
    RETENTION_THRESHOLD,
    CardFacts,
    bucket_for_card,
    bucket_sort_key,
    effective_retrievability,
    interval_days_for_level,
    is_due,
    next_repetition_level,
    retrievability,
)

__all__ = [
    "BUCKETS",
    "BUCKET_CRITICAL",
    "BUCKET_MIDTERM",
    "BUCKET_YESTERDAY",
    "RETENTION_THRESHOLD",
    "FsrsCardView",
    "MemoryHealth",
    "card_facts_from_row",
    "memory_health",
    "next_card_state",
    "queue_partition",
]


@dataclass
class FsrsCardView:
    """Spec-named projection of one ``database.FsrsCard`` row."""

    card_id: str
    challenge_id: str
    user_id: str
    last_reviewed_at: Optional[datetime]
    due_at: Optional[datetime]
    repetition_level: int
    retrievability_score: float
    stability: float
    difficulty: float
    state: int
    reps: int
    lapses: int

    def to_facts(self) -> CardFacts:
        return CardFacts(
            repetition_level=self.repetition_level,
            retrievability_score=self.retrievability_score,
            stability=self.stability,
            due_at=self.due_at,
            last_reviewed_at=self.last_reviewed_at,
            lapses=self.lapses,
            reps=self.reps,
        )

    def is_due(self, *, now: Optional[datetime] = None) -> bool:
        """Overdue by timestamp **or** decayed below the retention threshold.

        Uses the live recall probability (re-derived from stability + elapsed
        time) so the queue never waits for a cron to notice decay; cards with
        no stability yet fall back to the stored score.
        """
        return is_due(
            due_at=self.due_at,
            retrievability_score=effective_retrievability(self.to_facts(), now=now),
            now=now,
        )

    def to_payload(self, *, now: Optional[datetime] = None) -> dict[str, Any]:
        """JSON-safe shape returned inside the daily queue."""
        bucket = bucket_for_card(self.to_facts(), now=now)
        return {
            "card_id": self.card_id,
            "challenge_id": self.challenge_id,
            "user_id": self.user_id,
            "last_reviewed_at": self.last_reviewed_at,
            "due_at": self.due_at,
            "repetition_level": self.repetition_level,
            "retrievability_score": round(self.retrievability_score, 6),
            "stability": self.stability,
            "difficulty": self.difficulty,
            "state": self.state,
            "reps": self.reps,
            "lapses": self.lapses,
            "bucket": bucket,
        }


def card_facts_from_row(card: Any) -> FsrsCardView:
    """Build the view from a ``database.FsrsCard`` ORM row."""
    from datetime import timezone

    level = int(getattr(card, "repetition_level", 0) or 0)
    score = getattr(card, "retrievability_score", None)
    stability = float(card.stability or 0.0)
    last = getattr(card, "last_review", None)
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if (not score or float(score) <= 0.0) and level <= 0 and stability > 0 and last is not None:
        # Rows written before the Ebbinghaus columns existed (score still 0,
        # rung still 0): derive the recall probability on read.
        moment = datetime.now(timezone.utc)
        elapsed = max(0.0, (moment - last).total_seconds() / 86400.0)
        score = retrievability(stability, elapsed)
    return FsrsCardView(
        card_id=str(card.id),
        challenge_id=str(card.challenge_id),
        user_id=str(card.user_id),
        last_reviewed_at=last,
        due_at=card.due_date,
        repetition_level=level,
        retrievability_score=max(0.0, min(1.0, float(score or 0.0))),
        stability=stability,
        difficulty=float(card.difficulty or 0.0),
        state=int(card.state or 1),
        reps=int(card.reps or 0),
        lapses=int(card.lapses or 0),
    )


def next_card_state(
    card: Any,
    *,
    rating: int,
    new_stability: float,
    new_difficulty: float,
    next_due: datetime,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Ebbinghaus write-back fields for one graded review.

    ``rating`` follows the FSRS scale (1=Again .. 4=Easy); ``next_due`` is the
    FSRS-computed timestamp. Each completed review advances ``repetition_level``
    one rung on the 1/3/7/14/30 ladder (the spec's Day 1 -> Day 3 -> Day 7
    ritual); a lapse (Again) restarts consolidation at Day 1. The rung is a
    pedagogical counter — the FSRS scheduler stays in charge of the real
    ``due_at``.
    """
    moment = now or (next_due if next_due.tzinfo else next_due.replace(tzinfo=None))
    previous_level = int(getattr(card, "repetition_level", 0) or 0)
    lapsed = int(rating) == 1
    level = next_repetition_level(previous_level, lapsed=lapsed)
    return {
        "repetition_level": level,
        "retrievability_score": 0.0 if lapsed else 1.0,
        "last_reviewed_at": moment,
        "due_at": next_due,
        "interval_days_for_level": interval_days_for_level(level),
    }


@dataclass
class MemoryHealth:
    """Aggregated retention health for one scope (chapter or whole account)."""

    total: int
    fresh: int
    decaying: int
    critical: int
    due: int
    reviewed: int = 0
    average_retrievability: float = 1.0

    @property
    def score(self) -> float:
        """0.0..1.0 — the "صحة الذاكرة" percentage.

        The mean live recall probability of the reviewed cards: a literally
        interpretable number ("students remember ~74% of this chapter"), which
        maps straight onto the folder UI bands (green >= 90, amber >= 60).
        """
        return round(self.average_retrievability, 4)

    @property
    def band(self) -> str:
        """green 90-100%, amber 60-89%, red < 60% (matching the folder UI)."""
        pct = self.score * 100.0
        if pct >= 90.0:
            return "green"
        if pct >= 60.0:
            return "amber"
        return "red"


def memory_health(views: list[FsrsCardView], *, now: Optional[datetime] = None) -> MemoryHealth:
    """Aggregate card views into the folder Memory-Health meter."""
    moment = now
    total = len(views)
    fresh = decaying = critical = due = 0
    reviewed = 0
    r_sum = 0.0
    for view in views:
        facts = view.to_facts()
        r = effective_retrievability(facts, now=moment)
        bucket = bucket_for_card(facts, now=moment)
        if view.is_due(now=moment) or bucket == BUCKET_CRITICAL:
            due += 1
        # Never-reviewed (newly seeded) cards are "new material", not decayed
        # memory — they are counted in `total` but excluded from the score so a
        # fresh upload reads 100% healthy rather than 0%.
        if facts.last_reviewed_at is None and (facts.reps or 0) == 0:
            continue
        reviewed += 1
        r_sum += r
        if r >= RETENTION_THRESHOLD:
            fresh += 1
        elif r >= 0.60:
            decaying += 1
        else:
            critical += 1
    return MemoryHealth(
        total=total,
        fresh=fresh,
        decaying=decaying,
        critical=critical,
        due=due,
        reviewed=reviewed,
        average_retrievability=(r_sum / reviewed) if reviewed else 1.0,
    )


def queue_partition(
    views: list[FsrsCardView],
    *,
    now: Optional[datetime] = None,
) -> dict[str, list[FsrsCardView]]:
    """Split the due views into the three pedagogical buckets, most-decayed first."""
    moment = now
    buckets: dict[str, list[FsrsCardView]] = {name: [] for name in BUCKETS}
    for view in views:
        if not view.is_due(now=moment):
            continue
        facts = view.to_facts()
        name = bucket_for_card(facts, now=moment)
        buckets.setdefault(name, []).append(view)
    for name, items in buckets.items():
        items.sort(key=lambda view: bucket_sort_key(view.to_facts(), now=moment))
    return buckets
