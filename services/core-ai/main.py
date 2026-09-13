"""CampusLingo core-ai service.

FastAPI app that turns uploaded lecture PDFs into gamified challenges and
drives the FSRS-4.5 review loop.

Run:
    uvicorn main:app --reload --port 8000 --app-dir services/core-ai
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from database import (
    STATE_NEW,
    Challenge,
    Course,
    FsrsCard,
    LearningNode,
    Lecture,
    User,
    dispose_engine,
    get_due_challenges_for_user,
    get_session,
    init_db,
    record_card_review,
    save_lecture_and_challenges,
    session_scope,
    utcnow,
)
from fsrs.scheduler import (
    STATE_NAMES,
    CardSnapshot,
    FSRS4_5,
    FSRSScheduler,
    fsrs_scheduler,
    rating_from_result,
    retrievability,
    scheduler,
)
from ingestion.parser import (
    LectureParseError,
    extract_pdf_content,
    iterate_chunks,
    parse_pdf,
)
from llm.generator import (
    DEFAULT_BUNDLE_MODEL,
    OllamaError,
    generate_learning_bundle,
    get_client,
)
# NOTE: every model used in a request signature must be imported here. With
# `from __future__ import annotations` the annotations are strings, and FastAPI
# silently degrades an unresolved body model into a required *query* parameter
# (422 "query.<field> Field required" instead of reading the JSON body).
from schema import (
    ProcessLectureResponse,
    ReviewSubmission,
    StatsOut,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("campuslingo.core-ai")

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(80 * 1024 * 1024)))
MAX_NODES_PER_LECTURE = int(os.getenv("MAX_NODES_PER_LECTURE", "25"))
GENERATION_CONCURRENCY = int(os.getenv("GENERATION_CONCURRENCY", "2"))
HEARTS_MAX = 5
GEM_REFILL_COST = 350
LEAGUE_THRESHOLDS = (0, 500, 1500, 3500, 7000, 12000, 20000, 35000, 60000)
XP_BY_RATING = {1: 0, 2: 5, 3: 10, 4: 15}
XP_FIRST_CLEAR_BONUS = 5


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("database ready")
    client = get_client()
    health = await client.health()
    if health.get("ok"):
        if not health.get("chat_model_present"):
            logger.warning(
                "Ollama reachable but chat model %s is not pulled "
                "(run: ollama pull %s)",
                health.get("chat_model"),
                health.get("chat_model"),
            )
    else:
        logger.warning("Ollama not reachable: %s", health.get("error"))
    yield
    await dispose_engine()


app = FastAPI(
    title="CampusLingo core-ai",
    version="1.0.0",
    description="PDF ingestion, Qwen/Ollama challenge generation, FSRS-4.5 review loop.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv(
        "CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
    ).split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _league_for_xp(xp: int) -> int:
    tier = 0
    for index, threshold in enumerate(LEAGUE_THRESHOLDS):
        if xp >= threshold:
            tier = index
    return tier


def _apply_streak(user: User, moment: datetime) -> bool:
    """Increment/reset the daily streak. Returns True when it changed."""
    last = user.last_active_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    today = moment.date()
    if last is None:
        user.streak_count = 1
    else:
        delta = (today - last.date()).days
        if delta == 0:
            # Same-day activity keeps the streak; a fresh account that already
            # reviewed today counts as day one rather than day zero.
            if not user.streak_count:
                user.streak_count = 1
                user.last_active_at = moment
                return True
            user.last_active_at = moment
            return False
        if delta == 1:
            user.streak_count = (user.streak_count or 0) + 1
        else:
            user.streak_count = 1
    user.last_active_at = moment
    return True


async def _get_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"user {user_id} not found")
    return user


def _card_snapshot(card: FsrsCard) -> CardSnapshot:
    return CardSnapshot(
        state=card.state,
        stability=card.stability or 0.0,
        difficulty=card.difficulty or 0.0,
        due_date=card.due_date,
        last_review=card.last_review,
        reps=card.reps or 0,
        lapses=card.lapses or 0,
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health")
async def health(session: AsyncSession = Depends(get_session)) -> dict:
    db_ok = True
    db_error = None
    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        db_ok, db_error = False, str(exc)
    ollama = await get_client().health()
    return {
        "status": "ok" if (db_ok and ollama.get("ok")) else "degraded",
        "database": {"ok": db_ok, "error": db_error},
        "ollama": ollama,
        "fsrs": {"algorithm": "FSRS-4.5", "weights": len(scheduler.w)},
    }


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@app.post("/api/users", status_code=status.HTTP_201_CREATED)
async def upsert_user(payload: dict, session: AsyncSession = Depends(get_session)) -> dict:
    email = str(payload.get("email", "")).strip().lower()
    if "@" not in email:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "valid email required")
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    created = False
    if user is None:
        user = User(email=email, gems=500, xp_points=0, streak_count=0, last_active_at=utcnow())
        session.add(user)
        try:
            await session.flush()
            created = True
        except IntegrityError:
            await session.rollback()
            result = await session.execute(select(User).where(User.email == email))
            user = result.scalar_one()
            created = False
    return {
        "id": str(user.id),
        "email": user.email,
        "xp_points": user.xp_points,
        "gems": user.gems,
        "streak_count": user.streak_count,
        "league_tier": user.league_tier,
        "created": created,
    }


@app.get("/api/users/{user_id}/stats")
async def user_stats(user_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict:
    user = await _get_user(session, user_id)
    now = utcnow()

    # Passive gem regeneration: +50 gems per full 30-min interval since last_active_at
    if user.last_active_at is not None:
        last = user.last_active_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        elapsed_secs = max(0, (now - last).total_seconds())
        intervals = int(elapsed_secs // 1800)
        if intervals > 0:
            user.gems = (user.gems or 0) + intervals * 50
            user.last_active_at = last + timedelta(seconds=intervals * 1800)
            await session.flush()

    total = await session.scalar(
        select(func.count()).select_from(FsrsCard).where(FsrsCard.user_id == user_id)
    )
    due = await session.scalar(
        select(func.count())
        .select_from(FsrsCard)
        .where(FsrsCard.user_id == user_id, FsrsCard.due_date <= now)
    )
    relearning = await session.scalar(
        select(func.count())
        .select_from(FsrsCard)
        .where(FsrsCard.user_id == user_id, FsrsCard.state.in_((2, 4)))
    )
    recent = (
        await session.execute(
            select(FsrsCard.last_rating)
            .where(FsrsCard.user_id == user_id, FsrsCard.last_rating.is_not(None))
            .order_by(FsrsCard.last_review.desc().nullslast())
            .limit(50)
        )
    ).scalars().all()
    accuracy = (
        round(sum(1 for r in recent if r >= 3) / len(recent), 4) if recent else 0.0
    )

    return {
        "user_id": str(user.id),
        "email": user.email,
        "xp_points": user.xp_points,
        "gems": user.gems,
        "streak_count": user.streak_count,
        "league_tier": user.league_tier,
        "cards_total": total or 0,
        "cards_due": due or 0,
        "cards_relearning": relearning or 0,
        "accuracy_last_50": accuracy,
    }


class GemSpendRequest(BaseModel):
    amount: int = Field(..., gt=0, le=1_000_000)
    reason: str = Field("spend", max_length=120)


@app.post("/api/users/{user_id}/gems/spend")
async def spend_gems(
    user_id: uuid.UUID,
    payload: GemSpendRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    user = await _get_user(session, user_id)
    if user.gems < payload.amount:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            f"insufficient gems: have {user.gems}, need {payload.amount}",
        )
    user.gems -= payload.amount
    return {
        "user_id": str(user.id),
        "spent": payload.amount,
        "reason": payload.reason,
        "gems": user.gems,
    }


@app.post("/api/users/{user_id}/hearts/refill")
async def refill_hearts(
    user_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> dict:
    """Spend gems to refill hearts (hearts themselves live in the client store)."""
    user = await _get_user(session, user_id)
    if user.gems < GEM_REFILL_COST:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            f"refill costs {GEM_REFILL_COST} gems, you have {user.gems}",
        )
    user.gems -= GEM_REFILL_COST
    return {
        "user_id": str(user.id),
        "hearts": HEARTS_MAX,
        "gems": user.gems,
        "cost": GEM_REFILL_COST,
    }


# ---------------------------------------------------------------------------
# Courses & lectures
# ---------------------------------------------------------------------------


@app.post("/api/courses", status_code=status.HTTP_201_CREATED)
async def create_course(payload: dict, session: AsyncSession = Depends(get_session)) -> dict:
    try:
        user_id = uuid.UUID(str(payload.get("user_id")))
    except (TypeError, ValueError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "user_id must be a UUID")
    name = str(payload.get("name", "")).strip()
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "name is required")
    await _get_user(session, user_id)

    course = Course(
        user_id=user_id,
        name=name[:200],
        lms_type=str(payload.get("lms_type") or "manual")[:40],
        lms_course_id=(str(payload["lms_course_id"])[:200] if payload.get("lms_course_id") else None),
        sync_token=(str(payload["sync_token"])[:400] if payload.get("sync_token") else None),
    )
    session.add(course)
    await session.flush()

    lectures = (
        await session.execute(
            select(Lecture).where(Lecture.course_id == course.id).order_by(Lecture.created_at)
        )
    ).scalars().all()
    return {
        "id": str(course.id),
        "user_id": str(course.user_id),
        "name": course.name,
        "lms_type": course.lms_type,
        "lectures": [{"id": str(l.id), "title": l.title, "status": l.status} for l in lectures],
    }


@app.get("/api/courses")
async def list_courses(
    user_id: uuid.UUID = Query(...), session: AsyncSession = Depends(get_session)
) -> list[dict]:
    courses = (
        await session.execute(
            select(Course).where(Course.user_id == user_id).order_by(Course.created_at.desc())
        )
    ).scalars().all()
    out: list[dict] = []
    for course in courses:
        lectures = (
            await session.execute(
                select(Lecture).where(Lecture.course_id == course.id).order_by(Lecture.created_at)
            )
        ).scalars().all()
        out.append(
            {
                "id": str(course.id),
                "name": course.name,
                "lms_type": course.lms_type,
                "created_at": course.created_at,
                "lectures": [
                    {
                        "id": str(l.id),
                        "title": l.title,
                        "status": l.status,
                        "total_chunks": l.total_chunks,
                        "created_at": l.created_at,
                    }
                    for l in lectures
                ],
            }
        )
    return out


@app.get("/api/courses/{course_id}/nodes")
async def course_nodes(
    course_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> dict:
    course = await session.get(Course, course_id)
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "course not found")
    nodes = (
        await session.execute(
            select(LearningNode, Lecture.title, Lecture.id)
            .join(Lecture, LearningNode.lecture_id == Lecture.id)
            .where(Lecture.course_id == course_id)
            .order_by(Lecture.created_at, LearningNode.order_index)
        )
    ).all()
    counts = dict(
        (
            await session.execute(
                select(Challenge.node_id, func.count())
                .where(Challenge.node_id.in_([n[0].id for n in nodes] or [uuid.uuid4()]))
                .group_by(Challenge.node_id)
            )
        ).all()
    )
    return {
        "course_id": str(course.id),
        "name": course.name,
        "nodes": [
            {
                "id": str(node.id),
                "lecture_id": str(lecture_id),
                "lecture_title": lecture_title,
                "title": node.title,
                "content_summary": node.content_summary,
                "order_index": node.order_index,
                "challenge_count": int(counts.get(node.id, 0)),
            }
            for node, lecture_title, lecture_id in nodes
        ],
    }


@app.get("/api/lectures/{lecture_id}")
async def get_lecture(
    lecture_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> dict:
    lecture = await session.get(Lecture, lecture_id)
    if lecture is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "lecture not found")
    nodes = (
        await session.execute(
            select(LearningNode)
            .where(LearningNode.lecture_id == lecture_id)
            .order_by(LearningNode.order_index)
        )
    ).scalars().all()
    return {
        "id": str(lecture.id),
        "course_id": str(lecture.course_id),
        "title": lecture.title,
        "status": lecture.status,
        "total_chunks": lecture.total_chunks,
        "error_message": lecture.error_message,
        "nodes": [
            {
                "id": str(n.id),
                "title": n.title,
                "order_index": n.order_index,
                "char_count": len(n.content_summary or ""),
            }
            for n in nodes
        ],
    }


@app.post("/api/courses/{course_id}/lectures", status_code=status.HTTP_201_CREATED)
async def ingest_lecture(
    course_id: uuid.UUID,
    user_id: uuid.UUID = Form(...),
    file: UploadFile = File(...),
    max_nodes: int = Form(MAX_NODES_PER_LECTURE),
    generate: bool = Form(True),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Full pipeline: PDF -> clean text -> nodes -> 4 challenge types -> FSRS seeding."""
    started = time.perf_counter()
    course = await session.get(Course, course_id)
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "course not found")
    await _get_user(session, user_id)

    filename = file.filename or "lecture.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "only .pdf uploads")
    pdf_bytes = await file.read()
    if len(pdf_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"file exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)}MB",
        )

    lecture = Lecture(
        course_id=course_id,
        title=filename.rsplit(".", 1)[0][:160],
        status="parsing",
        source_file=filename[:400],
    )
    session.add(lecture)
    await session.flush()

    try:
        parsed = parse_pdf(pdf_bytes, filename)
    except LectureParseError as exc:
        lecture.status = "failed"
        lecture.error_message = str(exc)[:1000]
        await session.flush()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))

    selected = parsed.chunks[: max(1, max_nodes)]
    lecture.title = parsed.title[:160]
    lecture.total_chunks = len(selected)
    lecture.status = "generating"

    client = get_client()
    semaphore = asyncio.Semaphore(max(1, GENERATION_CONCURRENCY))
    generation_errors: list[str] = list(parsed.warnings)

    async def _build(chunk) -> tuple[object, list, list[str], Optional[list[float]]]:
        if not generate:
            return chunk, [], [], None
        async with semaphore:
            try:
                challenges, errors = await client.generate_for_node(
                    chunk.title, chunk.content
                )
            except Exception as exc:  # noqa: BLE001 - per-chunk isolation
                return chunk, [], [f"chunk {chunk.order_index}: {exc}"], None
            embedding: Optional[list[float]] = None
            try:
                embedding = await client.embed(f"{chunk.title}\n\n{chunk.content}")
            except (OllamaError, Exception) as exc:  # noqa: BLE001
                errors = errors + [f"chunk {chunk.order_index} embedding: {exc}"]
            return chunk, challenges, errors, embedding

    built = await asyncio.gather(*(_build(chunk) for chunk in selected))

    nodes_created = 0
    challenges_created = 0
    by_type: dict[str, int] = {}
    embeddings_stored = 0
    cards_seeded = 0

    for chunk, generated, errors, embedding in built:
        node = LearningNode(
            lecture_id=lecture.id,
            title=chunk.title[:200],
            content_summary=chunk.content[:8000],
            order_index=chunk.order_index,
        )
        session.add(node)
        await session.flush()
        nodes_created += 1
        generation_errors.extend(errors)

        if embedding:
            await session.execute(
                text(
                    "UPDATE learning_nodes SET embedding = CAST(:vec AS vector) WHERE id = :id"
                ),
                {"vec": "[" + ",".join(f"{v:.7f}" for v in embedding) + "]", "id": str(node.id)},
            )
            embeddings_stored += 1

        for item in generated:
            challenge = Challenge(
                node_id=node.id,
                type=item.challenge_type,
                payload_json=item.payload.model_dump(mode="json"),
                difficulty=item.difficulty,
            )
            session.add(challenge)
            await session.flush()
            challenges_created += 1
            by_type[item.challenge_type] = by_type.get(item.challenge_type, 0) + 1

            session.add(
                FsrsCard(
                    user_id=user_id,
                    challenge_id=challenge.id,
                    state=STATE_NEW,
                    stability=0.0,
                    difficulty=item.difficulty * 9.0 + 1.0,
                    due_date=utcnow(),
                    reps=0,
                    lapses=0,
                )
            )
            cards_seeded += 1

    lecture.status = "ready" if challenges_created else ("parsed" if not generate else "failed")
    if not challenges_created and generate:
        lecture.error_message = "; ".join(generation_errors[:5])[:1000] or "no challenges generated"
    await session.flush()

    return {
        "lecture_id": str(lecture.id),
        "title": lecture.title,
        "status": lecture.status,
        "total_chunks": len(selected),
        "nodes_created": nodes_created,
        "challenges_created": challenges_created,
        "challenges_by_type": by_type,
        "cards_seeded": cards_seeded,
        "embeddings_stored": embeddings_stored,
        "generation_errors": generation_errors[:20],
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


@app.post("/api/process-lecture", status_code=status.HTTP_201_CREATED)
async def process_lecture(
    course_id: uuid.UUID = Form(...),
    file: UploadFile = File(...),
    model_name: str = Form(DEFAULT_BUNDLE_MODEL),
    max_nodes: int = Form(MAX_NODES_PER_LECTURE),
    generate: bool = Form(True),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """PDF -> structured text -> Qwen learning unit(s) -> Postgres.

    Primary path is a single schema-constrained ``/api/generate`` call per chunk
    returning a whole :class:`LearningUnitBundle`. If that fails validation, the
    per-type fallback generator (one constrained call per challenge type, each
    with its own repair loop) is used for that chunk only.
    Returns the created node and every challenge.
    """
    started = time.perf_counter()
    course = await session.get(Course, course_id)
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "course not found")

    filename = file.filename or "lecture.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "only .pdf uploads")
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "empty upload")
    if len(pdf_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"file exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)}MB",
        )

    try:
        lecture_text = extract_pdf_content(pdf_bytes)
        chunks = iterate_chunks(lecture_text)
    except LectureParseError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    if not chunks:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "no extractable text found - the PDF is likely a scanned image; OCR it first",
        )
    chunks = chunks[: max(1, max_nodes)]

    if not generate:
        lecture = Lecture(
            course_id=course_id,
            title=filename.rsplit(".", 1)[0][:160],
            status="parsed",
            total_chunks=len(chunks),
            source_file=filename[:400],
        )
        session.add(lecture)
        await session.flush()
        return {
            "lecture_id": str(lecture.id),
            "node": {},
            "challenges": [],
            "cards_seeded": 0,
            "embeddings_stored": 0,
            "challenges_by_type": {},
            "generation_errors": [],
            "units": [{"chunk_index": c.order_index, "title": c.title} for c in chunks],
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }

    client = get_client()
    errors: list[str] = []
    units: list[dict] = []
    all_challenges: list[dict] = []
    by_type: dict[str, int] = {}
    cards_seeded = 0
    embeddings_stored = 0
    first_lecture_id: Optional[str] = None
    first_node: dict = {}

    for chunk in chunks:
        bundle: Optional[dict] = None
        try:
            bundle = await generate_learning_bundle(chunk.content, model_name=model_name)
        except Exception as exc:  # noqa: BLE001 - fallback below
            errors.append(f"chunk {chunk.order_index} bundle: {exc}")
            try:
                fallback, fallback_errors = await client.generate_for_node(
                    chunk.title, chunk.content
                )
                errors.extend(
                    f"chunk {chunk.order_index} {message}" for message in fallback_errors
                )
                if fallback:
                    bundle = {
                        "unit_id": f"fallback-{chunk.order_index}-{uuid.uuid4().hex[:8]}",
                        "concept_title": chunk.title,
                        "difficulty_level": 3,
                        "challenges": [
                            item.payload.model_dump(mode="json") for item in fallback
                        ],
                    }
            except Exception as fallback_exc:  # noqa: BLE001
                errors.append(f"chunk {chunk.order_index} fallback: {fallback_exc}")

        if not bundle:
            continue

        embedding: Optional[list[float]] = None
        try:
            embedding = await client.embed(f"{chunk.title}\n\n{chunk.content}")
        except Exception as exc:  # noqa: BLE001 - embeddings are optional
            errors.append(f"chunk {chunk.order_index} embedding: {exc}")

        try:
            saved = await save_lecture_and_challenges(
                course_id,
                chunk.title,
                bundle,
                session=session,
                source_file=filename,
                embedding=embedding,
                node_title=chunk.title,
            )
        except (ValueError, ValidationError) as exc:
            errors.append(f"chunk {chunk.order_index} persist: {exc}")
            continue

        cards_seeded += saved["cards_seeded"]
        embeddings_stored += 1 if embedding else 0
        for created in saved["challenges"]:
            by_type[created["type"]] = by_type.get(created["type"], 0) + 1
        all_challenges.extend(saved["challenges"])
        units.append(
            {
                "chunk_index": chunk.order_index,
                "lecture_id": saved["lecture"]["id"],
                "node": saved["node"],
                "challenges": len(saved["challenges"]),
                "difficulty_level": saved["node"].get("difficulty_level"),
            }
        )
        if first_lecture_id is None:
            first_lecture_id = saved["lecture"]["id"]
            first_node = saved["node"]

    if first_lecture_id is None:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"challenge generation failed for every chunk: {'; '.join(errors[:3]) or 'unknown error'}",
        )

    return {
        "lecture_id": first_lecture_id,
        "node": first_node,
        "challenges": all_challenges,
        "cards_seeded": cards_seeded,
        "embeddings_stored": embeddings_stored,
        "challenges_by_type": by_type,
        "generation_errors": errors[:20],
        "units": units,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


@app.delete("/api/lectures/{lecture_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_lecture(
    lecture_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> None:
    lecture = await session.get(Lecture, lecture_id)
    if lecture is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "lecture not found")
    # Cascade: delete FSRS cards → challenges → nodes → lecture
    node_ids = list(
        (
            await session.execute(
                select(LearningNode.id).where(LearningNode.lecture_id == lecture_id)
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
        {"id": str(lecture_id)},
    )
    await session.delete(lecture)


@app.delete("/api/courses/{course_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_course(
    course_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> None:
    course = await session.get(Course, course_id)
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "course not found")
    # Cascade: delete FSRS cards → challenges → nodes → lectures → course
    lecture_ids = list(
        (
            await session.execute(
                select(Lecture.id).where(Lecture.course_id == course_id)
            )
        ).scalars().all()
    )
    if lecture_ids:
        node_ids = list(
            (
                await session.execute(
                    select(LearningNode.id).where(LearningNode.lecture_id.in_(lecture_ids))
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
            text("DELETE FROM learning_nodes WHERE lecture_id = ANY(:ids)"),
            {"ids": [str(lid) for lid in lecture_ids]},
        )
    await session.execute(
        text("DELETE FROM lectures WHERE course_id = :id"),
        {"id": str(course_id)},
    )
    await session.delete(course)


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


@app.get("/api/nodes/{node_id}")
async def get_node(node_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> dict:
    node = await session.get(LearningNode, node_id)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "node not found")
    challenges = (
        await session.execute(
            select(Challenge)
            .where(Challenge.node_id == node_id)
            .order_by(Challenge.type, Challenge.created_at)
        )
    ).scalars().all()
    siblings = (
        await session.execute(
            select(LearningNode.id, LearningNode.title, LearningNode.order_index)
            .where(LearningNode.lecture_id == node.lecture_id, LearningNode.id != node.id)
            .order_by(LearningNode.order_index)
        )
    ).all()
    return {
        "id": str(node.id),
        "lecture_id": str(node.lecture_id),
        "title": node.title,
        "content_summary": node.content_summary,
        "order_index": node.order_index,
        "challenges": [
            {
                "id": str(c.id),
                "node_id": str(c.node_id),
                "type": c.type,
                "payload_json": c.payload_json,
                "difficulty": c.difficulty,
            }
            for c in challenges
        ],
        "next_nodes": [
            {"id": str(sid), "title": title, "order_index": idx}
            for sid, title, idx in siblings
        ],
    }


@app.get("/api/nodes/{node_id}/challenges")
async def node_challenges(
    node_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> dict:
    """Every challenge belonging to one learning node, grouped-ready for the UI."""
    node = await session.get(LearningNode, node_id)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "node not found")
    challenges = (
        await session.execute(
            select(Challenge)
            .where(Challenge.node_id == node_id)
            .order_by(Challenge.type, Challenge.created_at)
        )
    ).scalars().all()
    return {
        "node_id": str(node.id),
        "node_title": node.title,
        "lecture_id": str(node.lecture_id),
        "order_index": node.order_index,
        "count": len(challenges),
        "challenges": [
            {
                "id": str(challenge.id),
                "node_id": str(challenge.node_id),
                "type": challenge.type,
                "payload_json": challenge.payload_json,
                "difficulty": challenge.difficulty,
            }
            for challenge in challenges
        ],
    }


# ---------------------------------------------------------------------------
# Review queue / Mistakes Inbox
# ---------------------------------------------------------------------------


@app.get("/api/queue")
async def review_queue(
    user_id: uuid.UUID = Query(...),
    limit: int = Query(20, ge=1, le=100),
    only_due: bool = Query(True),
    session: AsyncSession = Depends(get_session),
) -> dict:
    items = await get_due_challenges_for_user(
        user_id, limit=limit, session=session, only_due=only_due
    )
    return {"user_id": str(user_id), "count": len(items), "items": items}


@app.get("/api/mistakes")
async def mistakes_inbox(
    user_id: uuid.UUID = Query(...),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Mistakes Inbox: every card back in Learning/Relearning after a lapse."""
    items = await get_due_challenges_for_user(
        user_id, limit=limit, session=session, only_due=False, only_mistakes=True
    )
    return {"user_id": str(user_id), "count": len(items), "items": items}


# ---------------------------------------------------------------------------
# FSRS review endpoint
# ---------------------------------------------------------------------------


async def _apply_review(session: AsyncSession, submission: ReviewSubmission) -> dict:
    """Shared pipeline: read card -> DSR update -> persist -> award rewards."""
    user_id = uuid.UUID(submission.user_id)
    challenge_id = uuid.UUID(submission.challenge_id)
    rating = submission.rating

    user = await _get_user(session, user_id)
    challenge = await session.get(Challenge, challenge_id)
    if challenge is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "challenge not found")

    card = (
        await session.execute(
            select(FsrsCard).where(
                FsrsCard.user_id == user_id, FsrsCard.challenge_id == challenge_id
            )
        )
    ).scalar_one_or_none()

    now = utcnow()
    stability = card.stability if card else 0.0
    difficulty = card.difficulty if card else challenge.difficulty * 9.0 + 1.0
    elapsed_days = 0.0
    if card is not None and card.last_review is not None:
        last = card.last_review
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        elapsed_days = max(0.0, (now - last).total_seconds() / 86400.0)

    retrievability_before = (
        fsrs_scheduler.retrievability(elapsed_days, stability) if stability else None
    )
    new_s, new_d, interval_days = fsrs_scheduler.update_card(
        rating, stability, difficulty, elapsed_days
    )
    next_due = fsrs_scheduler.due_date_for(interval_days, now)

    card_state = await record_card_review(
        user_id, challenge_id, rating, new_s, new_d, next_due, session=session
    )

    streak_multiplier = 1.0 + min(0.5, (user.streak_count or 0) * 0.02)
    xp = int(round(XP_BY_RATING[rating] * streak_multiplier))
    if (card_state["reps"] or 0) == 1 and rating >= 3:
        xp += XP_FIRST_CLEAR_BONUS
    hearts_lost = 0  # infinite hearts: never penalise
    gems = 5 if rating == 4 else (2 if rating >= 3 else 0)

    user.xp_points = (user.xp_points or 0) + xp
    user.gems = (user.gems or 0) + gems
    _apply_streak(user, now)
    user.league_tier = _league_for_xp(user.xp_points)
    await session.flush()

    return {
        "challenge_id": str(challenge_id),
        "user_id": str(user_id),
        "rating": rating,
        "state": card_state["state"],
        "state_name": STATE_NAMES.get(card_state["state"], "unknown"),
        "stability": card_state["stability"],
        "difficulty": card_state["difficulty"],
        "retrievability": retrievability_before,
        "due_date": card_state["due_date"],
        "interval_days": interval_days,
        "reps": card_state["reps"],
        "lapses": card_state["lapses"],
        "xp_awarded": xp,
        "hearts_lost": hearts_lost,
        "streak_count": user.streak_count,
        "gems": user.gems,
        "xp_points": user.xp_points,
        "league_tier": user.league_tier,
    }


@app.post("/api/reviews")
async def submit_review(
    submission: ReviewSubmission, session: AsyncSession = Depends(get_session)
) -> dict:
    """Grade one challenge attempt: DSR recalculation + persistence + rewards."""
    return await _apply_review(session, submission)


@app.post("/api/review")
async def review(payload: dict, session: AsyncSession = Depends(get_session)) -> dict:
    """Back-compat alias of ``/api/reviews`` accepting the same JSON body."""
    try:
        submission = ReviewSubmission.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, jsonable_encoder(exc.errors())[:3]
        ) from exc
    return await _apply_review(session, submission)


@app.get("/api/fsrs/preview/{challenge_id}")
async def fsrs_preview(
    challenge_id: uuid.UUID,
    user_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Interval/state prediction for all four ratings (UI interval hints)."""
    card = (
        await session.execute(
            select(FsrsCard).where(
                FsrsCard.user_id == user_id, FsrsCard.challenge_id == challenge_id
            )
        )
    ).scalar_one_or_none()
    snapshot = _card_snapshot(card) if card else CardSnapshot()
    now = utcnow()
    preview = scheduler.preview(snapshot, now)
    return {
        "challenge_id": str(challenge_id),
        "current_state": STATE_NAMES.get(snapshot.state, "new"),
        "current_retrievability": scheduler.current_retrievability(snapshot, now),
        "options": {
            str(rating): {
                "interval_days": res.interval_days,
                "due_date": res.due_date,
                "state": res.state,
                "state_name": res.state_name,
                "stability": res.stability,
                "difficulty": res.difficulty,
            }
            for rating, res in preview.items()
        },
    }


# ---------------------------------------------------------------------------
# Grading helper (used by the web app's optimistic client-side scoring)
# ---------------------------------------------------------------------------


@app.post("/api/grade")
async def grade_attempt(payload: dict) -> dict:
    """Map a raw game result onto an FSRS rating without touching the DB."""
    is_correct = bool(payload.get("is_correct"))
    elapsed_ms = int(payload.get("elapsed_ms") or 0)
    hints_used = int(payload.get("hints_used") or 0)
    rating = rating_from_result(is_correct, elapsed_ms, hints_used)
    return {
        "rating": rating,
        "is_correct": is_correct,
        "elapsed_ms": elapsed_ms,
        "hints_used": hints_used,
    }


if __name__ == "__main__":  # pragma: no cover - dev entrypoint
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=bool(os.getenv("RELOAD")),
    )
