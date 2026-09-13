"""FSRS-4.5 scheduler (Difficulty-Stability-Retrievability model).

Reference: Free Spaced Repetition Scheduler v4.5 with the adapted 19-parameter
weight vector. All formulas follow the published model:

    R(t, S)      = (1 + FACTOR * t / S) ** DECAY
    I(r, S)      = S / FACTOR * (r ** (1 / DECAY) - 1)
    S0(G)        = w[G-1]                                (initial stability)
    D0(G)        = clamp(w[4] - (G - 3) * w[5], 1, 10)   (initial difficulty)
    D'(D, G)     = w[7]*D0(4) + (1 - w[7]) * (D - w[6]*(G-3))
    S'_recall    = S * (1 + e^{w[8]} * (11 - D) * S^{-w[9]}
                        * (e^{w[10]*(1-R)} - 1) * hard * easy)
    S'_lapse     = w[11] * D^{-w[12]} * ((S + 1)^{w[13]} - 1) * e^{w[14]*(1-R)}

The module is pure: no database or HTTP imports. ``main.py`` reads the row,
calls :meth:`FSRS4_5.review`, and writes the result back.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

# --- ratings (FSRS scale, matches the UI buttons) --------------------------
RATING_AGAIN = 1
RATING_HARD = 2
RATING_GOOD = 3
RATING_EASY = 4
VALID_RATINGS = (RATING_AGAIN, RATING_HARD, RATING_GOOD, RATING_EASY)

# --- card states ----------------------------------------------------------
STATE_NEW = 1
STATE_LEARNING = 2
STATE_REVIEW = 3
STATE_RELEARNING = 4

STATE_NAMES = {
    STATE_NEW: "new",
    STATE_LEARNING: "learning",
    STATE_REVIEW: "review",
    STATE_RELEARNING: "relearning",
}

# FSRS-4.5 default parameters (19 weights, trained on the public collection).
DEFAULT_PARAMETERS: tuple[float, ...] = (
    0.4072,  # w0  S0(Again)
    1.1829,  # w1  S0(Hard)
    3.1262,  # w2  S0(Good)
    15.4722,  # w3  S0(Easy)
    7.2102,  # w4  D0 base
    0.5316,  # w5  D0 grading slope
    1.0651,  # w6  difficulty delta per grade step
    0.0234,  # w7  difficulty mean-reversion weight
    1.616,  # w8  stability growth scale
    0.1544,  # w9  stability saturation exponent
    1.0824,  # w10 retrievability sensitivity
    1.9813,  # w11 post-lapse stability scale
    0.0953,  # w12 post-lapse difficulty exponent
    0.2975,  # w13 post-lapse stability saturation
    2.2042,  # w14 post-lapse retrievability sensitivity
    0.2407,  # w15 Hard penalty
    2.9466,  # w16 Easy bonus
    0.5034,  # w17 short-term (same-day) stability scale
    0.6567,  # w18 short-term stability exponent
)

DECAY = -0.5
FACTOR = 19.0 / 81.0  # 0.9 ** (1 / DECAY) - 1
MIN_DIFFICULTY = 1.0
MAX_DIFFICULTY = 10.0
MIN_STABILITY = 0.01
MAX_STABILITY = 36500.0

# Learning-step intervals used while a card is in Learning/Relearning.
AGAIN_MINUTES = 1.0
HARD_MINUTES = 6.0
RELEARNING_MINUTES = 10.0
GRADUATING_DAYS = 1.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(moment: Optional[datetime]) -> Optional[datetime]:
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


@dataclass
class SchedulingResult:
    """Everything the API layer needs to persist and report."""

    state: int
    stability: float
    difficulty: float
    due_date: datetime
    last_review: datetime
    interval_days: float
    retrievability: Optional[float]
    reps: int
    lapses: int
    rating: int
    elapsed_days: float
    state_name: str = field(default="")

    def __post_init__(self) -> None:
        self.state_name = STATE_NAMES.get(self.state, "unknown")

    @property
    def is_lapse(self) -> bool:
        return self.rating == RATING_AGAIN


@dataclass
class CardSnapshot:
    """Current DB row state passed into the scheduler."""

    state: int = STATE_NEW
    stability: float = 0.0
    difficulty: float = 0.0
    due_date: Optional[datetime] = None
    last_review: Optional[datetime] = None
    reps: int = 0
    lapses: int = 0


def retrievability(stability: float, elapsed_days: float) -> float:
    """Probability of recall after ``elapsed_days`` given ``stability`` days."""
    if stability <= 0:
        return 0.0
    if elapsed_days <= 0:
        return 1.0
    value = (1.0 + FACTOR * elapsed_days / stability) ** DECAY
    return _clamp(value, 0.0, 1.0)


def interval_for_retention(stability: float, desired_retention: float) -> float:
    """Days until recall probability decays to ``desired_retention``."""
    desired_retention = _clamp(desired_retention, 0.70, 0.99)
    stability = max(stability, MIN_STABILITY)
    days = stability / FACTOR * (desired_retention ** (1.0 / DECAY) - 1.0)
    return _clamp(days, 0.0, MAX_STABILITY)


def _initial_stability(rating: int, w: tuple[float, ...]) -> float:
    return max(MIN_STABILITY, w[rating - 1])


def _initial_difficulty(rating: int, w: tuple[float, ...]) -> float:
    value = w[4] - (rating - 3) * w[5]
    return _clamp(value, MIN_DIFFICULTY, MAX_DIFFICULTY)


def _next_difficulty(difficulty: float, rating: int, w: tuple[float, ...]) -> float:
    difficulty = _clamp(difficulty, MIN_DIFFICULTY, MAX_DIFFICULTY)
    delta = difficulty - w[6] * (rating - 3)
    mean_reverted = w[7] * _initial_difficulty(RATING_EASY, w) + (1 - w[7]) * delta
    return _clamp(mean_reverted, MIN_DIFFICULTY, MAX_DIFFICULTY)


def _stability_on_recall(
    difficulty: float,
    stability: float,
    r: float,
    rating: int,
    w: tuple[float, ...],
) -> float:
    stability = max(stability, MIN_STABILITY)
    hard_penalty = w[15] if rating == RATING_HARD else 1.0
    easy_bonus = w[16] if rating == RATING_EASY else 1.0
    growth = (
        math.exp(w[8])
        * (11.0 - difficulty)
        * (stability ** -w[9])
        * (math.exp(w[10] * (1.0 - r)) - 1.0)
        * hard_penalty
        * easy_bonus
    )
    return _clamp(stability * (1.0 + growth), MIN_STABILITY, MAX_STABILITY)


def _stability_on_lapse(
    difficulty: float,
    stability: float,
    r: float,
    w: tuple[float, ...],
) -> float:
    stability = max(stability, MIN_STABILITY)
    post_lapse = (
        w[11]
        * (difficulty ** -w[12])
        * ((stability + 1.0) ** w[13] - 1.0)
        * math.exp(w[14] * (1.0 - r))
    )
    # A lapse never increases stability.
    return _clamp(min(post_lapse, stability), MIN_STABILITY, MAX_STABILITY)


def rating_from_result(is_correct: bool, elapsed_ms: int = 0, hints_used: int = 0) -> int:
    """Map a graded game attempt onto the FSRS rating scale.

    Correct and fast with no hints -> Easy; correct -> Good; correct but slow or
    hinted -> Hard; incorrect -> Again.
    """
    if not is_correct:
        return RATING_AGAIN
    if hints_used > 0:
        return RATING_HARD
    if 0 < elapsed_ms <= 8000:
        return RATING_EASY
    if elapsed_ms >= 45000:
        return RATING_HARD
    return RATING_GOOD


class FSRS4_5:
    """Stateful-free scheduler: ``review()`` is a pure function of card + grade."""

    def __init__(
        self,
        parameters: Optional[tuple[float, ...]] = None,
        desired_retention: float = 0.90,
        maximum_interval_days: int = 36500,
        fuzz: bool = True,
    ) -> None:
        weights = tuple(parameters or DEFAULT_PARAMETERS)
        if len(weights) != 19:
            raise ValueError(f"FSRS-4.5 needs exactly 19 weights, got {len(weights)}")
        self.w = weights
        self.desired_retention = _clamp(desired_retention, 0.70, 0.99)
        self.maximum_interval_days = maximum_interval_days
        self.fuzz = fuzz

    # -- helpers -----------------------------------------------------------
    def _fuzz(self, days: float, stability: float) -> float:
        """Spread due dates so review load does not spike (deterministic jitter)."""
        if not self.fuzz or days < 2.0:
            return days
        seed = int(abs(stability) * 1000) ^ int(days * 100)
        span = max(1.0, days * 0.05)
        jitter = ((seed % 1000) / 1000.0 - 0.5) * 2 * span
        return max(1.0, days + jitter)

    def _learning_interval_minutes(self, rating: int, relearning: bool) -> float:
        if rating == RATING_AGAIN:
            return AGAIN_MINUTES
        if rating == RATING_HARD:
            return HARD_MINUTES
        if relearning:
            return RELEARNING_MINUTES
        return 10.0

    # -- public API --------------------------------------------------------
    def review(
        self,
        card: CardSnapshot,
        rating: int,
        now: Optional[datetime] = None,
    ) -> SchedulingResult:
        """Apply one graded review and return the next card state."""
        if rating not in VALID_RATINGS:
            raise ValueError(f"rating must be one of {VALID_RATINGS}, got {rating!r}")
        now = _ensure_utc(now) or now_utc()
        last_review = _ensure_utc(card.last_review)

        elapsed_days = 0.0
        r: Optional[float] = None
        if last_review is not None and card.stability > 0:
            elapsed_days = max(0.0, (now - last_review).total_seconds() / 86400.0)
            r = retrievability(card.stability, elapsed_days)

        # "Never reviewed" is defined by state/stability/history, never by the
        # reps counter: a migrated or re-seeded row can carry reps == 0 while
        # holding real DSR state, and mistaking it for new would reset the card.
        is_first = card.state == STATE_NEW or card.stability <= 0 or last_review is None
        # ``due`` may be in the future if the learner opens a card early.
        overdue_days = 0.0
        card_due = _ensure_utc(card.due_date)
        if card_due is not None and last_review is not None:
            overdue_days = max(0.0, (now - card_due).total_seconds() / 86400.0)

        if is_first:
            stability = _initial_stability(rating, self.w)
            difficulty = _initial_difficulty(rating, self.w)
            if rating == RATING_AGAIN:
                state = STATE_LEARNING
                interval_days = AGAIN_MINUTES / 1440.0
            else:
                state = STATE_REVIEW
                interval_days = interval_for_retention(stability, self.desired_retention)
            reps = 1
            lapses = 0
            due = now + timedelta(minutes=max(1.0, interval_days * 1440.0))
            return SchedulingResult(
                state=state,
                stability=round(stability, 6),
                difficulty=round(difficulty, 6),
                due_date=due,
                last_review=now,
                interval_days=round(interval_days, 6),
                retrievability=None,
                reps=reps,
                lapses=lapses,
                rating=rating,
                elapsed_days=round(elapsed_days, 6),
            )

        assert r is not None  # card had a prior review, so retrievability exists
        difficulty = _next_difficulty(card.difficulty or _initial_difficulty(RATING_GOOD, self.w), rating, self.w)

        if rating == RATING_AGAIN:
            stability = _stability_on_lapse(card.difficulty or difficulty, card.stability, r, self.w)
            state = STATE_RELEARNING
            interval_days = RELEARNING_MINUTES / 1440.0
            lapses = card.lapses + 1
        else:
            stability = _stability_on_recall(difficulty, card.stability, r, rating, self.w)
            if card.state == STATE_RELEARNING or card.state == STATE_LEARNING:
                # Graduate out of the (re)learning queue on a successful recall.
                state = STATE_REVIEW
                interval_days = min(
                    max(GRADUATING_DAYS, interval_for_retention(stability, self.desired_retention)),
                    float(self.maximum_interval_days),
                )
            else:
                state = STATE_REVIEW
                interval_days = min(
                    interval_for_retention(stability, self.desired_retention),
                    float(self.maximum_interval_days),
                )
            lapses = card.lapses

        if interval_days >= 1.0:
            interval_days = self._fuzz(interval_days, stability)
            interval_days = _clamp(interval_days, 1.0, float(self.maximum_interval_days))
            due = now + timedelta(days=interval_days)
        else:
            minutes = max(1.0, interval_days * 1440.0)
            due = now + timedelta(minutes=minutes)

        return SchedulingResult(
            state=state,
            stability=round(stability, 6),
            difficulty=round(difficulty, 6),
            due_date=due,
            last_review=now,
            interval_days=round(interval_days, 6),
            retrievability=round(r, 6),
            reps=card.reps + 1,
            lapses=lapses,
            rating=rating,
            elapsed_days=round(elapsed_days + overdue_days, 6),
        )

    def preview(
        self,
        card: CardSnapshot,
        now: Optional[datetime] = None,
    ) -> dict[int, SchedulingResult]:
        """All four outcomes at once (used by the UI's interval hints)."""
        return {rating: self.review(card, rating, now) for rating in VALID_RATINGS}

    def current_retrievability(
        self, card: CardSnapshot, now: Optional[datetime] = None
    ) -> Optional[float]:
        last_review = _ensure_utc(card.last_review)
        if last_review is None or card.stability <= 0:
            return None
        now = _ensure_utc(now) or now_utc()
        elapsed = max(0.0, (now - last_review).total_seconds() / 86400.0)
        return round(retrievability(card.stability, elapsed), 6)


# Module-level default instance used by the API layer.
scheduler = FSRS4_5()


# ---------------------------------------------------------------------------
# Spec-facing facade
# ---------------------------------------------------------------------------


class FSRSScheduler:
    """DSR scheduler with the three-method surface the API layer calls.

    Thin, stateless facade over the formulas above: no duplicated maths, so a
    weight update or a formula fix lands in exactly one place. Use this class
    rather than the module functions when the caller only needs
    retrievability / next interval / updated parameters.
    """

    def __init__(
        self,
        parameters: Optional[tuple[float, ...]] = None,
        desired_retention: float = 0.90,
        maximum_interval_days: int = 36500,
        fuzz: bool = True,
    ) -> None:
        self.model = FSRS4_5(
            parameters=parameters,
            desired_retention=desired_retention,
            maximum_interval_days=maximum_interval_days,
            fuzz=fuzz,
        )
        self.desired_retention = self.model.desired_retention
        self.maximum_interval_days = self.model.maximum_interval_days

    # -- DSR equations -----------------------------------------------------
    def retrievability(self, elapsed_days: float, stability: float) -> float:
        """``R(t, S) = (1 + FACTOR * t / S) ** DECAY``."""
        return retrievability(stability, elapsed_days)

    def next_interval(self, stability: float) -> int:
        """Whole days until recall probability decays to the target retention."""
        return int(round(min(float(self.maximum_interval_days), interval_for_retention(stability, self.desired_retention))))

    def update_card(
        self,
        rating: int,
        stability: float,
        difficulty: float,
        elapsed_days: float,
    ) -> tuple[float, float, int]:
        """Apply one graded review.

        Returns ``(new_stability, new_difficulty, interval_days)``. A card with
        no history (``stability <= 0``) is treated as a first review, which is
        how the API reaches this method for freshly generated challenges.
        """
        if rating not in VALID_RATINGS:
            raise ValueError(f"rating must be one of {VALID_RATINGS}, got {rating!r}")
        elapsed_days = max(0.0, float(elapsed_days))
        r = retrievability(stability, elapsed_days) if stability > 0 else 0.0

        if stability <= 0:
            new_stability = _initial_stability(rating, self.model.w)
            new_difficulty = _initial_difficulty(rating, self.model.w)
            interval_days = (
                0
                if rating == RATING_AGAIN
                else self.next_interval(new_stability)
            )
            return (
                round(new_stability, 6),
                round(new_difficulty, 6),
                max(0, int(interval_days)),
            )

        new_difficulty = _next_difficulty(difficulty, rating, self.model.w)
        if rating == RATING_AGAIN:
            new_stability = _stability_on_lapse(difficulty, stability, r, self.model.w)
            interval_days = 0  # back into the relearning queue (minutes, not days)
        else:
            new_stability = _stability_on_recall(difficulty, stability, r, rating, self.model.w)
            interval_days = self.next_interval(new_stability)

        return (
            round(new_stability, 6),
            round(new_difficulty, 6),
            max(0, int(interval_days)),
        )

    # -- helpers used by the endpoints -------------------------------------
    def preview(
        self,
        stability: float,
        difficulty: float,
        elapsed_days: float,
    ) -> dict[int, tuple[float, float, int]]:
        """All four outcomes at once, keyed by rating."""
        return {
            rating: self.update_card(rating, stability, difficulty, elapsed_days)
            for rating in VALID_RATINGS
        }

    def due_date_for(self, interval_days: float, now: Optional[datetime] = None) -> datetime:
        """``now + interval`` where a 0-day interval means the relearning step."""
        now = _ensure_utc(now) or now_utc()
        if interval_days <= 0:
            return now + timedelta(minutes=RELEARNING_MINUTES)
        return now + timedelta(days=float(interval_days))


fsrs_scheduler = FSRSScheduler()
