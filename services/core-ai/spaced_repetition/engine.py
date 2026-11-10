"""Ebbinghaus forgetting-curve maths for the Daily Memory Warm-up.

Pure functions over plain data (no ORM, no HTTP), covering:

* the canonical Ebbinghaus ladder ``1 -> 3 -> 7 -> 14 -> 30`` days mapped onto
  integer ``repetition_level`` values 1..5,
* FSRS-4.5 retrievability ``R(t, S) = (1 + 19/81 * t / S) ** -0.5`` so the
  engine stays numerically identical to :mod:`fsrs.scheduler`,
* the pedagogical partition of the daily queue (Yesterday's Recap / Mid-Term
  Memory / Critical Decay),
* the warm-up ritual heuristics (streak-saver flag, XP bonus multiplier and
  the "3 minutes" estimate that keeps the ritual low-friction).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

# --- the Ebbinghaus ladder ---------------------------------------------------
# repetition_level 1..5 <-> the day the concept should next be rehearsed.
EBBINGHAUS_INTERVAL_DAYS: tuple[float, ...] = (1.0, 3.0, 7.0, 14.0, 30.0)

# Review-worthiness threshold: a card whose estimated recall probability has
# decayed below this is due even when ``due_at`` is still in the future.
RETENTION_THRESHOLD = 0.85

# Critical decay: below this recall probability the card is not merely due —
# it is about to be lost and needs an immediate refresher.
CRITICAL_RETENTION = 0.60

# "Yesterday's Recap" window: material learned 24-48 hours ago that needs its
# first consolidation pass (Ebbinghaus day 1).
YESTERDAY_WINDOW_HOURS = 48.0

# Gamified warm-up reward: reviews answered inside the daily ritual earn 1.5x XP.
WARMUP_XP_MULTIPLIER = 1.5

# Pedagogical buckets (Arabic labels are rendered client-side).
BUCKET_YESTERDAY = "yesterday"
BUCKET_MIDTERM = "midterm"
BUCKET_CRITICAL = "critical"
BUCKETS = (BUCKET_YESTERDAY, BUCKET_MIDTERM, BUCKET_CRITICAL)

BUCKET_LABELS: dict[str, str] = {
    BUCKET_YESTERDAY: "مراجعة الأمس",
    BUCKET_MIDTERM: "مراجعة الأيام الـ 3 والـ 7",
    BUCKET_CRITICAL: "معرض للنسيان!",
}

# Warm-up pacing: ~25 seconds per card keeps the ritual inside 3 minutes.
SECONDS_PER_CARD = 25


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(moment: Optional[datetime]) -> Optional[datetime]:
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def interval_days_for_level(repetition_level: int) -> float:
    """The Ebbinghaus day for a 1..5 level (level 5 clamps to 30)."""
    index = max(1, min(len(EBBINGHAUS_INTERVAL_DAYS), int(repetition_level or 1))) - 1
    return EBBINGHAUS_INTERVAL_DAYS[index]


def repetition_level_for_interval(interval_days: float) -> int:
    """Ebbinghaus rung an interval has reached: ``1d -> 1, 3d -> 2, 7d -> 3,
    14d -> 4, 30d+ -> 5``.

    Sub-day intervals (learning/relearning steps measured in minutes) map to
    level 0 — the concept has not yet entered the consolidation ladder. An
    interval of exactly 1 day is rung 1, 2.9 days is still rung 1, 3.0 days is
    rung 2, and so on.
    """
    days = max(0.0, float(interval_days or 0.0))
    level = 0
    for rung in EBBINGHAUS_INTERVAL_DAYS:
        if days >= rung:
            level += 1
        else:
            break
    return level


def next_repetition_level(previous_level: int, *, lapsed: bool) -> int:
    """The rung on the 1/3/7/14/30 ladder after one graded review.

    Every completed review advances consolidation one rung — the spec's
    ``Day 1 -> Day 3 -> Day 7`` ritual — while the actual ``due_at`` timestamp
    still comes from FSRS. A lapse (Again) restarts consolidation at the Day-1
    rung; the top rung (Day 30) is absorbing.
    """
    if lapsed:
        return 1
    previous = max(0, int(previous_level or 0))
    return min(len(EBBINGHAUS_INTERVAL_DAYS), previous + 1)


def due_at_for_level(
    level: int, *, now: Optional[datetime] = None
) -> datetime:
    """``now + Ebbinghaus interval`` for the given repetition level."""
    moment = _ensure_utc(now) or utcnow()
    return moment + timedelta(days=interval_days_for_level(level))


def retrievability(stability: float, elapsed_days: float) -> float:
    """FSRS-4.5 ``R(t, S)`` — identical formula to ``fsrs.scheduler``."""
    import math

    if not stability or stability <= 0:
        return 0.0
    if elapsed_days <= 0:
        return 1.0
    factor = 19.0 / 81.0
    decay = -0.5
    value = (1.0 + factor * elapsed_days / stability) ** decay
    return min(1.0, max(0.0, value))


def is_due(
    *,
    due_at: Optional[datetime],
    retrievability_score: Optional[float],
    now: Optional[datetime] = None,
    threshold: float = RETENTION_THRESHOLD,
) -> bool:
    """The daily-queue predicate: overdue **or** decayed below threshold."""
    moment = _ensure_utc(now) or utcnow()
    due = _ensure_utc(due_at)
    if due is not None and due <= moment:
        return True
    if retrievability_score is not None and float(retrievability_score) < threshold:
        return True
    return False


@dataclass
class CardFacts:
    """Minimal card view the bucketing needs (plain data, no ORM)."""

    repetition_level: int = 0
    retrievability_score: Optional[float] = None
    stability: float = 0.0
    due_at: Optional[datetime] = None
    last_reviewed_at: Optional[datetime] = None
    lapses: int = 0
    reps: int = 0

    def with_utc(self) -> "CardFacts":
        self.due_at = _ensure_utc(self.due_at)
        self.last_reviewed_at = _ensure_utc(self.last_reviewed_at)
        return self


def effective_retrievability(
    card: CardFacts, *, now: Optional[datetime] = None
) -> float:
    """Live recall probability: the stored score, re-derived if it went stale.

    ``retrievability_score`` is persisted on write-back, but the queue must not
    wait for a cron to notice decay — when a card has been reviewed before, the
    score is recomputed from ``stability`` and the time since
    ``last_reviewed_at`` so the partition reflects the moment of the request.
    """
    moment = _ensure_utc(now) or utcnow()
    last = _ensure_utc(card.last_reviewed_at)
    if card.stability > 0 and last is not None:
        elapsed = max(0.0, (moment - last).total_seconds() / 86400.0)
        return retrievability(card.stability, elapsed)
    if card.retrievability_score is not None:
        return max(0.0, min(1.0, float(card.retrievability_score)))
    return 0.0


def bucket_for_card(
    card: CardFacts,
    *,
    now: Optional[datetime] = None,
    retention_threshold: float = RETENTION_THRESHOLD,
    critical_threshold: float = CRITICAL_RETENTION,
) -> str:
    """Which pedagogical bucket the due card belongs to.

    * **yesterday** — never reviewed (freshly seeded) or learned within the
      last 48 hours: the day-1 consolidation pass.
    * **critical** — recall probability below the critical floor, or the card
      has lapsed repeatedly (2+ lapses): it is at real risk of being forgotten.
    * **midterm** — everything else on the day-3/day-7 (and beyond) rungs.
    """
    moment = _ensure_utc(now) or utcnow()
    last = _ensure_utc(card.last_reviewed_at)

    # No review history: a never-reviewed card seeded by an upload is immediate
    # consolidation material — the "learned yesterday" bucket pedagogically.
    # (Checked first: its derived recall probability is 0.0, which must not be
    # mistaken for decay.)
    if last is None and (card.reps or 0) == 0:
        return BUCKET_YESTERDAY

    r = effective_retrievability(card, now=moment)
    if r < critical_threshold or (card.lapses or 0) >= 2:
        return BUCKET_CRITICAL

    if last is not None:
        hours_since = (moment - last).total_seconds() / 3600.0
        if hours_since <= YESTERDAY_WINDOW_HOURS:
            return BUCKET_YESTERDAY

    # A heavily decayed card that has not crossed the critical floor still
    # belongs to mid-term refreshment.
    return BUCKET_MIDTERM


def bucket_sort_key(card: CardFacts, *, now: Optional[datetime] = None) -> tuple[float, float]:
    """Within-bucket ordering: most decayed first, ties break on due date.

    Ascending recall probability serves the most fragile memory first; the
    due-date ordinal keeps deterministic ordering within a recall band.
    """
    r = effective_retrievability(card, now=now)
    due = _ensure_utc(card.due_at) or _ensure_utc(now) or utcnow()
    return (r, due.timestamp())


def estimated_minutes(due_count: int) -> int:
    """Rounded warm-up length in minutes (the "3 minutes" promise)."""
    return max(1, round(due_count * SECONDS_PER_CARD / 60.0))


def is_streak_at_risk(
    *,
    completed_today: bool,
    streak_count: int,
    now: Optional[datetime] = None,
) -> bool:
    """True when today's review is still pending and a streak is on the line.

    The streak-saver beat: the banner shouts harder when there is something to
    lose (streak >= 1 and no warm-up finished today yet).
    """
    if completed_today or streak_count < 1:
        return False
    moment = _ensure_utc(now) or utcnow()
    # Nothing special about the hour — the daily ritual resets at midnight, so
    # the streak is "at risk" all day until the warm-up is done.
    _ = moment
    return True


@dataclass
class WarmupPlan:
    """The banner/arena contract: buckets, totals and reward hints."""

    now: datetime = field(default_factory=utcnow)
    yesterday: list[CardFacts] = field(default_factory=list)
    midterm: list[CardFacts] = field(default_factory=list)
    critical: list[CardFacts] = field(default_factory=list)

    @property
    def total_due_count(self) -> int:
        return len(self.yesterday) + len(self.midterm) + len(self.critical)

    @property
    def estimated_minutes(self) -> int:
        return estimated_minutes(self.total_due_count)

    @property
    def xp_bonus_multiplier(self) -> float:
        return WARMUP_XP_MULTIPLIER

    @property
    def buckets(self) -> dict[str, list[CardFacts]]:
        return {
            BUCKET_YESTERDAY: self.yesterday,
            BUCKET_MIDTERM: self.midterm,
            BUCKET_CRITICAL: self.critical,
        }
