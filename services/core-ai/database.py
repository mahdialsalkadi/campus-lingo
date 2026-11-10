"""Async SQLAlchemy layer for CampusLingo.

Owns the engine, the ORM models mirroring packages/database/schema.sql,
and an idempotent ``init_db()`` that applies the DDL at startup so the
service also works against a database that was never bootstrapped by the
postgres container entrypoint.
"""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

logger = logging.getLogger("campuslingo.database")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:password123@localhost:5432/campuslingo",
)

# --- FSRS card states -------------------------------------------------------
STATE_NEW = 1
STATE_LEARNING = 2
STATE_REVIEW = 3
STATE_RELEARNING = 4

engine = create_async_engine(
    DATABASE_URL,
    echo=bool(os.getenv("SQL_ECHO")),
    pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
    max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "20")),
    pool_pre_ping=True,
    pool_recycle=1800,
)

SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# ORM models
# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    # scrypt hash ("scrypt$N$r$p$salt_hex$hash_hex"), NULL for legacy
    # passwordless accounts created before password auth existed.
    password_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    streak_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_active_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    xp_points: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    gems: Mapped[int] = mapped_column(Integer, nullable=False, default=500)
    league_tier: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    # --- gamified economy (see /api/shop + /api/leaderboards) ---------------
    # XP earned since the current weekly league cycle started; reset every
    # Sunday 00:00 by the rollover sweep in main.py.
    weekly_xp: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Streak Freeze (200 gems): survives a single missed day. Consumed by
    # ``_apply_streak`` when a gap of >1 day would otherwise reset the streak.
    streak_freeze_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # Purchased items: {"streak_freeze": 2, "golden_pass": 1, ...}
    inventory: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    # --- Google-Classroom-style roles & monetization -------------------------
    # 'student' | 'instructor'. Instructors own classrooms and see the roster;
    # every account can still be a student elsewhere.
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="student")
    # $1 Pro Pass: zero ads, unlimited uploads, offline cheatsheets.
    is_pro: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pro_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Creator royalty stash (see CREATOR_ROYALTY_GEMS): gems credited whenever
    # a classmate clears a challenge in one of this user's shared courses.
    creator_gems: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ``courses`` is the OWNED material. ``instructor_id`` on courses adds a
    # second FK path back to users (taught classrooms), so the owner link must
    # be pinned explicitly or SQLAlchemy cannot pick a join condition.
    courses: Mapped[list["Course"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="Course.user_id",
    )

    @property
    def pro_active(self) -> bool:
        """True while the Pro Pass is unexpired."""
        if not self.is_pro:
            return False
        if self.pro_expires_at is None:
            return True
        expires = self.pro_expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires > utcnow()


class AppMeta(Base):
    """Tiny key/value table for scheduler markers (weekly league rollover)."""

    __tablename__ = "app_meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    lms_type: Mapped[str] = mapped_column(Text, nullable=False, default="manual")
    lms_course_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sync_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 6-char share code (stored with the CL- prefix, e.g. "CL-4F8A") for the
    # course sharing system; NULL until the owner first shares the course.
    share_code: Mapped[Optional[str]] = mapped_column(
        String(8), unique=True, nullable=True, index=True
    )
    # Discovery / marketplace metadata: which courses appear in the public
    # catalog and how they are browsable (category, institution, major).
    is_public: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    category: Mapped[str] = mapped_column(
        String(50), nullable=False, default="academic"
    )  # academic / language / general
    institution: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, index=True
    )  # e.g. "UTM"
    major: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, index=True
    )  # e.g. "AI"
    downloads_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )  # popularity counter for cloned/imported courses
    # --- Google Classroom mode ----------------------------------------------
    # A classroom is an instructor-owned course students enroll into with a
    # 6-character code; lectures uploaded by the instructor are pushed to every
    # enrolled student's dashboard in real time.
    is_classroom: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    enrollment_code: Mapped[Optional[str]] = mapped_column(
        String(6), unique=True, nullable=True, index=True
    )
    instructor_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Classroom metadata (section/term + free-form description) shown on the
    # instructor dashboard and the /join landing page.
    section: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped[User] = relationship(
        back_populates="courses", foreign_keys=[user_id]
    )
    instructor: Mapped[Optional[User]] = relationship(
        foreign_keys=[instructor_id], viewonly=True
    )
    lectures: Mapped[list["Lecture"]] = relationship(
        back_populates="course", cascade="all, delete-orphan"
    )
    enrollments: Mapped[list["Enrollment"]] = relationship(
        back_populates="course", cascade="all, delete-orphan"
    )


