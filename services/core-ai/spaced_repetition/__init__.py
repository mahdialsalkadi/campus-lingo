"""Spaced Repetition engine: Ebbinghaus forgetting-curve scheduling.

Pure package — no database or HTTP imports — so the scheduling maths is
unit-testable in isolation. ``models.py`` is the spec-facing facade;
``database.FsrsCard`` is the durable row.
"""

from spaced_repetition.engine import (
    BUCKETS,
    BUCKET_CRITICAL,
    BUCKET_MIDTERM,
    BUCKET_YESTERDAY,
    BUCKET_LABELS,
    CRITICAL_RETENTION,
    EBBINGHAUS_INTERVAL_DAYS,
    RETENTION_THRESHOLD,
    WARMUP_XP_MULTIPLIER,
    YESTERDAY_WINDOW_HOURS,
    CardFacts,
    WarmupPlan,
    bucket_for_card,
    bucket_sort_key,
    due_at_for_level,
    estimated_minutes,
    interval_days_for_level,
    is_due,
    is_streak_at_risk,
    next_repetition_level,
    repetition_level_for_interval,
    retrievability,
)

__all__ = [
    "BUCKETS",
    "BUCKET_CRITICAL",
    "BUCKET_MIDTERM",
    "BUCKET_YESTERDAY",
    "BUCKET_LABELS",
    "CRITICAL_RETENTION",
    "EBBINGHAUS_INTERVAL_DAYS",
    "RETENTION_THRESHOLD",
    "WARMUP_XP_MULTIPLIER",
    "YESTERDAY_WINDOW_HOURS",
    "CardFacts",
    "WarmupPlan",
    "bucket_for_card",
    "bucket_sort_key",
    "due_at_for_level",
    "estimated_minutes",
    "interval_days_for_level",
    "is_due",
    "is_streak_at_risk",
    "next_repetition_level",
    "repetition_level_for_interval",
    "retrievability",
]
