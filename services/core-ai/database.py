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
    DateTime,
    Float,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
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
    streak_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_active_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    xp_points: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    gems: Mapped[int] = mapped_column(Integer, nullable=False, default=500)
    league_tier: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    courses: Mapped[list["Course"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="courses")
    lectures: Mapped[list["Lecture"]] = relationship(
        back_populates="course", cascade="all, delete-orphan"
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


async def save_lecture_and_challenges(
    course_id: uuid.UUID | str,
    title: str,
    bundle: dict,
    *,
    session: Optional[AsyncSession] = None,
    source_file: Optional[str] = None,
    embedding: Optional[list[float]] = None,
    node_title: Optional[str] = None,
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
) -> dict:
    """Write back the DSR attributes produced by :class:`FSRSScheduler`.

    Creates the card on first contact. ``state`` is derived from the rating and
    the previous state so ``/api/reviews`` can stay a thin orchestrator.
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
        }

    if session is not None:
        return await _run(session)
    async with session_scope() as db:
        return await _run(db)


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
        streak_count INT NOT NULL DEFAULT 0,
        last_active_at TIMESTAMPTZ,
        xp_points BIGINT NOT NULL DEFAULT 0,
        gems INT NOT NULL DEFAULT 500,
        league_tier SMALLINT NOT NULL DEFAULT 0
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
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
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
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT challenges_type_check CHECK (
            type IN ('MATCH_MADNESS', 'SPOT_MISTAKE', 'CONTEXTUAL_CLOZE', 'INTERACTIVE_STORY')
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
        UNIQUE (user_id, challenge_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_courses_user ON courses (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_lectures_course ON lectures (course_id)",
    "CREATE INDEX IF NOT EXISTS idx_nodes_lecture ON learning_nodes (lecture_id, order_index)",
    "CREATE INDEX IF NOT EXISTS idx_challenges_node ON challenges (node_id, type)",
    "CREATE INDEX IF NOT EXISTS idx_fsrs_sched ON fsrs_cards (user_id, due_date)",
)

# Columns added after the first schema revision; applied when missing.
COLUMN_MIGRATIONS: tuple[str, ...] = (
    "ALTER TABLE lectures ADD COLUMN IF NOT EXISTS source_file TEXT",
    "ALTER TABLE lectures ADD COLUMN IF NOT EXISTS error_message TEXT",
    "ALTER TABLE challenges ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    "ALTER TABLE fsrs_cards ADD COLUMN IF NOT EXISTS reps INT NOT NULL DEFAULT 0",
    "ALTER TABLE fsrs_cards ADD COLUMN IF NOT EXISTS lapses INT NOT NULL DEFAULT 0",
    "ALTER TABLE fsrs_cards ADD COLUMN IF NOT EXISTS last_rating SMALLINT",
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