class Enrollment(Base):
    """A student's membership in an instructor classroom.

    The roster, per-student analytics and the grade export all read from here,
    so a student leaving a class never touches the instructor's own materials.
    """

    __tablename__ = "enrollments"
    __table_args__ = (
        UniqueConstraint("course_id", "student_id", name="enrollments_course_student_key"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    course_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), nullable=False
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    course: Mapped[Course] = relationship(back_populates="enrollments")


class Notification(Base):
    """In-app inbox row; the same payload is what Web Push tries to deliver."""

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    course_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), nullable=True
    )
    # new_lecture | streak_saver | fsrs_decay | classroom | creator_royalty
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="classroom")
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # "sent" (Web Push accepted) | "recorded" (in-app only) | "failed: ..."
    push_status: Mapped[str] = mapped_column(String(64), nullable=False, default="recorded")
    read_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PushSubscription(Base):
    """A browser/PWA Web Push endpoint. Delivery needs `pywebpush` + VAPID keys."""

    __tablename__ = "push_subscriptions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    endpoint: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    keys: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    user_agent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExamSession(Base):
    """A diagnostic timed mock exam built by /api/courses/{id}/exam-crunch."""

    __tablename__ = "exam_sessions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=2700)
    # [{challenge_id, node_id, node_title, chapter, type, prompt, options,
    #   answer_index, explanation}] — assembled server-side, graded server-side.
    questions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    # {challenge_id: selected_index}
    answers: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    cheatsheet: Mapped[str] = mapped_column(Text, nullable=False, default="")
    score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    grade: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)
    readiness: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Lecture(Base):
    __tablename__ = "lectures"

    id: Mapped[uuid.UUID] = _uuid_pk()
    course_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("courses.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    total_chunks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_file: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    course: Mapped[Course] = relationship(back_populates="lectures")
    nodes: Mapped[list["LearningNode"]] = relationship(
        back_populates="lecture", cascade="all, delete-orphan"
    )


class LearningNode(Base):
    __tablename__ = "learning_nodes"

    id: Mapped[uuid.UUID] = _uuid_pk()
    lecture_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("lectures.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    content_summary: Mapped[str] = mapped_column(Text, nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    lecture: Mapped[Lecture] = relationship(back_populates="nodes")
    challenges: Mapped[list["Challenge"]] = relationship(
        back_populates="node", cascade="all, delete-orphan"
    )


class Challenge(Base):
    __tablename__ = "challenges"

    id: Mapped[uuid.UUID] = _uuid_pk()
    node_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("learning_nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    difficulty: Mapped[float] = mapped_column(Float, nullable=False, default=0.3)
    # Extracted slide assets referenced by the payload, e.g.
    # {"diagram": "static/uploads/diagrams/<course>/page-3-1.png"}. Kept
    # separate from payload_json so a client can preload media cheaply.
    assets: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    node: Mapped[LearningNode] = relationship(back_populates="challenges")


class FsrsCard(Base):
    __tablename__ = "fsrs_cards"
    __table_args__ = (UniqueConstraint("user_id", "challenge_id", name="fsrs_cards_user_id_challenge_id_key"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    challenge_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("challenges.id", ondelete="CASCADE"),
        nullable=False,
    )
    state: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=STATE_NEW)
    stability: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    difficulty: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    due_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_review: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lapses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_rating: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    # --- Ebbinghaus forgetting-curve fields (Daily Memory Warm-up) ----------
    # Integer rung on the 1/3/7/14/30-day ladder: 1=Day 1, 2=Day 3, 3=Day 7,
    # 4=Day 14, 5=Day 30. 0 = not yet consolidated (learning steps).
    repetition_level: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    # Estimated recall probability 0.0..1.0, persisted on every review write-back
    # and re-derived on read when the row predates the column.
    retrievability_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a transactional session."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# High-level persistence helpers
# ---------------------------------------------------------------------------


def _as_bundle(bundle: Any) -> Any:
    """Accept a validated LearningUnitBundle or its raw dict form."""
    from schema import LearningUnitBundle  # local import keeps the ORM module dependency-light

    if isinstance(bundle, LearningUnitBundle):
        return bundle
    return LearningUnitBundle.model_validate(bundle)


def _unit_difficulty(difficulty_level: int) -> float:
    """Map the bundle's 1..5 pedagogical level onto the 0..1 challenge weight."""
    level = max(1, min(5, int(difficulty_level)))
    return round(0.15 + (level - 1) * 0.175, 3)  # 0.15, 0.325, 0.5, 0.675, 0.85


def node_title_with_number(base_title: str, lecture_title: str, order_index: int) -> str:
    """Predictable sub-topic name: "Part 2: Paging Mechanics", never "Node 2".

    Every node inside a lecture folder is labelled ``Part {i}: {Core Concept}``
    where ``i`` is the node's 1-based position in the lecture and ``{Core
    Concept}`` is the chunk-derived topic heading. Unusable headings (empty,
    generic placeholders like "Part 1"/"Untitled", or a duplicated
    lecture-title prefix) are replaced with the lecture title so the name never
    comes out truncated or generic.
    """
    import re

    base = (base_title or "").strip()
    lecture_clean = (lecture_title or "").strip()
    # Strip a duplicated lecture-title prefix so nodes don't read
    # "X - Part 1 X - Part 1" or "Chapter 3: X Part 1: Y".
    if lecture_clean and base.lower().startswith(lecture_clean.lower()[:40]):
        base = base[len(lecture_clean):].lstrip(" -–—:").strip()
    # Drop a platform marker the parser/model already added ("Part 1:",
    # "Chapter 3 -"): the single prefix below is the only numbering.
    base = re.sub(
        r"(?i)^\s*(?:part|chapter|section|lecture|unit|module|topic)\s*\d*\s*[:.\-–—]\s*",
        "",
        base,
    )
    # Raw filenames and trailing path/bracket noise never belong in a title.
    base = re.sub(r"\.pdf$", "", base, flags=re.IGNORECASE)
    base = base.strip(" -–—:/\\|[]{}().")
    base = re.sub(r"\s+", " ", base).strip()
    # A heading that is just a part marker is generic — drop it.
    if re.fullmatch(
        r"(?i)(?:part|chapter|section|lecture|unit|module|topic|node|slide|untitled)"
        r"\s*\d*\s*[:.\-–—]?\s*",
        base or "x",
    ):
        base = ""
    if not base:
        # Fall back to the lecture title minus its own chapter marker, so the
        # result reads "Part 1: Intro", not "Part 1: Chapter 1: Intro".
        base = re.sub(
            r"(?i)^\s*\d*[.)]?\s*(?:chapter|lecture|section|unit|module|part|topic)"
            r"\s*\d*\s*[:.\-–—]\s*",
            "",
            lecture_clean,
        ).strip(" -–—:")
        base = base or lecture_clean or f"Topic {order_index + 1}"
    # Safety net: a table header, figure caption, raw number or graph axis is
    # never a node title. Replace it with a meaningful, context-derived topic
    # (e.g. "Empirical Evaluation") instead of persisting nonsense.
    from ingestion.parser import sanitize_concept_title  # local: keeps ORM import-light

    base = sanitize_concept_title(base, context=lecture_clean, fallback=lecture_clean)
    return f"Part {order_index + 1}: {base}"[:200]


async def find_replaceable_lecture(
    course_id: uuid.UUID | str,
    *,
    source_file: Optional[str] = None,
    title: Optional[str] = None,
    session: AsyncSession,
) -> Optional[Lecture]:
    """The existing lecture a re-upload should replace (same file OR title).

    Deduplication guard: uploading the same PDF twice must not create two
    parallel chapter folders. Matches case-insensitively on the original file
    name or the derived chapter title within the course, and returns the most
    recent match — or ``None`` when the upload is genuinely new.
    """
    course_uuid = uuid.UUID(str(course_id))
    clauses = []
    if source_file and source_file.strip():
        clauses.append(func.lower(Lecture.source_file) == source_file.strip().lower())
    if title and title.strip():
        clauses.append(func.lower(Lecture.title) == title.strip().lower())
    if not clauses:
        return None
    return (
        await session.execute(
            select(Lecture)
            .where(Lecture.course_id == course_uuid, or_(*clauses))
            .order_by(Lecture.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def lecture_snapshot(session: AsyncSession, lecture: Lecture) -> dict:
    """Read-only snapshot of an existing lecture (nodes + challenge counts).

    Lets a duplicate upload short-circuit: the caller can return the already
    generated lecture instead of re-running inference, without touching any
    row.
    """
    nodes = list(
        (
            await session.execute(
                select(LearningNode)
                .where(LearningNode.lecture_id == lecture.id)
                .order_by(LearningNode.order_index)
            )
        ).scalars().all()
    )
    units: list[dict] = []
    by_type: dict[str, int] = {}
    cards_seeded = 0
    for node in nodes:
        challenges = list(
            (
                await session.execute(
                    select(Challenge).where(Challenge.node_id == node.id)
                )
            ).scalars().all()
        )
        for challenge in challenges:
            by_type[challenge.type] = by_type.get(challenge.type, 0) + 1
            cards_seeded += 1
        units.append(
            {
                "chunk_index": node.order_index,
                "lecture_id": str(lecture.id),
                "node": {
                    "id": str(node.id),
                    "lecture_id": str(lecture.id),
                    "title": node.title,
                    "content_summary": node.content_summary,
                    "order_index": node.order_index,
                },
                "challenges": len(challenges),
            }
        )
    return {
        "lecture": {
            "id": str(lecture.id),
            "course_id": str(lecture.course_id),
            "title": lecture.title,
            "status": lecture.status,
            "total_chunks": lecture.total_chunks,
        },
        "units": units,
        "challenges_by_type": by_type,
        "cards_seeded": cards_seeded,
    }


async def delete_lecture_subtree(
    session: AsyncSession, lecture_id: uuid.UUID | str
) -> None:
    """Delete ONE lecture and its descendants — never the parent course.

    Strictly scoped to ``lecture_id``: every FK runs child→parent, so this can
    only cascade downwards (nodes → challenges → fsrs_cards). Sibling lectures
    and the parent ``Course`` row are unreachable from here.
    """
    lecture_uuid = uuid.UUID(str(lecture_id))
    node_ids = list(
        (
            await session.execute(
                select(LearningNode.id).where(LearningNode.lecture_id == lecture_uuid)
            )
        ).scalars().all()
    )
    if node_ids:
        challenge_ids = list(
            (
                await session.execute(
                    select(Challenge.id).where(Challenge.node_id.in_(node_ids))
                )
            ).scalars().all()
        )
        if challenge_ids:
            await session.execute(
                text("DELETE FROM fsrs_cards WHERE challenge_id = ANY(:ids)"),
                {"ids": [str(cid) for cid in challenge_ids]},
            )
        await session.execute(
            text("DELETE FROM challenges WHERE node_id = ANY(:ids)"),
            {"ids": [str(nid) for nid in node_ids]},
        )
    await session.execute(
        text("DELETE FROM learning_nodes WHERE lecture_id = :id"),
        {"id": str(lecture_uuid)},
    )
    await session.execute(
        text("DELETE FROM lectures WHERE id = :id"),
        {"id": str(lecture_uuid)},
    )


async def save_pdf_lecture(
    course_id: uuid.UUID | str,
    lecture_title: str,
    bundles: list[dict],
    *,
    session: Optional[AsyncSession] = None,
    source_file: Optional[str] = None,
    base_titles: Optional[list[str]] = None,
    embeddings: Optional[list[Optional[list[float]]]] = None,
    assets: Optional[dict[str, Any]] = None,
) -> dict:
    """Persist ONE lecture (one PDF) with 2-3 numbered learning nodes.

    Hierarchy contract: one uploaded PDF = one ``Lecture`` row named after the
    file/main title (e.g. "Chapter 3: Memory Management"), containing exactly
    2-3 ``LearningNode`` sub-topics with clean academic names ("Part 1: Paging
    Mechanics", "Part 2: Virtual Memory"), each carrying a content-adaptive set of
    varied challenges and FSRS cards for the course owner.

    ``bundles[i]`` is the validated :class:`LearningUnitBundle` payload for
    chunk ``i`` (empty/None entries are skipped). ``base_titles[i]`` overrides
    the node's base name (the chunk heading); ``embeddings[i]`` is the node's
    optional embedding vector.

    Pass ``session`` to enlist in an existing transaction; otherwise the write
    commits in its own session before returning.
    """
    course_uuid = uuid.UUID(str(course_id))
    valid = [(index, bundle) for index, bundle in enumerate(bundles) if bundle]
    if not valid:
        raise ValueError("no valid bundles to persist")

    if base_titles is None:
        base_titles = [""] * len(bundles)
    if embeddings is None:
        embeddings = [None] * len(bundles)

    async def _run(db: AsyncSession) -> dict:
        course = await db.get(Course, course_uuid)
        if course is None:
            raise ValueError(f"course {course_uuid} not found")

        # The lecture's number within the course (1-based) drives the 3.1/3.2
        # node prefixes.
        lecture_number = (
            await db.scalar(
                select(func.count()).select_from(Lecture).where(Lecture.course_id == course_uuid)
            )
            or 0
        ) + 1

        lecture = Lecture(
            course_id=course_uuid,
            title=lecture_title[:160],
            status="ready",
            total_chunks=len(valid),
            source_file=(source_file or lecture_title)[:400],
        )
        db.add(lecture)
        await db.flush()

        nodes_out: list[dict] = []
        created: list[dict] = []
        for index, bundle in valid:
            unit = _as_bundle(bundle)
            difficulty = _unit_difficulty(unit.difficulty_level)
            node = LearningNode(
                lecture_id=lecture.id,
                title=node_title_with_number(
                    base_titles[index] or unit.concept_title,
                    f"{lecture_number}. {lecture_title}",
                    len(nodes_out),  # consecutive numbering even with skipped chunks
                ),
                content_summary=unit.concept_title[:8000],
                order_index=len(nodes_out),
            )
            db.add(node)
            await db.flush()
            nodes_out.append(
                {
                    "id": str(node.id),
                    "lecture_id": str(lecture.id),
                    "title": node.title,
                    "content_summary": node.content_summary,
                    "order_index": node.order_index,
                    "difficulty_level": unit.difficulty_level,
                }
            )

            embedding = embeddings[index] if index < len(embeddings) else None
            if embedding:
                vector = "[" + ",".join(f"{value:.7f}" for value in embedding) + "]"
                await db.execute(
                    text(
                        "UPDATE learning_nodes SET embedding = CAST(:vec AS vector) WHERE id = :id"
                    ),
                    {"vec": vector, "id": str(node.id)},
                )

            for challenge_type, payload in unit.challenge_payloads():
                challenge = Challenge(
                    node_id=node.id,
                    type=challenge_type,
                    payload_json=payload,
                    difficulty=difficulty,
                    assets=assets or {},
                )
                db.add(challenge)
                await db.flush()
                db.add(
                    FsrsCard(
                        user_id=course.user_id,
                        challenge_id=challenge.id,
                        state=STATE_NEW,
                        stability=0.0,
                        difficulty=difficulty * 9.0 + 1.0,
                        due_date=utcnow(),
                        reps=0,
                        lapses=0,
                    )
                )
                created.append(
                    {
                        "id": str(challenge.id),
                        "node_id": str(node.id),
                        "type": challenge_type,
                        "payload_json": payload,
                        "difficulty": difficulty,
                    }
                )

        return {
            "lecture": {
                "id": str(lecture.id),
                "course_id": str(course_uuid),
                "title": lecture.title,
                "status": lecture.status,
                "total_chunks": lecture.total_chunks,
            },
            "nodes": nodes_out,
            "first_node": nodes_out[0] if nodes_out else {},
            "challenges": created,
            "cards_seeded": len(created),
        }

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)


async def save_lecture_and_challenges(
    course_id: uuid.UUID | str,
    title: str,
    bundle: dict,
    *,
    session: Optional[AsyncSession] = None,
    source_file: Optional[str] = None,
    embedding: Optional[list[float]] = None,
    node_title: Optional[str] = None,
    assets: Optional[dict[str, Any]] = None,
) -> dict:
    """Insert a lecture, its learning node, every challenge and the seed cards.

    ``bundle`` is a :class:`schema.LearningUnitBundle` payload (or the validated
    model itself). Cards are seeded for the course owner, so every freshly
    generated challenge is immediately reviewable.

    Pass ``session`` to enlist in an existing transaction; otherwise the write
    is committed in its own session before returning.
    """
    unit = _as_bundle(bundle)
    course_uuid = uuid.UUID(str(course_id))
    difficulty = _unit_difficulty(unit.difficulty_level)

    async def _run(db: AsyncSession) -> dict:
        course = await db.get(Course, course_uuid)
        if course is None:
            raise ValueError(f"course {course_uuid} not found")

        lecture = Lecture(
            course_id=course_uuid,
            title=title[:160],
            status="ready",
            total_chunks=1,
            source_file=(source_file or title)[:400],
        )
        db.add(lecture)
        await db.flush()

        node = LearningNode(
            lecture_id=lecture.id,
            title=(node_title or unit.concept_title)[:200],
            content_summary=unit.concept_title[:8000],
            order_index=0,
        )
        db.add(node)
        await db.flush()

        if embedding:
            vector = "[" + ",".join(f"{value:.7f}" for value in embedding) + "]"
            await db.execute(
                text(
                    "UPDATE learning_nodes SET embedding = CAST(:vec AS vector) WHERE id = :id"
                ),
                {"vec": vector, "id": str(node.id)},
            )

        created: list[dict] = []
        for challenge_type, payload in unit.challenge_payloads():
            challenge = Challenge(
                node_id=node.id,
                type=challenge_type,
                payload_json=payload,
                difficulty=difficulty,
                assets=assets or {},
            )
            db.add(challenge)
            await db.flush()
            db.add(
                FsrsCard(
                    user_id=course.user_id,
                    challenge_id=challenge.id,
                    state=STATE_NEW,
                    stability=0.0,
                    difficulty=difficulty * 9.0 + 1.0,
                    due_date=utcnow(),
                    reps=0,
                    lapses=0,
                )
            )
            created.append(
                {
                    "id": str(challenge.id),
                    "node_id": str(node.id),
                    "type": challenge_type,
                    "payload_json": payload,
                    "difficulty": difficulty,
                }
            )

        return {
            "lecture": {
                "id": str(lecture.id),
                "course_id": str(course_uuid),
                "title": lecture.title,
                "status": lecture.status,
                "total_chunks": lecture.total_chunks,
            },
            "node": {
                "id": str(node.id),
                "lecture_id": str(lecture.id),
                "title": node.title,
                "content_summary": node.content_summary,
                "order_index": node.order_index,
                "difficulty_level": unit.difficulty_level,
                "embedding_stored": bool(embedding),
            },
            "challenges": created,
            "cards_seeded": len(created),
        }

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)


async def get_due_challenges_for_user(
    user_id: uuid.UUID | str,
    *,
    limit: int = 20,
    session: Optional[AsyncSession] = None,
    only_due: bool = True,
    only_mistakes: bool = False,
) -> list[dict]:
    """Challenges whose FSRS card is due (``due_date <= NOW()``) for one user.

    With ``only_mistakes`` the result is the Mistakes Inbox: cards sitting in
    Learning/Relearning after at least one lapse.
    """
    user_uuid = uuid.UUID(str(user_id))
    now = utcnow()

    statement = (
        select(FsrsCard, Challenge, LearningNode)
        .join(Challenge, FsrsCard.challenge_id == Challenge.id)
        .join(LearningNode, Challenge.node_id == LearningNode.id)
        .where(FsrsCard.user_id == user_uuid)
    )
    if only_due:
        statement = statement.where(FsrsCard.due_date <= now)
    if only_mistakes:
        statement = statement.where(FsrsCard.state.in_((2, 4)), FsrsCard.lapses > 0)
    statement = statement.order_by(FsrsCard.due_date.asc()).limit(max(1, limit))

    async def _run(db: AsyncSession) -> list[dict]:
        rows = (await db.execute(statement)).all()
        items: list[dict] = []
        for card, challenge, node in rows:
            elapsed_days = 0.0
            if card.last_review is not None and card.stability:
                last = card.last_review
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                elapsed_days = max(0.0, (now - last).total_seconds() / 86400.0)
            items.append(
                {
                    "card_id": str(card.id),
                    "challenge_id": str(challenge.id),
                    "node_id": str(node.id),
                    "node_title": node.title,
                    "lecture_id": str(node.lecture_id),
                    "type": challenge.type,
                    "payload_json": challenge.payload_json,
                    "state": card.state,
                    "stability": round(card.stability or 0.0, 4),
                    "difficulty": round(card.difficulty or 0.0, 4),
                    "due_date": card.due_date,
                    "retrievability": (
                        round(_retrievability(card.stability, elapsed_days), 6)
                        if card.last_review is not None and card.stability
                        else None
                    ),
                    "is_mistake": card.state in (2, 4),
                    "lapses": card.lapses or 0,
                }
            )
        return items

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)


def _retrievability(stability: float, elapsed_days: float) -> float:
    """FSRS R(t, S) — duplicated locally to keep the ORM layer import-free."""
    import math

    if not stability or stability <= 0:
        return 0.0
    if elapsed_days <= 0:
        return 1.0
    return min(1.0, max(0.0, (1.0 + (19.0 / 81.0) * elapsed_days / stability) ** -0.5))


async def record_card_review(
    user_id: uuid.UUID | str,
    challenge_id: uuid.UUID | str,
    rating: int,
    new_s: float,
    new_d: float,
    next_due: datetime,
    *,
    session: Optional[AsyncSession] = None,
    ebbinghaus: Optional[dict] = None,
) -> dict:
    """Write back the DSR attributes produced by :class:`FSRSScheduler`.

    Creates the card on first contact. ``state`` is derived from the rating and
    the previous state so ``/api/reviews`` can stay a thin orchestrator.

    ``ebbinghaus`` optionally carries the forgetting-curve write-back produced
    by :func:`models.next_card_state` (``repetition_level``,
    ``retrievability_score``); when omitted the fields are derived inline so
    legacy callers stay correct.
    """
    if int(rating) not in (1, 2, 3, 4):
        raise ValueError("rating must be 1..4")
    user_uuid = uuid.UUID(str(user_id))
    challenge_uuid = uuid.UUID(str(challenge_id))
    now = utcnow()
    due = next_due if next_due.tzinfo else next_due.replace(tzinfo=timezone.utc)

    async def _run(db: AsyncSession) -> dict:
        card = (
            await db.execute(
                select(FsrsCard).where(
                    FsrsCard.user_id == user_uuid,
                    FsrsCard.challenge_id == challenge_uuid,
                )
            )
        ).scalar_one_or_none()

        if card is None:
            challenge = await db.get(Challenge, challenge_uuid)
            if challenge is None:
                raise ValueError(f"challenge {challenge_uuid} not found")
            card = FsrsCard(
                user_id=user_uuid,
                challenge_id=challenge_uuid,
                state=STATE_NEW,
                stability=0.0,
                difficulty=challenge.difficulty * 9.0 + 1.0,
                due_date=now,
            )
            db.add(card)
            await db.flush()

        previous_state = card.state
        if int(rating) == 1:
            card.state = STATE_RELEARNING
            card.lapses = (card.lapses or 0) + 1
        elif previous_state in (1, 2, 4):
            card.state = STATE_REVIEW
            card.lapses = card.lapses or 0
        else:
            card.state = STATE_REVIEW
            card.lapses = card.lapses or 0

        card.stability = float(new_s)
        card.difficulty = float(new_d)
        card.due_date = due
        card.last_review = now
        card.reps = (card.reps or 0) + 1
        card.last_rating = int(rating)

        # --- Ebbinghaus forgetting-curve write-back ----------------------
        if ebbinghaus:
            card.repetition_level = int(ebbinghaus.get("repetition_level", 0) or 0)
            card.retrievability_score = float(
                ebbinghaus.get("retrievability_score", 0.0) or 0.0
            )
        else:
            from models import next_card_state as _next_card_state  # local: avoids a cycle

            derived = _next_card_state(
                card,
                rating=int(rating),
                new_stability=float(new_s),
                new_difficulty=float(new_d),
                next_due=due,
                now=now,
            )
            card.repetition_level = int(derived["repetition_level"])
            card.retrievability_score = float(derived["retrievability_score"])

        return {
            "card_id": str(card.id),
            "user_id": str(user_uuid),
            "challenge_id": str(challenge_uuid),
            "state": card.state,
            "stability": round(card.stability, 6),
            "difficulty": round(card.difficulty, 6),
            "due_date": card.due_date,
            "last_review": card.last_review,
            "reps": card.reps,
            "lapses": card.lapses,
            "last_rating": card.last_rating,
            "repetition_level": card.repetition_level,
            "retrievability_score": round(card.retrievability_score, 6),
        }

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)


# ---------------------------------------------------------------------------
# Classrooms, notifications and creator royalties
# ---------------------------------------------------------------------------

ENROLLMENT_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I/O/0/1
ENROLLMENT_CODE_LENGTH = 6


def generate_enrollment_code() -> str:
    """6-char class code (e.g. ``CS301X``) from an unambiguous alphabet."""
    import secrets as _secrets

    return "".join(
        _secrets.choice(ENROLLMENT_CODE_ALPHABET)
        for _ in range(ENROLLMENT_CODE_LENGTH)
    )


async def unique_enrollment_code(session: AsyncSession) -> str:
    """Allocate a class code no other classroom holds."""
    for _ in range(25):
        code = generate_enrollment_code()
        taken = await session.scalar(
            select(Course.id).where(Course.enrollment_code == code).limit(1)
        )
        if taken is None:
            return code
    raise RuntimeError("could not allocate a unique enrollment code")


async def enrollment_for(
    session: AsyncSession, course_id: uuid.UUID, student_id: uuid.UUID
) -> Optional[Enrollment]:
    return (
        await session.execute(
            select(Enrollment).where(
                Enrollment.course_id == course_id,
                Enrollment.student_id == student_id,
            )
        )
    ).scalar_one_or_none()


async def enrolled_student_ids(
    session: AsyncSession, course_id: uuid.UUID
) -> list[uuid.UUID]:
    rows = (
        await session.execute(
            select(Enrollment.student_id)
            .where(Enrollment.course_id == course_id)
            .order_by(Enrollment.created_at)
        )
    ).scalars().all()
    return list(rows)


async def courses_for_student(
    session: AsyncSession, student_id: uuid.UUID
) -> list[Course]:
    """Classrooms this student is enrolled in (newest first)."""
    return list(
        (
            await session.execute(
                select(Course)
                .join(Enrollment, Enrollment.course_id == Course.id)
                .where(Enrollment.student_id == student_id)
                .order_by(Course.created_at.desc())
            )
        )
        .scalars()
        .all()
    )


async def classrooms_for_instructor(
    session: AsyncSession, instructor_id: uuid.UUID
) -> list[Course]:
    return list(
        (
            await session.execute(
                select(Course)
                .where(Course.instructor_id == instructor_id)
                .order_by(Course.created_at.desc())
            )
        )
        .scalars()
        .all()
    )


async def clone_course_tree(
    session: AsyncSession,
    source: Course,
    owner_id: uuid.UUID,
    *,
    name: Optional[str] = None,
    seed_cards: bool = True,
) -> dict:
    """Deep-clone a course (lectures/nodes/challenges) without any LLM call.

    Used by the peer-to-peer ``/join/{share_code}`` flow: a shared peer course is
    copied into the joiner's account so no inference is ever re-triggered.
    Fresh FSRS cards are seeded for the new owner when ``seed_cards``.
    """
    new_course = Course(
        user_id=owner_id,
        name=(name or source.name)[:200],
        lms_type=source.lms_type,
        category=source.category,
        institution=source.institution,
        major=source.major,
    )
    session.add(new_course)
    await session.flush()

    lectures = (
        await session.execute(
            select(Lecture).where(Lecture.course_id == source.id).order_by(Lecture.created_at)
        )
    ).scalars().all()
    lecture_map: dict[uuid.UUID, Lecture] = {}
    for lecture in lectures:
        clone = Lecture(
            course_id=new_course.id,
            title=lecture.title,
            status=lecture.status,
            total_chunks=lecture.total_chunks,
            source_file=lecture.source_file,
        )
        session.add(clone)
        lecture_map[lecture.id] = clone
    await session.flush()

    nodes = (
        await session.execute(
            select(LearningNode)
            .where(LearningNode.lecture_id.in_(list(lecture_map) or [uuid.uuid4()]))
            .order_by(LearningNode.order_index)
        )
    ).scalars().all()
    node_map: dict[uuid.UUID, LearningNode] = {}
    for node in nodes:
        clone = LearningNode(
            lecture_id=lecture_map[node.lecture_id].id,
            title=node.title,
            content_summary=node.content_summary,
            order_index=node.order_index,
        )
        session.add(clone)
        node_map[node.id] = clone
    await session.flush()

    challenges = (
        await session.execute(
            select(Challenge).where(Challenge.node_id.in_(list(node_map) or [uuid.uuid4()]))
        )
    ).scalars().all()
    cloned_challenges = 0
    for challenge in challenges:
        clone = Challenge(
            node_id=node_map[challenge.node_id].id,
            type=challenge.type,
            payload_json=challenge.payload_json,
            difficulty=challenge.difficulty,
            assets=challenge.assets or {},
        )
        session.add(clone)
        await session.flush()
        cloned_challenges += 1
        if seed_cards:
            session.add(
                FsrsCard(
                    user_id=owner_id,
                    challenge_id=clone.id,
                    state=STATE_NEW,
                    stability=0.0,
                    difficulty=challenge.difficulty * 9.0 + 1.0,
                    due_date=utcnow(),
                    reps=0,
                    lapses=0,
                )
            )
    return {
        "course_id": str(new_course.id),
        "lectures_cloned": len(lecture_map),
        "nodes_cloned": len(node_map),
        "challenges_cloned": cloned_challenges,
        "cards_seeded": cloned_challenges if seed_cards else 0,
    }


async def course_owner_for_challenge(
    session: AsyncSession, challenge_id: uuid.UUID
) -> Optional[uuid.UUID]:
    """Walk challenge -> node -> lecture -> course and return the course owner.

    Used for creator royalties: when somebody else clears a challenge in a
    shared peer course, the owner is credited.
    """
    return await session.scalar(
        select(Course.user_id)
        .join(Lecture, Lecture.course_id == Course.id)
        .join(LearningNode, LearningNode.lecture_id == Lecture.id)
        .join(Challenge, Challenge.node_id == LearningNode.id)
        .where(Challenge.id == challenge_id)
        .limit(1)
    )


async def course_owner_and_title(
    session: AsyncSession, course_id: uuid.UUID
) -> tuple[Optional[uuid.UUID], Optional[str]]:
    row = (
        await session.execute(
            select(Course.user_id, Course.name).where(Course.id == course_id).limit(1)
        )
    ).first()
    return (row[0], row[1]) if row else (None, None)


# ---------------------------------------------------------------------------
# DDL bootstrap (idempotent)
# ---------------------------------------------------------------------------

def ddl_statements(uuid_default: str = "uuid_generate_v4()") -> tuple[str, ...]:
    """Idempotent table/index DDL, parameterised by the available UUID function."""
    return (
    f"""
    CREATE TABLE IF NOT EXISTS users (
        id UUID PRIMARY KEY DEFAULT {uuid_default},
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT,
        streak_count INT NOT NULL DEFAULT 0,
        last_active_at TIMESTAMPTZ,
        xp_points BIGINT NOT NULL DEFAULT 0,
        gems INT NOT NULL DEFAULT 500,
        league_tier SMALLINT NOT NULL DEFAULT 0,
        weekly_xp INT NOT NULL DEFAULT 0,
        streak_freeze_active BOOLEAN NOT NULL DEFAULT FALSE,
        inventory JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        role VARCHAR(16) NOT NULL DEFAULT 'student',
        is_pro BOOLEAN NOT NULL DEFAULT FALSE,
        pro_expires_at TIMESTAMPTZ,
        creator_gems INT NOT NULL DEFAULT 0
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS app_meta (
        key VARCHAR(64) PRIMARY KEY,
        value TEXT NOT NULL DEFAULT ''
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS courses (
        id UUID PRIMARY KEY DEFAULT {uuid_default},
        user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        lms_type TEXT NOT NULL DEFAULT 'manual',
        lms_course_id TEXT,
        sync_token TEXT,
        is_public BOOLEAN NOT NULL DEFAULT FALSE,
        category VARCHAR(50) NOT NULL DEFAULT 'academic',
        institution VARCHAR(100),
        major VARCHAR(100),
        downloads_count INT NOT NULL DEFAULT 0,
        is_classroom BOOLEAN NOT NULL DEFAULT FALSE,
        enrollment_code VARCHAR(6),
        instructor_id UUID REFERENCES users(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS enrollments (
        id UUID PRIMARY KEY DEFAULT {uuid_default},
        course_id UUID NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
        student_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (course_id, student_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS notifications (
        id UUID PRIMARY KEY DEFAULT {uuid_default},
        user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        course_id UUID REFERENCES courses(id) ON DELETE CASCADE,
        kind VARCHAR(32) NOT NULL DEFAULT 'classroom',
        title TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '',
        url TEXT,
        push_status VARCHAR(64) NOT NULL DEFAULT 'recorded',
        read_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS push_subscriptions (
        id UUID PRIMARY KEY DEFAULT {uuid_default},
        user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        endpoint TEXT UNIQUE NOT NULL,
        keys JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        user_agent TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS exam_sessions (
        id UUID PRIMARY KEY DEFAULT {uuid_default},
        user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        course_id UUID NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
        status VARCHAR(16) NOT NULL DEFAULT 'active',
        duration_seconds INT NOT NULL DEFAULT 2700,
        questions JSONB NOT NULL DEFAULT '[]'::jsonb,
        answers JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        cheatsheet TEXT NOT NULL DEFAULT '',
        score INT,
        total INT NOT NULL DEFAULT 0,
        grade VARCHAR(2),
        readiness REAL,
        started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        finished_at TIMESTAMPTZ
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS lectures (
        id UUID PRIMARY KEY DEFAULT {uuid_default},
        course_id UUID NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        total_chunks INT NOT NULL DEFAULT 0,
        source_file TEXT,
        error_message TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS learning_nodes (
        id UUID PRIMARY KEY DEFAULT {uuid_default},
        lecture_id UUID NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        content_summary TEXT NOT NULL,
        order_index INT NOT NULL DEFAULT 0,
        embedding vector(1536),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS challenges (
        id UUID PRIMARY KEY DEFAULT {uuid_default},
        node_id UUID NOT NULL REFERENCES learning_nodes(id) ON DELETE CASCADE,
        type TEXT NOT NULL,
        payload_json JSONB NOT NULL,
        difficulty REAL NOT NULL DEFAULT 0.3,
        assets JSONB NOT NULL DEFAULT '{{}}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT challenges_type_check CHECK (
            type IN (
                'MATCH_MADNESS', 'SPOT_MISTAKE', 'CONTEXTUAL_CLOZE', 'INTERACTIVE_STORY',
                'MCQ_QUIZ', 'TRUE_FALSE', 'DIAGRAM_QA', 'SWIPE_BOOLEAN', 'SPOT_IMPOSTER',
                'TAP_TO_FILL', 'BOSS_FIGHT'
            )
        )
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS fsrs_cards (
        id UUID PRIMARY KEY DEFAULT {uuid_default},
        user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        challenge_id UUID NOT NULL REFERENCES challenges(id) ON DELETE CASCADE,
        state SMALLINT NOT NULL DEFAULT 1,
        stability REAL NOT NULL DEFAULT 0,
        difficulty REAL NOT NULL DEFAULT 0,
        due_date TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_review TIMESTAMPTZ,
        reps INT NOT NULL DEFAULT 0,
        lapses INT NOT NULL DEFAULT 0,
        last_rating SMALLINT,
        repetition_level SMALLINT NOT NULL DEFAULT 0,
        retrievability_score REAL NOT NULL DEFAULT 0,
        UNIQUE (user_id, challenge_id)
    )
    """,
    # NOTE: indexes on columns that only exist via COLUMN_MIGRATIONS (e.g.
    # idx_courses_public on is_public) live in COLUMN_MIGRATIONS and must stay
    # there: init_db() runs this tuple first, and on a pre-existing database
    # the ADD COLUMN has not run yet — an index here would abort startup with
    # UndefinedColumnError (as happened on 2026-09-18).
    # MCQ + True/False challenge types: pre-existing databases carry the old
    # 4-type CHECK constraint, which CREATE TABLE IF NOT EXISTS never rewrites.
    "ALTER TABLE challenges DROP CONSTRAINT IF EXISTS challenges_type_check",
    """
    ALTER TABLE challenges ADD CONSTRAINT challenges_type_check CHECK (
        type IN (
            'MATCH_MADNESS', 'SPOT_MISTAKE', 'CONTEXTUAL_CLOZE', 'INTERACTIVE_STORY',
            'MCQ_QUIZ', 'TRUE_FALSE', 'DIAGRAM_QA', 'SWIPE_BOOLEAN', 'SPOT_IMPOSTER',
            'TAP_TO_FILL', 'BOSS_FIGHT'
        )
    )
    """,
    # Classroom metadata columns (section/term + description) added after the
    # first schema revision; pre-existing databases get them via migration.
    "ALTER TABLE courses ADD COLUMN IF NOT EXISTS section VARCHAR(120)",
    "ALTER TABLE courses ADD COLUMN IF NOT EXISTS description TEXT",
    "CREATE INDEX IF NOT EXISTS idx_courses_user ON courses (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_enrollments_course ON enrollments (course_id)",
    "CREATE INDEX IF NOT EXISTS idx_enrollments_student ON enrollments (student_id)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications (user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user ON push_subscriptions (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_exam_sessions_user ON exam_sessions (user_id, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_lectures_course ON lectures (course_id)",
    "CREATE INDEX IF NOT EXISTS idx_nodes_lecture ON learning_nodes (lecture_id, order_index)",
    "CREATE INDEX IF NOT EXISTS idx_challenges_node ON challenges (node_id, type)",
    "CREATE INDEX IF NOT EXISTS idx_fsrs_sched ON fsrs_cards (user_id, due_date)",
)

# Columns added after the first schema revision; applied when missing.
COLUMN_MIGRATIONS: tuple[str, ...] = (
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT",
    "ALTER TABLE lectures ADD COLUMN IF NOT EXISTS source_file TEXT",
    "ALTER TABLE lectures ADD COLUMN IF NOT EXISTS error_message TEXT",
    "ALTER TABLE challenges ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    "ALTER TABLE fsrs_cards ADD COLUMN IF NOT EXISTS reps INT NOT NULL DEFAULT 0",
    "ALTER TABLE fsrs_cards ADD COLUMN IF NOT EXISTS lapses INT NOT NULL DEFAULT 0",
    "ALTER TABLE fsrs_cards ADD COLUMN IF NOT EXISTS last_rating SMALLINT",
    # Ebbinghaus forgetting-curve fields (Daily Memory Warm-up ritual).
    "ALTER TABLE fsrs_cards ADD COLUMN IF NOT EXISTS repetition_level SMALLINT NOT NULL DEFAULT 0",
    "ALTER TABLE fsrs_cards ADD COLUMN IF NOT EXISTS retrievability_score REAL NOT NULL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS idx_fsrs_retrievability ON fsrs_cards (user_id, retrievability_score)",
    # Course sharing: nullable unique share code (e.g. "CL-4F8A").
    "ALTER TABLE courses ADD COLUMN IF NOT EXISTS share_code VARCHAR(8)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_courses_share_code ON courses (share_code)",
    # Course discovery / marketplace fields.
    "ALTER TABLE courses ADD COLUMN IF NOT EXISTS is_public BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE courses ADD COLUMN IF NOT EXISTS category VARCHAR(50) NOT NULL DEFAULT 'academic'",
    "ALTER TABLE courses ADD COLUMN IF NOT EXISTS institution VARCHAR(100)",
    "ALTER TABLE courses ADD COLUMN IF NOT EXISTS major VARCHAR(100)",
    "ALTER TABLE courses ADD COLUMN IF NOT EXISTS downloads_count INT NOT NULL DEFAULT 0",
    # Gamified economy: weekly league XP, streak-freeze shield, item inventory.
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS weekly_xp INT NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS streak_freeze_active BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS inventory JSONB NOT NULL DEFAULT '{}'::jsonb",
    "CREATE INDEX IF NOT EXISTS idx_users_weekly_xp ON users (weekly_xp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_courses_public ON courses (is_public)",
    "CREATE INDEX IF NOT EXISTS idx_courses_institution ON courses (institution)",
    "CREATE INDEX IF NOT EXISTS idx_courses_major ON courses (major)",
    # Google Classroom mode: instructor-owned courses + 6-char enrollment code.
    "ALTER TABLE courses ADD COLUMN IF NOT EXISTS is_classroom BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE courses ADD COLUMN IF NOT EXISTS enrollment_code VARCHAR(6)",
    "ALTER TABLE courses ADD COLUMN IF NOT EXISTS instructor_id UUID REFERENCES users(id) ON DELETE SET NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_courses_enrollment_code ON courses (enrollment_code)",
    "CREATE INDEX IF NOT EXISTS idx_courses_classroom ON courses (is_classroom)",
    "CREATE INDEX IF NOT EXISTS idx_courses_instructor ON courses (instructor_id)",
    # Roles + monetization: instructor student, $1 Pro Pass, royalty stash.
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(16) NOT NULL DEFAULT 'student'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_pro BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS pro_expires_at TIMESTAMPTZ",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS creator_gems INT NOT NULL DEFAULT 0",
    # Slide assets extracted from PDFs (DIAGRAM_QA lives off this column).
    "ALTER TABLE challenges ADD COLUMN IF NOT EXISTS assets JSONB NOT NULL DEFAULT '{}'::jsonb",
)


async def _try_extension(conn, statement: str) -> bool:
    """Attempt a CREATE EXTENSION inside a SAVEPOINT.

    Without the savepoint a missing extension aborts the surrounding
    transaction and every later statement fails with InFailedSQLTransaction.
    """
    try:
        async with conn.begin_nested():
            await conn.execute(text(statement))
        return True
    except Exception:  # noqa: BLE001 - availability probe
        return False


async def _resolve_uuid_default(conn) -> str:
    """Prefer uuid-ossp (schema.sql default); fall back to core gen_random_uuid()."""
    if await _try_extension(conn, 'CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'):
        return "uuid_generate_v4()"
    logger.warning(
        "uuid-ossp unavailable; using built-in gen_random_uuid() for column defaults"
    )
    return "gen_random_uuid()"


async def _require_vector(conn) -> None:
    """pgvector is not optional: learning_nodes.embedding depends on it."""
    if not await _try_extension(conn, 'CREATE EXTENSION IF NOT EXISTS "vector"'):
        raise RuntimeError(
            "pgvector is not installed on this PostgreSQL server. Use the "
            "pgvector/pgvector:pg16 image, or install the extension, then restart."
        )


async def init_db() -> None:
    """Create extensions, tables, indexes and missing columns. Safe to re-run."""
    async with engine.begin() as conn:
        uuid_default = await _resolve_uuid_default(conn)
        await _require_vector(conn)
        for stmt in ddl_statements(uuid_default):
            await conn.execute(text(stmt))
        for stmt in COLUMN_MIGRATIONS:
            await conn.execute(text(stmt))
        # ANN index is optional: it needs a non-empty table to be cheap, and
        # pgvector < 0.5 lacks the build-time guards. Failure is non-fatal.
        try:
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_nodes_embedding ON learning_nodes "
                    "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
                )
            )
        except Exception:  # pragma: no cover - index is a performance nicety
            pass


async def dispose_engine() -> None:
    await engine.dispose()
