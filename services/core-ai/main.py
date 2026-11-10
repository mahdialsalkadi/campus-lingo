"""CampusLingo core-ai service.

FastAPI app that turns uploaded lecture PDFs into gamified challenges and
drives the FSRS-4.5 review loop.

Run:
    uvicorn main:app --reload --port 8000 --app-dir services/core-ai
"""

from __future__ import annotations

import asyncio
import csv
import functools
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import string
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from arena import (
    QUESTION_SECONDS,
    ArenaError,
    arena_question_from_challenge,
    room_manager,
)
from classroom import classroom_hub

from database import (
    STATE_NEW,
    AppMeta,
    Challenge,
    Course,
    Enrollment,
    ExamSession,
    FsrsCard,
    LearningNode,
    Lecture,
    Notification,
    PushSubscription,
    User,
    classrooms_for_instructor,
    clone_course_tree,
    course_owner_for_challenge,
    course_owner_and_title,
    courses_for_student,
    delete_lecture_subtree,
    dispose_engine,
    enrolled_student_ids,
    enrollment_for,
    find_replaceable_lecture,
    get_due_challenges_for_user,
    lecture_snapshot,
    get_session,
    init_db,
    node_title_with_number,
    record_card_review,
    save_pdf_lecture,
    session_scope,
    unique_enrollment_code,
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
    derive_lecture_title,
    extract_pages,
    extract_pdf_content,
    extract_pdf_diagrams,
    iterate_chunks,
    parse_pdf,
    save_diagrams,
)
from llm.crunch import (
    EXAM_DURATION_SECONDS,
    EXAM_TARGET_MAX,
    EXAM_TARGET_MIN,
    assemble_mock_exam,
    build_digest,
    exam_target,
    grade_exam,
    public_exam_questions,
    synthesize_cheatsheet,
)
from llm.diagrams import synthesize_diagram_challenge
from llm.generator import (
    DEFAULT_BUNDLE_MODEL,
    MODEL_TIERS,
    LlamaCppError,
    aclose_all_clients,
    build_chapter_boss_fight,
    generate_bundles_for_chunks,
    iter_generated_bundles_for_chunks,
    generate_learning_bundle,
    get_client,
    get_active_client,
    get_tier,
    normalize_tier,
)
from retention import (
    VAPID_PUBLIC_KEY,
    dispatch,
    notify_new_lecture,
    push_available,
    retention_loop,
    retention_sweep,
)
from spaced_repetition.engine import (
    WARMUP_XP_MULTIPLIER,
    estimated_minutes,
)
from models import (
    BUCKETS,
    BUCKET_CRITICAL,
    BUCKET_MIDTERM,
    BUCKET_YESTERDAY,
    card_facts_from_row,
    memory_health,
    next_card_state,
    queue_partition,
)
# NOTE: every model used in a request signature must be imported here. With
# `from __future__ import annotations` the annotations are strings, and FastAPI
# silently degrades an unresolved body model into a required *query* parameter
# (422 "query.<field> Field required" instead of reading the JSON body).
from schema import (
    ALL_CHALLENGE_TYPES,
    GENERATION_CHALLENGE_TYPES,
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
UPLOAD_READ_CHUNK_BYTES = 1024 * 1024


async def _read_upload_capped(
    file: UploadFile, max_bytes: int = MAX_UPLOAD_BYTES
) -> bytes:
    """Read an upload into memory in bounded chunks, enforcing the size cap.

    ``UploadFile.read()`` with no argument slurps the whole (possibly multi-
    hundred-MB) body in one allocation before any size check runs, which is a
    reliable way to OOM the worker and drop the connection. Reading 1 MiB at a
    time lets the 413 fire before the peak allocation grows past the cap.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"file exceeds {max_bytes // (1024 * 1024)}MB",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _guard_upload(endpoint):
    """Return a JSON 500 instead of dropping the TCP connection on a crash.

    An unexpected exception raised mid-request makes the ASGI server abort the
    socket, so the browser sees an opaque `NetworkError` with no status code.
    Wrapping every upload handler means a parse/persist failure is logged with
    its full traceback and delivered as a normal ``{"detail": ...}`` 500 body
    the client can render. ``HTTPException`` passes through untouched so the
    deliberate 4xx statuses keep their original message.

    ``functools.wraps`` keeps ``__wrapped__``, so FastAPI still reads the
    endpoint's real signature and injects the ``Form``/``File`` parameters.
    """

    @functools.wraps(endpoint)
    async def wrapper(*args, **kwargs):
        try:
            return await endpoint(*args, **kwargs)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - the whole point is to catch all
            logger.error(
                "upload handler %s crashed", endpoint.__name__, exc_info=True
            )
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"{endpoint.__name__} failed: {exc}",
            ) from exc

    return wrapper


# Duolingo-style granularity: consolidate each lecture into 2-3 thematic nodes
# instead of fragmenting it into dozens of micro-sections.
MAX_NODES_PER_LECTURE = int(os.getenv("MAX_NODES_PER_LECTURE", "3"))
# --- DIAGRAM_QA ingestion ----------------------------------------------------
# How many extracted slide figures become diagram challenges per upload, and
# the starting difficulty assigned to those challenges.
MAX_DIAGRAMS_PER_UPLOAD = int(os.getenv("MAX_DIAGRAMS_PER_UPLOAD", "6"))
DIAGRAM_DIFFICULTY = float(os.getenv("DIAGRAM_DIFFICULTY", "0.4"))
# Creator royalty: gems credited to an author when a classmate clears one of
# their shared challenges (see _apply_review).
CREATOR_ROYALTY_GEMS = int(os.getenv("CREATOR_ROYALTY_GEMS", "2"))
# Freemium: free accounts get this many lecture uploads per rolling day.
FREE_DAILY_UPLOADS = int(os.getenv("FREE_DAILY_UPLOADS", "5"))
GENERATION_CONCURRENCY = int(os.getenv("GENERATION_CONCURRENCY", "2"))
# Model tiers of the Qwen 3.5 family, each served by its own llama-server on a
# fixed port (800M -> 8081 ... 9B -> 8084). Uploads pick the tier; generation
# routes to that tier's endpoint with the generic model id "default".
DEFAULT_MODEL_TIER = os.getenv("SELECTED_MODEL_TIER", "4B")
# Cap on concurrent single-chunk bundle generations. Qwen on a consumer GPU
# does ~20-40 tok/s serially; llama-server with ``-np 2`` runs two slots, so a
# fan-out wider than that simply queues inside the server. Tune alongside
# ``-np`` / ``--parallel`` on the llama-server command line.
BUNDLE_CONCURRENCY = int(os.getenv("BUNDLE_CONCURRENCY", "2"))


def parse_challenge_types(raw: str) -> list[str]:
    """Parse the ``challenge_types`` upload form field into a validated list.

    Comma-separated names ("SPOT_MISTAKE,MCQ_QUIZ"); empty/missing means the
    four high-yield pedagogical variants (:data:`GENERATION_CHALLENGE_TYPES`).
    DIAGRAM_QA is attached separately from extracted slide figures and TRUE_FALSE
    is legacy/render-only, so neither is a valid text-generation request here.
    Unknown names are dropped with a warning so a stale client cannot kill an
    upload, and de-duplication preserves the caller's order.
    """
    requested = [item.strip().upper() for item in (raw or "").split(",") if item.strip()]
    default = list(GENERATION_CHALLENGE_TYPES)
    if not requested:
        return default
    known = set(GENERATION_CHALLENGE_TYPES)
    seen: list[str] = []
    for item in requested:
        if item in known and item not in seen:
            seen.append(item)
    if not seen:
        logger.warning(
            "challenge_types field had no generatable types (%r); using the four variants",
            raw,
        )
        return default
    return seen


HEARTS_MAX = 5
GEM_REFILL_COST = 350
GEM_REGEN_AMOUNT = 50       # gems granted per full 30-minute block of inactivity
GEM_REGEN_INTERVAL_SECS = 1800
LEAGUE_THRESHOLDS = (0, 500, 1500, 3500, 7000, 12000, 20000, 35000, 60000)
XP_BY_RATING = {1: 0, 2: 5, 3: 10, 4: 15}
XP_FIRST_CLEAR_BONUS = 5

# In-quiz Socratic tutor (POST /api/tutor/hint) — constants + streaming core
# live in llm/tutor.py; main.py keeps only the HTTP surface (gem charging).
from llm.tutor import (  # noqa: E402  (after llm.generator import block)
    TUTOR_HINT_COST,
    TUTOR_SYSTEM_PROMPT,
    TutorHintRequest,
    build_tutor_messages,
    encode_sse,
    stream_socratic_hint,
)

# Legacy alias (older call sites used the private name).
_sse = encode_sse

# Backwards-compatible alias for the configured preferred tutor tier.
TUTOR_TIER = os.getenv("TUTOR_TIER", "800M")

# --- Gem Shop catalog -------------------------------------------------------
# ``effect`` documents what the purchase does; the server applies the mechanical
# part (gems, inventory, streak_freeze_active) so prices cannot be spoofed.
SHOP_ITEMS: tuple[dict[str, Any], ...] = (
    {
        "id": "streak_freeze",
        "name_ar": "تجميد الحماسة",
        "name_en": "Streak Freeze",
        "icon": "🛡️",
        "price": 200,
        "description_ar": "يحمي سلسلة أيامك إذا غبت 24 ساعة",
        "effect": "streak_freeze",
    },
    {
        "id": "double_xp",
        "name_ar": "جرعة مضاعفة النقاط",
        "name_en": "Double XP Potion",
        "icon": "🧪",
        "price": 150,
        "description_ar": "يضاعف نقاط الخبرة لمدة 30 دقيقة",
        "effect": "double_xp",
        "duration_seconds": 1800,
    },
    {
        "id": "golden_pass",
        "name_ar": "التمريرة الذهبية",
        "name_en": "Golden Infinite Pass",
        "icon": "🏅",
        "price": 900,
        "description_ar": "شارة دائمة على ملفك الشخصي",
        "effect": "badge",
    },
)
SHOP_ITEMS_BY_ID = {item["id"]: item for item in SHOP_ITEMS}

# --- Weekly leagues ---------------------------------------------------------
LEAGUE_NAMES = (
    "Bronze", "Silver", "Gold", "Sapphire", "Ruby",
    "Emerald", "Amethyst", "Pearl", "Obsidian",
)
LEAGUE_COHORT_SIZE = 30
LEAGUE_PROMOTION_ZONE = 5   # top 5 climb a tier every Sunday
LEAGUE_RELEGATION_ZONE = 5  # bottom 5 drop a tier


# Extracted slide diagrams (and future upload artefacts) are served from
# /static so the browser can render a DIAGRAM_QA figure directly.
STATIC_DIR = os.getenv(
    "STATIC_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
)
DIAGRAM_DIRNAME = "uploads/diagrams"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("database ready")
    client = get_client()
    health = await client.health()
    if health.get("ok"):
        if not health.get("chat_model_present"):
            logger.warning(
                "llama-server reachable but chat model %s is not loaded "
                "(check the -m flag passed to llama-server)",
                health.get("chat_model"),
            )
    else:
        logger.warning("llama-server not reachable: %s", health.get("error"))

    # Retention loop: streak-saver (8 PM) + FSRS decay reminders. Runs as a
    # background task so an unreachable model or a slow DB never delays boot.
    retention_task: Optional[asyncio.Task] = None
    if os.getenv("RETENTION_ENABLED", "1").lower() not in ("0", "false", "no"):
        retention_task = asyncio.create_task(retention_loop())
        logger.info("retention loop started")
    try:
        yield
    finally:
        if retention_task is not None:
            retention_task.cancel()
            try:
                await retention_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await aclose_all_clients()
        await dispose_engine()


app = FastAPI(
    title="CampusLingo core-ai",
    version="1.0.0",
    description="PDF ingestion, llama.cpp challenge generation, FSRS-4.5 review loop.",
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

@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort net: turn any unhandled crash into a JSON 500 body.

    Without this, an exception escaping a handler makes the ASGI server abort
    the response and the browser reports a bare `NetworkError`. This guarantees
    a valid ``{"detail": ...}`` payload for every endpoint, with the traceback
    captured server-side for debugging.
    """
    logger.error(
        "unhandled exception on %s %s", request.method, request.url.path,
        exc_info=True,
    )
    return JSONResponse(status_code=500, content={"detail": "internal server error"})


os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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
        elif delta == 2 and user.streak_freeze_active:
            # Streak Freeze (🛡️ تجميد الحماسة) absorbs exactly one missed day:
            # the shield is spent and the streak survives as if never broken.
            user.streak_freeze_active = False
            inventory = dict(user.inventory or {})
            inventory["streak_freeze_used"] = int(inventory.get("streak_freeze_used", 0)) + 1
            user.inventory = inventory
            user.streak_count = (user.streak_count or 0) + 1
            logger.info("streak freeze consumed for %s", user.id)
        else:
            user.streak_count = 1
    user.last_active_at = moment
    return True


async def _get_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"user {user_id} not found")
    return user


# ---------------------------------------------------------------------------
# Classroom + diagram helpers
# ---------------------------------------------------------------------------


def _boss_fight_enabled() -> bool:
    """Chapter-culmination BOSS_FIGHT synthesis toggle (on by default)."""
    return os.getenv("ENABLE_BOSS_FIGHT", "1").strip().lower() not in ("0", "false", "no", "off")


async def _attach_chapter_boss(
    session: AsyncSession,
    lecture_id: uuid.UUID,
    lecture_title: str,
    owner_id: uuid.UUID,
) -> Optional[dict]:
    """Synthesize + persist the chapter-culmination BOSS_FIGHT on the last node.

    The boss re-uses the strongest questions the lecture already produced
    (free, deterministic, always on-topic) instead of a tenth GPU pass, and is
    seeded with its own FSRS card like every other challenge. Returns the
    created-challenge dict, or ``None`` when the material is too thin.
    """
    rows = (
        await session.execute(
            select(Challenge, LearningNode.order_index)
            .join(LearningNode, Challenge.node_id == LearningNode.id)
            .where(LearningNode.lecture_id == lecture_id)
            .order_by(LearningNode.order_index)
        )
    ).all()
    if not rows:
        return None
    payloads = [challenge.payload_json for challenge, _order in rows if challenge.payload_json]
    boss = build_chapter_boss_fight(lecture_title, payloads)
    if boss is None:
        return None
    last_node_id = max(rows, key=lambda row: row[1])[0].node_id
    challenge = Challenge(
        node_id=last_node_id,
        type="BOSS_FIGHT",
        payload_json=boss.payload.model_dump(mode="json"),
        difficulty=boss.difficulty,
    )
    session.add(challenge)
    await session.flush()
    session.add(
        FsrsCard(
            user_id=owner_id,
            challenge_id=challenge.id,
            state=STATE_NEW,
            stability=0.0,
            difficulty=boss.difficulty * 9.0 + 1.0,
            due_date=utcnow(),
            reps=0,
            lapses=0,
        )
    )
    return {
        "id": str(challenge.id),
        "node_id": str(last_node_id),
        "type": "BOSS_FIGHT",
        "payload_json": challenge.payload_json,
        "difficulty": boss.difficulty,
    }


async def _attach_diagram_challenges(
    session: AsyncSession,
    course_id: uuid.UUID,
    owner_id: uuid.UUID,
    nodes: list[dict],
    pdf_bytes: bytes,
    *,
    tier: Optional[str] = None,
    max_diagrams: int = MAX_DIAGRAMS_PER_UPLOAD,
) -> list[dict]:
    """Extract slide figures and persist one DIAGRAM_QA challenge per figure.

    The figure is written to ``static/uploads/diagrams/{course_id}/`` and the
    challenge payload carries its public ``/static/...`` URL, so the browser can
    render it in the zoomable diagram card. Figures are distributed round-robin
    across the lecture's nodes, so a long deck spreads its visuals out.
    """
    if not nodes:
        return []
    diagrams = extract_pdf_diagrams(pdf_bytes, max_diagrams=max_diagrams)
    if not diagrams:
        return []

    page_text: dict[int, str] = {}
    try:
        for page in extract_pages(pdf_bytes):
            page_text[page.page_number] = page.clean_text
    except LectureParseError:
        page_text = {}

    directory = os.path.join(STATIC_DIR, DIAGRAM_DIRNAME, str(course_id))
    url_prefix = f"/static/{DIAGRAM_DIRNAME}/{course_id}"
    assets = save_diagrams(diagrams, directory, url_prefix=url_prefix)
    titles = [str(node.get("title") or "") for node in nodes]

    created: list[dict] = []
    for index, asset in enumerate(assets):
        target = nodes[index % len(nodes)]
        text = page_text.get(int(asset.get("page") or 0), "")
        caption = next((line.strip() for line in text.splitlines() if line.strip()), "")
        distractors = [title for title in titles if title and title != target.get("title")]
        payload = await synthesize_diagram_challenge(
            image_url=str(asset["url"]),
            diagram_ref=str(asset["ref"]),
            caption=caption,
            context=f"{target.get('title', '')}\n{text or target.get('content_summary', '')}",
            distractors=distractors,
            tier=tier,
        )
        challenge = Challenge(
            node_id=uuid.UUID(str(target["id"])),
            type="DIAGRAM_QA",
            payload_json=payload,
            difficulty=DIAGRAM_DIFFICULTY,
            assets={"diagram": asset["url"], "page": asset.get("page")},
        )
        session.add(challenge)
        await session.flush()
        session.add(
            FsrsCard(
                user_id=owner_id,
                challenge_id=challenge.id,
                state=STATE_NEW,
                stability=0.0,
                difficulty=DIAGRAM_DIFFICULTY * 9.0 + 1.0,
                due_date=utcnow(),
                reps=0,
                lapses=0,
            )
        )
        created.append(
            {
                "id": str(challenge.id),
                "node_id": str(target["id"]),
                "type": "DIAGRAM_QA",
                "payload_json": payload,
                "difficulty": DIAGRAM_DIFFICULTY,
                "assets": {"diagram": asset["url"], "page": asset.get("page")},
            }
        )
    return created


async def _publish_classroom_update(
    session: AsyncSession,
    course_id: uuid.UUID,
    *,
    lecture_title: str,
    lecture_id: Optional[str] = None,
    node_count: int = 0,
) -> dict:
    """Fan a new-lecture event out to an instructor's classroom.

    No-op for personal (non-classroom) courses. Enrolled students get a durable
    notification (plus a Web Push attempt) *and* every connected dashboard gets
    the SSE event, so the chapter tree updates without a refresh.
    """
    course = await session.get(Course, course_id)
    if course is None or not course.is_classroom:
        return {"is_classroom": False, "notified": 0, "listeners": 0}

    student_ids = await enrolled_student_ids(session, course_id)
    notifications: list[Notification] = []
    if student_ids:
        notifications = await notify_new_lecture(
            session,
            student_ids=student_ids,
            lecture_title=lecture_title,
            course_id=course_id,
            course_name=course.name,
            url="/",
        )
    event = {
        "type": "lecture_added",
        "lecture_id": lecture_id,
        "lecture_title": lecture_title,
        "node_count": node_count,
    }
    listeners = await classroom_hub.publish(course_id, event)
    # The instructor's own dashboard listens on the same channel: a second tab
    # or a co-teacher screen refreshes too.
    return {
        "is_classroom": True,
        "notified": len(notifications),
        "listeners": listeners,
    }


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
    llm = await get_client().health()
    # Qwen 3.5 family: probe each tier's llama-server (8081..8084) so the UI can
    # offer every tier that is actually up. Overall status is ok if the default
    # tier answers (legacy single-server deployments set LLAMA_SERVER_URL, which
    # pins every tier to that endpoint).
    tiers_health: dict[str, dict] = {}
    for tier_id, spec in MODEL_TIERS.items():
        tiers_health[tier_id] = await get_client(tier_id).health()
    llm["tiers"] = {
        tier_id: {
            "ok": bool(tiers_health[tier_id].get("ok")),
            "port": spec["port"],
            "name": spec["name"],
            "label": spec["label"],
            "description": spec["description"],
            "model_path": spec["model_path"],
            "parallel_slots": spec["parallel_slots"],
        }
        for tier_id, spec in MODEL_TIERS.items()
    }
    llm["ok"] = bool(
        llm.get("ok") or any(status.get("ok") for status in tiers_health.values())
    )
    llm["selected_tier"] = get_tier().get("id")
    llm["tier_catalog"] = {
        tier_id: {
            "name": spec["name"],
            "label": spec["label"],
            "description": spec["description"],
            "port": spec["port"],
        }
        for tier_id, spec in MODEL_TIERS.items()
    }
    return {
        "status": "ok" if (db_ok and llm.get("ok")) else "degraded",
        "database": {"ok": db_ok, "error": db_error},
        "llm": llm,
        # Back-compat alias for clients written against the Ollama-era shape.
        "ollama": llm,
        "fsrs": {"algorithm": "FSRS-4.5", "weights": len(scheduler.w)},
    }


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


# scrypt parameters (OWASP-recommended 2024 baseline: N=2^17, r=8, p=1).
# 128*N*r = 128MiB per hash, so maxmem must be raised above OpenSSL's 32MiB
# default or hashlib.scrypt raises "memory limit exceeded".
_SCRYPT_N = 2**17
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 256 * 1024 * 1024
_MIN_PASSWORD_LEN = 8
_MAX_PASSWORD_LEN = 128


def _hash_password(password: str) -> str:
    """Hash with scrypt (memory-hard, stdlib). Format: scrypt$N$r$p$salt$hash."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    """Constant-time verification of a stored scrypt hash; False on any mismatch."""
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_SCRYPT_MAXMEM,
        )
        return secrets.compare_digest(digest, bytes.fromhex(hash_hex))
    except (ValueError, TypeError):
        return False


def _validate_password(password: str) -> str:
    if not _MIN_PASSWORD_LEN <= len(password) <= _MAX_PASSWORD_LEN:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"password must be {_MIN_PASSWORD_LEN}-{_MAX_PASSWORD_LEN} characters",
        )
    return password


@app.post("/api/auth/register", status_code=status.HTTP_201_CREATED)
async def register_user(payload: dict, session: AsyncSession = Depends(get_session)) -> dict:
    """Create an account with email + password. The email must be new."""
    email = str(payload.get("email", "")).strip().lower()
    if "@" not in email or len(email) > 320:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "valid email required")
    password = _validate_password(str(payload.get("password", "")))

    existing = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "an account with this email already exists")

    user = User(
        email=email,
        password_hash=_hash_password(password),
        gems=500,
        xp_points=0,
        streak_count=0,
        last_active_at=utcnow(),
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "an account with this email already exists")
    return {
        "id": str(user.id),
        "email": user.email,
        "xp_points": user.xp_points,
        "gems": user.gems,
        "streak_count": user.streak_count,
        "league_tier": user.league_tier,
        "created": True,
    }


@app.post("/api/auth/login")
async def login_user(payload: dict, session: AsyncSession = Depends(get_session)) -> dict:
    """Verify email + password. Deliberately vague on which field was wrong."""
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))
    user = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if (
        user is None
        or not user.password_hash
        or not _verify_password(password, user.password_hash)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "incorrect email or password")
    return {
        "id": str(user.id),
        "email": user.email,
        "xp_points": user.xp_points,
        "gems": user.gems,
        "streak_count": user.streak_count,
        "league_tier": user.league_tier,
        "created": False,
    }


@app.patch("/api/users/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    user_id: uuid.UUID, payload: dict, session: AsyncSession = Depends(get_session)
) -> None:
    """Set a password for a legacy passwordless account, or change it (auth not
    wired to the UI yet — the endpoint exists so accounts are not locked out)."""
    user = await _get_user(session, user_id)
    password = _validate_password(str(payload.get("password", "")))
    current = payload.get("current_password")
    if user.password_hash:
        if not current or not _verify_password(str(current), user.password_hash):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "current password is incorrect")
    user.password_hash = _hash_password(password)
    await session.flush()


@app.post("/api/users", status_code=status.HTTP_201_CREATED)
async def upsert_user(payload: dict, session: AsyncSession = Depends(get_session)) -> dict:
    """Email-only sign-in gate: create the account or restore an existing one.

    * New email → ``created: true``; the account starts with 500 gems and a
      blank dashboard (no password required at this gate; the password
      endpoints cover accounts that want one).
    * Existing email → ``created: false``; progress, courses, cards, XP and
      gems are restored by virtue of being keyed to the user id.
    """
    email = str(payload.get("email", "")).strip().lower()
    if "@" not in email or len(email) > 320:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "valid email required")
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    created = False
    if user is None:
        user = User(
            email=email,
            gems=500,
            xp_points=0,
            streak_count=0,
            last_active_at=utcnow(),
        )
        session.add(user)
        try:
            await session.flush()
            created = True
        except IntegrityError:
            # A concurrent signup won the race; restore that account instead.
            await session.rollback()
            result = await session.execute(select(User).where(User.email == email))
            user = result.scalar_one()
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

    # ------------------------------------------------------------------
    # Passive gem regeneration: +50 gems for every full 30-minute block
    # elapsed since last_active_at. Committed immediately in its own
    # transaction so regenerated gems survive even if the stats query below
    # fails; the last_active_at cursor is advanced by exactly the granted
    # time so partial blocks keep accruing.
    # ------------------------------------------------------------------
    if user.last_active_at is not None:
        last = user.last_active_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        elapsed_secs = max(0, (now - last).total_seconds())
        full_blocks = int(elapsed_secs // GEM_REGEN_INTERVAL_SECS)
        if full_blocks > 0:
            user.gems = (user.gems or 0) + full_blocks * GEM_REGEN_AMOUNT
            user.last_active_at = last + timedelta(
                seconds=full_blocks * GEM_REGEN_INTERVAL_SECS
            )
            await session.commit()
            logger.debug(
                "passive gem regen for %s: +%d gems (%d blocks)",
                user_id,
                full_blocks * GEM_REGEN_AMOUNT,
                full_blocks,
            )

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
        "league_name": LEAGUE_NAMES[min(user.league_tier, len(LEAGUE_NAMES) - 1)],
        "weekly_xp": user.weekly_xp,
        "streak_freeze_active": user.streak_freeze_active,
        "inventory": user.inventory or {},
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


SHARE_CODE_ALPHABET = string.ascii_uppercase + string.digits  # unambiguous-ish, uppercase
SHARE_CODE_PREFIX = "CL-"
SHARE_CODE_LENGTH = 6  # the code body itself; displayed/formatted as CL-4F8A2B


def _generate_share_code() -> str:
    """Random 6-char share-code body (crypto-safe, uppercase alnum).

    Only the body is stored in ``courses.share_code`` (VARCHAR(8)); the
    user-facing ``CL-`` prefix is added at the API layer.
    """
    return "".join(
        secrets.choice(SHARE_CODE_ALPHABET) for _ in range(SHARE_CODE_LENGTH)
    )


def _format_share_code(body: str) -> str:
    """User-facing form of a share code, e.g. ``4F8A2B`` -> ``CL-4F8A2B``."""
    return f"{SHARE_CODE_PREFIX}{body}"


def _normalize_share_code(raw: str) -> str:
    """Accept ``CL-4F8A2B``, ``cl-4f8a2b`` or ``4f8a2b``; return the bare body."""
    code = raw.strip().upper()
    if code.startswith(SHARE_CODE_PREFIX):
        code = code[len(SHARE_CODE_PREFIX):]
    return code


async def _unique_share_code(session: AsyncSession) -> str:
    """Generate a share code whose body is not already taken."""
    for _ in range(20):
        code = _generate_share_code()
        taken = await session.scalar(
            select(Course.id).where(Course.share_code == code).limit(1)
        )
        if taken is None:
            return code
    raise HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "could not allocate a unique share code, please retry",
    )


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
        share_code=await _unique_share_code(session),
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
        "share_code": _format_share_code(course.share_code),
        "lectures": [{"id": str(l.id), "title": l.title, "status": l.status} for l in lectures],
    }


@app.get("/api/courses")
async def list_courses(
    user_id: uuid.UUID = Query(...), session: AsyncSession = Depends(get_session)
) -> list[dict]:
    """Every course the user owns, with its lectures in ANY status.

    Progressive-ingestion contract: a lecture is listed as soon as its row
    exists (``parsing``/``generating``/``partially_ready``/``ready``), so the
    dashboard can show the deck while later parts still generate. Do not filter
    on ``Lecture.status`` here.
    """
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
                "share_code": _format_share_code(course.share_code) if course.share_code else None,
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
    """Every learning node in the course, regardless of lecture status.

    Progressive-ingestion contract: progressive unlocking persists a node the
    moment its chunk's bundle lands, flipping the lecture to ``partially_ready``
    while parts 2..N keep generating. This query therefore MUST NOT filter on
    ``Lecture.status`` — hiding nodes until ``status == 'ready'`` would defeat
    the zero-wait "Part 1" start. Ordering (lecture created_at, node order) is
    stable, so newly committed parts simply append to the end of the tree.
    """
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
        "share_code": _format_share_code(course.share_code) if course.share_code else None,
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


# ---------------------------------------------------------------------------
# Course sharing (6-char share codes)
# ---------------------------------------------------------------------------


@app.get("/api/courses/{course_id}/share-code")
async def get_share_code(
    course_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> dict:
    """Return (or lazily generate for) the owner's course share code.

    Codes are stored with the ``CL-`` prefix, e.g. ``CL-4F8A``. Generation is
    retried on unique-violation races so concurrent share requests succeed.
    """
    course = await session.get(Course, course_id)
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "course not found")

    if course.share_code:
        return {
            "course_id": str(course.id),
            "share_code": _format_share_code(course.share_code),
        }

    for _ in range(5):
        course.share_code = await _unique_share_code(session)
        try:
            await session.commit()
            return {
                "course_id": str(course.id),
                "share_code": _format_share_code(course.share_code),
            }
        except IntegrityError:
            await session.rollback()
            course.share_code = None
    raise HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "could not allocate a unique share code, please retry",
    )


class ImportCourseRequest(BaseModel):
    user_id: str = Field(..., min_length=32, max_length=64)
    share_code: str = Field(..., min_length=4, max_length=16)


@app.post("/api/courses/import", status_code=status.HTTP_201_CREATED)
async def import_course(
    payload: ImportCourseRequest, session: AsyncSession = Depends(get_session)
) -> dict:
    """Deep-clone a shared course into the importing user's account.

    Duplicates the course, its lectures, learning nodes and challenges, then
    seeds one fresh ``fsrs_cards`` row per cloned challenge (state 0 / new, due
    immediately) so the importer's spaced-repetition schedule starts from
    scratch. Returns the new course id.
    """
    try:
        user_id = uuid.UUID(payload.user_id)
    except (TypeError, ValueError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "user_id must be a UUID")

    # Accept ``CL-4F8A2B``, ``cl-4f8a2b`` or a bare ``4f8a2b``.
    code = _normalize_share_code(payload.share_code)
    if not re.fullmatch(r"[0-9A-Z]{6}", code):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid share code format (expected 6 characters, e.g. CL-4F8A2B)",
        )

    await _get_user(session, user_id)

    source = (
        await session.execute(select(Course).where(Course.share_code == code))
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no course with this share code")

    # Eager-load the whole tree in one go.
    lectures = (
        await session.execute(
            select(Lecture)
            .where(Lecture.course_id == source.id)
            .order_by(Lecture.created_at)
        )
    ).scalars().all()
    lecture_ids = [lecture.id for lecture in lectures]
    nodes = (
        await session.execute(
            select(LearningNode)
            .where(LearningNode.lecture_id.in_(lecture_ids or [uuid.uuid4()]))
            .order_by(LearningNode.created_at, LearningNode.order_index)
        )
    ).scalars().all()
    node_ids = [node.id for node in nodes]
    challenges = (
        await session.execute(
            select(Challenge)
            .where(Challenge.node_id.in_(node_ids or [uuid.uuid4()]))
            .order_by(Challenge.created_at)
        )
    ).scalars().all()

    # ------------------------------------------------------------------
    # Deep clone. Importing your own course is allowed (a personal reset);
    # every row gets a fresh UUID and the clone gets its own share code so
    # both courses remain independently shareable.
    # ------------------------------------------------------------------
    clone = Course(
        user_id=user_id,
        name=source.name[:200],
        lms_type=source.lms_type,
        share_code=await _unique_share_code(session),
    )  # the clone gets its own bare-body share code
    session.add(clone)
    await session.flush()

    # ------------------------------------------------------------------
    # Clone level by level, flushing between levels so every new row has
    # its generated UUID before the next level references it.
    # ------------------------------------------------------------------
    new_lectures: list[Lecture] = []
    lecture_id_map: dict[uuid.UUID, uuid.UUID] = {}
    for lecture in lectures:
        new_lecture = Lecture(
            course_id=clone.id,
            title=lecture.title,
            status=lecture.status,
            total_chunks=lecture.total_chunks,
            source_file=lecture.source_file,
            error_message=lecture.error_message,
        )
        session.add(new_lecture)
        new_lectures.append(new_lecture)
    await session.flush()  # assigns new_lectures[*].id
    lecture_id_map = {
        source_lecture.id: new_lecture.id
        for source_lecture, new_lecture in zip(lectures, new_lectures)
    }

    new_nodes: list[LearningNode] = []
    for node in nodes:
        new_node = LearningNode(
            lecture_id=lecture_id_map[node.lecture_id],
            title=node.title,
            content_summary=node.content_summary,  # embeddings are not copied
            order_index=node.order_index,
        )
        session.add(new_node)
        new_nodes.append(new_node)
    await session.flush()  # assigns new_nodes[*].id
    node_id_map = {
        source_node.id: new_node.id
        for source_node, new_node in zip(nodes, new_nodes)
    }

    new_challenges: list[Challenge] = []
    for challenge in challenges:
        new_challenge = Challenge(
            node_id=node_id_map[challenge.node_id],
            type=challenge.type,
            payload_json=challenge.payload_json,
            difficulty=challenge.difficulty,
        )
        session.add(new_challenge)
        new_challenges.append(new_challenge)
    await session.flush()  # assigns new_challenges[*].id

    # Fresh FSRS cards for the importer: brand-new schedules from state 0,
    # one per cloned challenge, due immediately.
    for challenge, new_challenge in zip(challenges, new_challenges):
        session.add(
            FsrsCard(
                user_id=user_id,
                challenge_id=new_challenge.id,
                state=STATE_NEW,
                stability=0.0,
                difficulty=challenge.difficulty * 9.0 + 1.0,
                due_date=utcnow(),
                reps=0,
                lapses=0,
            )
        )

    await session.commit()

    cards_seeded = len(new_challenges)

    return {
        "course_id": str(clone.id),
        "name": clone.name,
        "source_course_id": str(source.id),
        "lectures_cloned": len(lecture_id_map),
        "nodes_cloned": len(node_id_map),
        "challenges_cloned": len(new_challenges),
        "cards_seeded": cards_seeded,
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
@_guard_upload
async def ingest_lecture(
    course_id: uuid.UUID,
    user_id: uuid.UUID = Form(...),
    file: UploadFile = File(...),
    model_tier: str = Form(DEFAULT_MODEL_TIER),
    max_nodes: int = Form(MAX_NODES_PER_LECTURE),
    generate: bool = Form(True),
    challenge_types: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Full pipeline: PDF -> clean text -> nodes -> chosen challenge types -> FSRS seeding."""
    selected_types = parse_challenge_types(challenge_types)
    started = time.perf_counter()
    course = await session.get(Course, course_id)
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "course not found")
    await _get_user(session, user_id)

    filename = file.filename or "lecture.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "only .pdf uploads")
    pdf_bytes = await _read_upload_capped(file)
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

    # One PDF = one dedicated Lecture, named after the extracted main title
    # (or the filename when no title can be derived).
    selected = parsed.chunks[: max(1, max_nodes)]
    lecture.title = parsed.title[:160]
    lecture.total_chunks = len(selected)
    lecture.status = "generating"

    client = get_client(normalize_tier(model_tier))
    semaphore = asyncio.Semaphore(max(1, GENERATION_CONCURRENCY))
    generation_errors: list[str] = list(parsed.warnings)

    async def _build(chunk) -> tuple[object, list, list[str], Optional[list[float]]]:
        if not generate:
            return chunk, [], [], None
        async with semaphore:
            try:
                challenges, errors = await client.generate_for_node(
                    chunk.title, chunk.content, challenge_types=selected_types
                )
            except Exception as exc:  # noqa: BLE001 - per-chunk isolation
                return chunk, [], [f"chunk {chunk.order_index}: {exc}"], None
            # Embeddings are optional: with the default LLAMA_EMBEDDINGS_ENABLED=0
            # this returns a deterministic fallback vector without any HTTP call.
            embedding = await client.embed_optional(f"{chunk.title}\n\n{chunk.content}")
            return chunk, challenges, errors, embedding

    built = await asyncio.gather(*(_build(chunk) for chunk in selected))

    nodes_created = 0
    challenges_created = 0
    by_type: dict[str, int] = {}
    embeddings_stored = 0
    cards_seeded = 0

    for position, (chunk, generated, errors, embedding) in enumerate(built):
        node = LearningNode(
            lecture_id=lecture.id,
            # Academic sub-topic name ("3.1 Paging Mechanics"), never "Node 1".
            title=node_title_with_number(chunk.title, lecture.title, position),
            content_summary=chunk.content[:8000],
            order_index=position,
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

    # Chapter culmination: one BOSS_FIGHT on the last node synthesizing the
    # whole chapter (skipped when the deck is too thin or disabled).
    if generate and challenges_created and _boss_fight_enabled():
        boss_challenge = await _attach_chapter_boss(session, lecture.id, lecture.title, user_id)
        if boss_challenge is not None:
            challenges_created += 1
            by_type["BOSS_FIGHT"] = by_type.get("BOSS_FIGHT", 0) + 1
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
@_guard_upload
async def process_lecture(
    course_id: uuid.UUID = Form(...),
    file: UploadFile = File(...),
    model_name: str = Form(DEFAULT_BUNDLE_MODEL),
    model_tier: str = Form(DEFAULT_MODEL_TIER),
    max_nodes: int = Form(MAX_NODES_PER_LECTURE),
    generate: bool = Form(True),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """PDF -> structured text -> Qwen learning unit(s) -> Postgres.

    Primary path is a single schema-constrained ``/api/generate`` call per chunk
    returning a whole :class:`LearningUnitBundle`. If that fails validation, the
    per-type fallback generator (one constrained call per challenge type, each
    with its own repair loop) is used for that chunk only.
    Chunks are generated concurrently (``BUNDLE_CONCURRENCY``, default 2) so a
    multi-chunk lecture no longer serialises the whole GPU; persistence stays
    in course order afterwards.
    Returns the created node and every challenge.
    """
    started = time.perf_counter()
    course = await session.get(Course, course_id)
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "course not found")

    filename = file.filename or "lecture.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "only .pdf uploads")
    pdf_bytes = await _read_upload_capped(file)
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

    # Chapter folder title: a clean heading from the first two slides, else the
    # original file name (never a generated concept title or "Lecture 1").
    derived_title = derive_lecture_title(lecture_text, filename)

    # Duplicate guard: an identical lecture that already has content is returned
    # as-is — no second folder and no re-run of inference.
    existing = await find_replaceable_lecture(
        course_id, source_file=filename, title=derived_title, session=session
    )
    if existing is not None:
        snapshot = await lecture_snapshot(session, existing)
        if snapshot["units"]:
            logger.info(
                "duplicate upload for course %s -> returning lecture %s",
                course_id,
                existing.id,
            )
            return {
                "lecture_id": snapshot["lecture"]["id"],
                "node": snapshot["units"][0]["node"],
                "challenges": [],
                "cards_seeded": snapshot["cards_seeded"],
                "embeddings_stored": 0,
                "challenges_by_type": snapshot["challenges_by_type"],
                "generation_errors": ["duplicate upload: returned the existing lecture"],
                "units": snapshot["units"],
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
            }
        # A previous attempt left an empty lecture (failed parse/generation):
        # replace it instead of creating a duplicate parallel folder.
        await delete_lecture_subtree(session, existing.id)

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

    client = get_client(normalize_tier(model_tier))

    # Phase 1 — generate all bundles concurrently. No DB session is held while
    # the GPU works, so pool slots never sit pinned for minutes.
    results = await generate_bundles_for_chunks(
        chunks, client, model_name=model_name, concurrency=BUNDLE_CONCURRENCY
    )

    # Phase 2 — persist the WHOLE PDF as ONE lecture with numbered nodes
    # ("3.1 Paging Mechanics"), in one transaction.
    errors: list[str] = []
    units: list[dict] = []
    all_challenges: list[dict] = []
    by_type: dict[str, int] = {}
    embeddings_stored = 0
    lecture_row: dict = {}
    first_node: dict = {}

    bundles = [bundle for _chunk, bundle, _emb, _errs in results]
    base_titles = [chunk.title for chunk, _b, _e, _errs in results]
    embeddings = [embedding for _c, _b, embedding, _errs in results]
    for _c, _b, _e, chunk_errors in results:
        errors.extend(chunk_errors)

    if any(bundle for _c, bundle, _e, _errs in results):
        try:
            saved = await save_pdf_lecture(
                course_id,
                derived_title,
                bundles,
                session=session,
                source_file=filename,
                base_titles=base_titles,
                embeddings=embeddings,
            )
        except (ValueError, ValidationError) as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"persist failed: {exc}",
            ) from exc

        lecture_row = saved["lecture"]
        embeddings_stored = sum(1 for e in embeddings if e)
        for node in saved["nodes"]:
            node_challenges = [c for c in saved["challenges"] if c["node_id"] == node["id"]]
            for created in node_challenges:
                by_type[created["type"]] = by_type.get(created["type"], 0) + 1
            all_challenges.extend(node_challenges)
            units.append(
                {
                    "chunk_index": node["order_index"],
                    "lecture_id": saved["lecture"]["id"],
                    "node": node,
                    "challenges": len(node_challenges),
                    "difficulty_level": node.get("difficulty_level"),
                }
            )
        first_node = saved["first_node"]

        # DIAGRAM_QA: every real figure in the deck becomes a visual challenge,
        # attached round-robin to this lecture's nodes.
        diagram_challenges = await _attach_diagram_challenges(
            session, course_id, course.user_id, saved["nodes"], pdf_bytes, tier=model_tier
        )
        for created in diagram_challenges:
            by_type["DIAGRAM_QA"] = by_type.get("DIAGRAM_QA", 0) + 1
            all_challenges.append(created)
            for unit in units:
                if unit["node"]["id"] == created["node_id"]:
                    unit["challenges"] = unit.get("challenges", 0) + 1
                    break
        cards_seeded = saved["cards_seeded"] + len(diagram_challenges)

        # Chapter culmination: one BOSS_FIGHT on the last node.
        if _boss_fight_enabled():
            boss_challenge = await _attach_chapter_boss(
                session, uuid.UUID(str(lecture_row["id"])), derived_title, course.user_id
            )
            if boss_challenge is not None:
                by_type["BOSS_FIGHT"] = by_type.get("BOSS_FIGHT", 0) + 1
                all_challenges.append(boss_challenge)
                cards_seeded += 1
                for unit in units:
                    if unit["node"]["id"] == boss_challenge["node_id"]:
                        unit["challenges"] = unit.get("challenges", 0) + 1
                        break

        # Instructor -> students: notify + broadcast when this is a classroom.
        classroom = await _publish_classroom_update(
            session,
            course_id,
            lecture_title=derived_title,
            lecture_id=lecture_row["id"],
            node_count=len(saved["nodes"]),
        )
    else:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"challenge generation failed for every chunk: {'; '.join(errors[:3]) or 'unknown error'}",
        )

    return {
        "lecture_id": lecture_row["id"],
        "node": first_node,
        "challenges": all_challenges,
        "cards_seeded": cards_seeded,
        "embeddings_stored": embeddings_stored,
        "challenges_by_type": by_type,
        "generation_errors": errors[:20],
        "units": units,
        "classroom": classroom,
        "diagram_challenges": len(diagram_challenges),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
    }


# ---------------------------------------------------------------------------
# Background ingestion — same pipeline as /api/process-lecture but the HTTP
# request returns immediately with a job id, and progress is polled. The
# generation loop is identical; only the session lifecycle differs (a dedicated
# session per chunk persist instead of one request-scoped session).
#
# PROGRESSIVE UNLOCKING (zero-wait start): each chunk commits to PostgreSQL as
# soon as its bundle finishes inference ("Part 1" lands after ~2-3s), the
# lecture status moves to ``partially_ready`` until every chunk is saved, and
# a classroom-hub SSE event (``node_unlocked``) fires per part so a listening
# frontend can unlock "Part 1" for immediate play while parts 2..N generate
# in the background.
# ---------------------------------------------------------------------------


def _as_progressive_bundle(bundle: Any) -> Any:
    """Validate a raw bundle dict into the typed LearningUnitBundle."""
    from schema import LearningUnitBundle

    if isinstance(bundle, LearningUnitBundle):
        return bundle
    return LearningUnitBundle.model_validate(bundle)


async def _commit_progressive_node(
    course_id: uuid.UUID,
    lecture_id: uuid.UUID,
    lecture_number: int,
    base_title: str,
    bundle: Any,
    embedding: Optional[list[float]],
    position: int,
) -> dict:
    """Persist ONE finished chunk (node + challenges + FSRS cards) in its own
    transaction and report the created node.

    Called by the background worker as soon as a single chunk's generation
    completes — long before the remaining chunks finish — so the student can
    start playing Part 1 immediately (``partially_ready`` zero-wait start).
    """
    async with session_scope() as session:
        course = await session.get(Course, course_id)
        if course is None:
            raise ValueError(f"course {course_id} not found")
        # Accept a validated LearningUnitBundle or its raw dict form.
        unit_bundle = _as_progressive_bundle(bundle)
        # Zero-wait signal: as soon as the FIRST chunk lands, flip the lecture
        # row to ``partially_ready`` so every reader of the DB (frontend tree,
        # GET /api/lectures) sees Part 1 as playable while parts 2..N generate.
        lecture_row = await session.get(Lecture, lecture_id)
        if lecture_row is not None and position == 0:
            lecture_row.status = "partially_ready"
        node = LearningNode(
            lecture_id=lecture_id,
            title=node_title_with_number(
                base_title,
                f"{lecture_number}. Lecture",
                position,
            ),
            content_summary=str(unit_bundle.concept_title or base_title)[:8000],
            order_index=position,
        )
        session.add(node)
        await session.flush()
        if embedding:
            await session.execute(
                text(
                    "UPDATE learning_nodes SET embedding = CAST(:vec AS vector) WHERE id = :id"
                ),
                {
                    "vec": "[" + ",".join(f"{v:.7f}" for v in embedding) + "]",
                    "id": str(node.id),
                },
            )
        created = 0
        for item in unit_bundle.challenge_payloads():
            challenge_type, payload = item
            challenge = Challenge(
                node_id=node.id,
                type=str(challenge_type),
                payload_json=payload,
                difficulty=0.4,
            )
            session.add(challenge)
            await session.flush()
            session.add(
                FsrsCard(
                    user_id=course.user_id,
                    challenge_id=challenge.id,
                    state=STATE_NEW,
                    stability=0.0,
                    difficulty=0.4 * 9.0 + 1.0,
                    due_date=utcnow(),
                    reps=0,
                    lapses=0,
                )
            )
            created += 1

        # Explicit, IMMEDIATE write-back. ``session_scope`` would also commit on
        # context exit, but committing (and refreshing) here — inside the block
        # — guarantees the node + challenges + FSRS cards are durable and
        # visible to concurrent readers the moment this function returns, never
        # deferred until the whole deck finishes. (expire_on_commit=False keeps
        # the refreshed ORM state readable afterwards.)
        await session.commit()
        await session.refresh(node)

        return {
            "chunk_index": position,
            "lecture_id": str(lecture_id),
            "node": {
                "id": str(node.id),
                "lecture_id": str(lecture_id),
                "title": node.title,
                "content_summary": node.content_summary,
                "order_index": node.order_index,
            },
            "challenges": created,
        }


@dataclass
class _IngestJob:
    """In-memory ingestion job. Progress fields are read cross-task."""

    job_id: str
    course_id: uuid.UUID
    total_chunks: int
    done: int = 0
    failed: int = 0
    # queued | parsing | generating | partially_ready | saving | done | error
    status: str = "queued"
    error: Optional[str] = None
    lecture_id: Optional[str] = None
    units: list[dict] = field(default_factory=list)
    generation_errors: list[str] = field(default_factory=list)
    challenges_by_type: dict[str, int] = field(default_factory=dict)
    cards_seeded: int = 0
    embeddings_stored: int = 0
    elapsed_ms: int = 0
    diagram_challenges: int = 0
    classroom: dict = field(default_factory=dict)
    unlocked_nodes: list[str] = field(default_factory=list)


_INGEST_JOBS: dict[str, _IngestJob] = {}


def _job_public_state(job: _IngestJob) -> dict:
    """The pollable view of a job — safe to hand to jsonable_encoder."""
    done = job.done
    total = max(1, job.total_chunks)
    return {
        "job_id": job.job_id,
        "status": job.status,
        "total_chunks": job.total_chunks,
        "done_chunks": done,
        "failed_chunks": job.failed,
        "progress": round(done / total, 4),
        "error": job.error,
        "lecture_id": job.lecture_id,
        "units": list(job.units),
        "generation_errors": list(job.generation_errors[-20:]),
        "challenges_by_type": dict(job.challenges_by_type),
        "cards_seeded": job.cards_seeded,
        "embeddings_stored": job.embeddings_stored,
        "elapsed_ms": job.elapsed_ms,
        "diagram_challenges": job.diagram_challenges,
        "classroom": dict(job.classroom),
        # Progressive unlocking: ids of nodes already playable while the rest
        # of the deck still generates in the background.
        "unlocked_nodes": list(job.unlocked_nodes),
    }


async def _run_ingest_job(
    job: _IngestJob,
    *,
    filename: str,
    pdf_bytes: bytes,
    model_name: str,
    model_tier: str = DEFAULT_MODEL_TIER,
    challenge_types: Optional[list[str]] = None,
) -> None:
    """Background body of an ingestion job (parse -> generate -> persist)."""
    started = time.perf_counter()
    job.status = "parsing"
    try:
        lecture_text = extract_pdf_content(pdf_bytes)
        chunks = iterate_chunks(lecture_text)
        if not chunks:
            job.status = "error"
            job.error = (
                "no extractable text found - the PDF is likely a scanned image; OCR it first"
            )
            return
        chunks = chunks[: job.total_chunks]
        job.total_chunks = len(chunks)
    except LectureParseError as exc:
        job.status = "error"
        job.error = str(exc)
        return

    derived_title = derive_lecture_title(lecture_text, filename)

    # Duplicate guard: an identical lecture that already has content is returned
    # as-is — no second folder and no re-run of inference (a long GPU batch must
    # not be spent twice on the same upload).
    async with session_scope() as session:
        existing = await find_replaceable_lecture(
            job.course_id, source_file=filename, title=derived_title, session=session
        )
        if existing is not None:
            snapshot = await lecture_snapshot(session, existing)
            if snapshot["units"]:
                logger.info(
                    "duplicate upload for course %s -> returning lecture %s",
                    job.course_id,
                    existing.id,
                )
                job.lecture_id = snapshot["lecture"]["id"]
                job.units = snapshot["units"]
                job.challenges_by_type = snapshot["challenges_by_type"]
                job.cards_seeded = snapshot["cards_seeded"]
                job.done = len(snapshot["units"])
                job.total_chunks = max(1, len(snapshot["units"]))
                job.generation_errors.append(
                    "duplicate upload: returned the existing lecture"
                )
                job.elapsed_ms = int((time.perf_counter() - started) * 1000)
                job.status = "done"
                return
            # A previous attempt left an empty lecture: replace, don't duplicate.
            await delete_lecture_subtree(session, existing.id)

    client = get_client(normalize_tier(model_tier))
    job.status = "generating"

    # ------------------------------------------------------------------
    # PROGRESSIVE PIPELINE: generate every chunk concurrently but commit
    # each finished bundle IMMEDIATELY in its own transaction. Part 1 becomes
    # playable after ~2-3s (lecture flips to ``partially_ready`` and an SSE
    # ``node_unlocked`` event fires); parts 2..N unlock while the student
    # plays. Nothing blocks on the whole deck finishing inference.
    # ------------------------------------------------------------------
    async with session_scope() as session:
        course_row = await session.get(Course, job.course_id)
        if course_row is None:
            job.status = "error"
            job.error = f"course {job.course_id} not found"
            return
        course_owner_id = course_row.user_id
        is_classroom = bool(course_row.is_classroom)
        lecture_number = (
            await session.scalar(
                select(func.count()).select_from(Lecture).where(Lecture.course_id == job.course_id)
            )
            or 0
        ) + 1
        lecture = Lecture(
            course_id=job.course_id,
            title=derived_title[:160],
            status="generating",
            total_chunks=len(chunks),
            source_file=filename[:400],
        )
        session.add(lecture)
        await session.flush()
        job.lecture_id = str(lecture.id)

    # ------------------------------------------------------------------
    # STREAMING PROGRESSIVE PIPELINE: consume the generator as each chunk
    # finishes inference and persist IMMEDIATELY in its own transaction. The
    # first completed chunk commits (and the lecture flips to
    # ``partially_ready``) within seconds of the upload while parts 2..N are
    # still generating — nothing waits for the whole deck's inference.
    # ------------------------------------------------------------------
    job.status = "saving"
    valid_results = 0
    position = 0
    async for chunk, bundle, embedding, chunk_errors in iter_generated_bundles_for_chunks(
        chunks,
        client,
        model_name=model_name,
        concurrency=BUNDLE_CONCURRENCY,
        challenge_types=challenge_types,
    ):
        position += 1
        job.generation_errors.extend(chunk_errors)
        if not bundle:
            job.failed += 1
            continue
        valid_results += 1
        try:
            unit = await _commit_progressive_node(
                course_id=job.course_id,
                lecture_id=uuid.UUID(str(job.lecture_id)),
                lecture_number=lecture_number,
                base_title=chunk.title,
                bundle=bundle,
                embedding=embedding,
                # ``chunk.order_index`` is the chunk's own position in the
                # deck, so streaming completion order cannot scramble part
                # numbering even though chunks finish out of order.
                position=getattr(chunk, "order_index", position - 1),
            )
        except (ValueError, ValidationError) as exc:
            job.failed += 1
            job.generation_errors.append(f"persist failed for chunk {chunk.order_index}: {exc}")
            continue
        job.units.append(unit)
        job.done += 1
        job.unlocked_nodes.append(unit["node"]["id"])
        for challenge_payload in (bundle.get("challenges") or []) if isinstance(bundle, dict) else []:
            created_type = (
                challenge_payload.get("type")
                if isinstance(challenge_payload, dict)
                else None
            )
            if created_type:
                job.challenges_by_type[str(created_type)] = (
                    job.challenges_by_type.get(str(created_type), 0) + 1
                )
        job.cards_seeded += unit["challenges"]
        if embedding:
            job.embeddings_stored += 1

        # Zero-wait beat: at least one part exists and is playable.
        if job.status == "saving":
            job.status = "partially_ready"
        # Push the unlock over SSE. Personal courses have no classroom channel,
        # so publishing there only logs the misleading
        # "published node_unlocked to 0 listener(s)" — the frontend's 1.5s
        # ingest-job polling already covers that case. Classrooms get the live
        # event for every connected dashboard.
        if is_classroom:
            await classroom_hub.publish(
                job.course_id,
                {
                    "type": "node_unlocked",
                    "lecture_id": str(job.lecture_id),
                    "lecture_title": derived_title,
                    "node_id": unit["node"]["id"],
                    "node_title": unit["node"]["title"],
                    "part_number": unit["chunk_index"] + 1,
                    "total_parts": len(chunks),
                    "done_chunks": job.done,
                },
            )

    if valid_results == 0:
        job.status = "error"
        job.error = (
            "challenge generation failed for every chunk: "
            + ("; ".join(job.generation_errors[:3]) or "unknown error")
        )
        async with session_scope() as session:
            await delete_lecture_subtree(session, uuid.UUID(str(job.lecture_id)))
        return

    # DIAGRAM_QA + final status + instructor fan-out, in one final transaction.
    async with session_scope() as session:
        lecture_row = await session.get(Lecture, uuid.UUID(str(job.lecture_id)))
        nodes = list(
            (
                await session.execute(
                    select(LearningNode)
                    .where(LearningNode.lecture_id == uuid.UUID(str(job.lecture_id)))
                    .order_by(LearningNode.order_index)
                )
            ).scalars().all()
        )
        node_payloads = [
            {
                "id": str(node.id),
                "lecture_id": str(node.lecture_id),
                "title": node.title,
                "content_summary": node.content_summary,
                "order_index": node.order_index,
            }
            for node in nodes
        ]
        diagram_challenges = await _attach_diagram_challenges(
            session,
            job.course_id,
            course_owner_id,
            node_payloads,
            pdf_bytes,
            tier=model_tier,
        )
        for created in diagram_challenges:
            job.challenges_by_type["DIAGRAM_QA"] = (
                job.challenges_by_type.get("DIAGRAM_QA", 0) + 1
            )
            for unit in job.units:
                if unit["node"]["id"] == created["node_id"]:
                    unit["challenges"] = unit.get("challenges", 0) + 1
                    break
        job.cards_seeded += len(diagram_challenges)
        job.diagram_challenges = len(diagram_challenges)

        # Chapter culmination: one BOSS_FIGHT on the last node.
        if _boss_fight_enabled():
            boss_challenge = await _attach_chapter_boss(
                session,
                uuid.UUID(str(job.lecture_id)),
                derived_title,
                course_owner_id,
            )
            if boss_challenge is not None:
                job.challenges_by_type["BOSS_FIGHT"] = (
                    job.challenges_by_type.get("BOSS_FIGHT", 0) + 1
                )
                job.cards_seeded += 1
                for unit in job.units:
                    if unit["node"]["id"] == boss_challenge["node_id"]:
                        unit["challenges"] = unit.get("challenges", 0) + 1
                        break

        if lecture_row is not None:
            # ``partially_ready`` stays visible when some chunks failed so the
            # UI keeps the unlocked parts playable instead of hiding them.
            lecture_row.status = "ready" if job.failed == 0 else "partially_ready"

        job.classroom = await _publish_classroom_update(
            session,
            job.course_id,
            lecture_title=derived_title,
            lecture_id=job.lecture_id,
            node_count=len(node_payloads),
        )

    job.elapsed_ms = int((time.perf_counter() - started) * 1000)
    job.status = "done"


@app.post("/api/process-lecture-async", status_code=status.HTTP_202_ACCEPTED)
@_guard_upload
async def process_lecture_async(
    course_id: uuid.UUID = Form(...),
    file: UploadFile = File(...),
    model_name: str = Form(DEFAULT_BUNDLE_MODEL),
    model_tier: str = Form(DEFAULT_MODEL_TIER),
    max_nodes: int = Form(MAX_NODES_PER_LECTURE),
    challenge_types: str = Form(""),
) -> dict:
    """Kick off ingestion in the background; returns ``{job_id}`` immediately.

    Validation mirrors the synchronous endpoint (course must exist, PDF only,
    size cap) so the client learns about bad input from the 202 response.
    Progress: ``GET /api/ingest-jobs/{job_id}``.
    """
    async with session_scope() as session:
        course = await session.get(Course, course_id)
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "course not found")

    filename = file.filename or "lecture.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "only .pdf uploads")
    pdf_bytes = await _read_upload_capped(file)
    if not pdf_bytes:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "empty upload")
    if len(pdf_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"file exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)}MB",
        )

    job = _IngestJob(
        job_id=uuid.uuid4().hex,
        course_id=course_id,
        total_chunks=max(1, max_nodes),
    )
    _INGEST_JOBS[job.job_id] = job

    asyncio.create_task(
        _run_ingest_job(
            job,
            filename=filename,
            pdf_bytes=pdf_bytes,
            model_name=model_name,
            model_tier=model_tier,
            challenge_types=parse_challenge_types(challenge_types),
        )
    )
    logger.info("ingestion job %s started for course %s", job.job_id, course_id)
    return _job_public_state(job)


@app.get("/api/ingest-jobs/{job_id}")
async def get_ingest_job(job_id: str) -> dict:
    """Poll an ingestion job: status, per-chunk progress and final result."""
    job = _INGEST_JOBS.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ingestion job not found")
    state = _job_public_state(job)
    if job.status in ("done", "error"):
        _INGEST_JOBS.pop(job_id, None)  # one-shot delivery; frees the store
    return JSONResponse(jsonable_encoder(state))


@app.delete("/api/lectures/{lecture_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_lecture(
    lecture_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> None:
    """Delete ONE lecture and its descendants — never the parent course.

    Isolation contract: the delete query filters strictly by
    ``lectures.id == lecture_id``. Sibling lectures and the parent ``Course``
    row are unreachable from here: every FK in the hierarchy points
    child→parent (learning_nodes→lectures→courses), so a lecture delete can
    only cascade downwards (nodes → challenges → fsrs_cards), never sideways
    or upwards. fsrs_cards/challenges are removed explicitly first because
    their only FK path runs through challenges → learning_nodes.
    """
    lecture = await session.get(Lecture, lecture_id)
    if lecture is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "lecture not found")

    # Depth-first cleanup, all scoped to THIS lecture's ids only. The final
    # DELETE is pinned to the primary key: no subquery, no join, no course_id
    # in sight — deleting a lecture can never delete its course or siblings.
    await delete_lecture_subtree(session, lecture_id)


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

    # Ebbinghaus write-back: advance (or reset) the forgetting-curve rung and
    # reset the recall probability, so the daily warm-up queue stays honest.
    ebbinghaus = next_card_state(
        card,
        rating=rating,
        new_stability=new_s,
        new_difficulty=new_d,
        next_due=next_due,
        now=now,
    ) if card is not None else None

    card_state = await record_card_review(
        user_id, challenge_id, rating, new_s, new_d, next_due,
        session=session, ebbinghaus=ebbinghaus,
    )

    streak_multiplier = 1.0 + min(0.5, (user.streak_count or 0) * 0.02)
    xp = int(round(XP_BY_RATING[rating] * streak_multiplier))
    # Daily Memory Warm-up ritual: reviews submitted from the warm-up arena
    # earn the advertised 1.5x XP bonus (the banner promises it, so the award
    # has to honor it).
    if (submission.context or "") == "warmup":
        xp = int(round(xp * WARMUP_XP_MULTIPLIER))
    if (card_state["reps"] or 0) == 1 and rating >= 3:
        xp += XP_FIRST_CLEAR_BONUS
    # Infinite hearts: no practice lockouts, so reviews never cost a life.
    hearts_lost = 0
    gems = 5 if rating == 4 else (2 if rating >= 3 else 0)

    user.xp_points = (user.xp_points or 0) + xp
    # Weekly leagues rank on XP earned inside the current Sunday-to-Sunday
    # cycle, so the same award feeds both the lifetime tier and the weekly race.
    user.weekly_xp = (user.weekly_xp or 0) + xp
    user.gems = (user.gems or 0) + gems
    _apply_streak(user, now)
    user.league_tier = _league_for_xp(user.xp_points)

    # Creator royalty (viral sharing): clearing a challenge that lives in
    # somebody else's shared course credits the author's gem stash, so students
    # are rewarded for publishing good material instead of only studying it.
    creator_royalty = 0
    owner_id = await course_owner_for_challenge(session, challenge_id)
    if owner_id is not None and owner_id != user_id:
        creator = await session.get(User, owner_id)
        if creator is not None:
            creator.gems = (creator.gems or 0) + CREATOR_ROYALTY_GEMS
            creator.creator_gems = (creator.creator_gems or 0) + CREATOR_ROYALTY_GEMS
            creator_royalty = CREATOR_ROYALTY_GEMS

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
        "repetition_level": card_state["repetition_level"],
        "retrievability_score": card_state["retrievability_score"],
        "xp_awarded": xp,
        "hearts_lost": hearts_lost,
        "streak_count": user.streak_count,
        "gems": user.gems,
        "xp_points": user.xp_points,
        "league_tier": user.league_tier,
        "weekly_xp": user.weekly_xp,
        "streak_freeze_active": user.streak_freeze_active,
        "creator_royalty": creator_royalty,
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


# ---------------------------------------------------------------------------
# Ebbinghaus Daily Memory Warm-up (spaced repetition ritual)
# ---------------------------------------------------------------------------


async def _load_card_views(
    session: AsyncSession, user_id: uuid.UUID, *, limit: int = 500
) -> list:
    """Every FSRS card for one user as spec-facing views (no due filtering)."""
    rows = (
        await session.execute(
            select(FsrsCard).where(FsrsCard.user_id == user_id).limit(max(1, limit))
        )
    ).scalars().all()
    return [card_facts_from_row(row) for row in rows]


@app.get("/api/reviews/daily-queue")
async def daily_review_queue(
    user_id: uuid.UUID = Query(...),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The Daily Memory Warm-up ritual: today's due cards, pedagogically bucketed.

    A card enters the queue when ``due_date <= now()`` **or** its estimated
    recall probability has decayed below 0.85 (Ebbinghaus retention threshold).
    The queue is partitioned into:

    * ``yesterday`` — مراجعة الأمس: learned 24-48h ago, first consolidation,
    * ``midterm`` — مراجعة الأيام الـ 3 والـ 7: secondary consolidation rungs,
    * ``critical`` — معرض للنسيان!: high failure rate / deep decay.

    The response also carries the gamification envelope the dashboard banner
    renders: the streak-saver flag and the 1.5x warm-up XP multiplier.
    """
    user = await _get_user(session, user_id)
    now = utcnow()

    views = await _load_card_views(session, user_id)
    buckets = queue_partition(views, now=now)

    bucket_labels = {
        BUCKET_YESTERDAY: "مراجعة الأمس (Day 1)",
        BUCKET_MIDTERM: "مراجعة الأيام الـ 3 والـ 7",
        BUCKET_CRITICAL: "معرض للنسيان!",
    }

    cards_out: list[dict] = []
    for name in (BUCKET_YESTERDAY, BUCKET_MIDTERM, BUCKET_CRITICAL):
        for view in buckets.get(name, [])[:limit]:
            payload = view.to_payload(now=now)
            payload["bucket"] = name
            payload["bucket_label"] = bucket_labels[name]
            cards_out.append(payload)

    # Enrich with the renderable challenge payload so the warm-up arena can
    # drill the card immediately without a second round-trip.
    if cards_out:
        challenge_ids = [
            uuid.UUID(card["challenge_id"]) for card in cards_out
        ]
        rows = (
            await session.execute(
                select(Challenge, LearningNode)
                .join(LearningNode, Challenge.node_id == LearningNode.id)
                .where(Challenge.id.in_(challenge_ids))
            )
        ).all()
        by_challenge: dict[str, dict] = {}
        for challenge, node in rows:
            by_challenge[str(challenge.id)] = {
                "node_id": str(node.id),
                "node_title": node.title,
                "lecture_id": str(node.lecture_id),
                "type": challenge.type,
                "payload_json": challenge.payload_json,
            }
        for card in cards_out:
            extra = by_challenge.get(card["challenge_id"])
            if extra:
                card.update(extra)

    yesterday_count = len(buckets.get(BUCKET_YESTERDAY, []))
    midterm_count = len(buckets.get(BUCKET_MIDTERM, []))
    critical_count = len(buckets.get(BUCKET_CRITICAL, []))
    total_due = yesterday_count + midterm_count + critical_count

    last_active = user.last_active_at
    if last_active is not None and last_active.tzinfo is None:
        last_active = last_active.replace(tzinfo=timezone.utc)
    streak_saver = (
        total_due > 0
        and (user.streak_count or 0) >= 1
        and (last_active is None or last_active.date() < now.date())
    )
    return {
        "user_id": str(user_id),
        "date": now.date().isoformat(),
        "total_due_count": total_due,
        "estimated_minutes": estimated_minutes(total_due),
        "yesterday_count": yesterday_count,
        "midterm_count": midterm_count,
        "critical_count": critical_count,
        "streak_saver": streak_saver,
        "xp_bonus_multiplier": WARMUP_XP_MULTIPLIER,
        "cards": cards_out,
    }


@app.get("/api/lectures/{lecture_id}/memory-health")
async def lecture_memory_health(
    lecture_id: uuid.UUID,
    user_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Chapter-folder "صحة الذاكرة" meter: aggregate retention for one lecture.

    Returns the 0..1 health score, its green/amber/red band and the per-card
    breakdown so the folder can show "n مفاهيم بدأت تتلاشى" and offer the
    one-click mini-review of only this chapter's expired cards.
    """
    rows = (
        await session.execute(
            select(FsrsCard)
            .join(Challenge, FsrsCard.challenge_id == Challenge.id)
            .join(LearningNode, Challenge.node_id == LearningNode.id)
            .where(FsrsCard.user_id == user_id, LearningNode.lecture_id == lecture_id)
        )
    ).scalars().all()
    views = [card_facts_from_row(row) for row in rows]
    health = memory_health(views)
    return {
        "lecture_id": str(lecture_id),
        "user_id": str(user_id),
        "total": health.total,
        "due": health.due,
        "fresh": health.fresh,
        "decaying": health.decaying,
        "critical": health.critical,
        "score": health.score,
        "band": health.band,
    }


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


# ---------------------------------------------------------------------------
# Socratic AI tutor (streaming, Tier 1 / 800M) — core lives in llm/tutor.py
# ---------------------------------------------------------------------------


@app.post("/api/tutor/hint")
async def tutor_hint(
    payload: TutorHintRequest, session: AsyncSession = Depends(get_session)
) -> StreamingResponse:
    """Stream a Socratic clue for the current challenge, word-by-word.

    Prefers the ultra-fast Tier 1 (Qwen 3.5 0.8B, port 8081) so the hint feels
    instant; when that tier's llama-server is unreachable, the stream is
    transparently re-routed to the highest healthy tier (2B -> 4B -> 9B) via
    :func:`get_active_client` (see :mod:`llm.tutor`) so the tutor never dies
    with a connection error just because the user only booted one tier.
    ``temperature`` is pinned to 0.3 for stable, on-topic analogies. When
    ``user_id`` is present the 10-gem price is charged before the first token,
    so a hint can never be free-ridden.
    """
    if payload.user_id:
        try:
            user = await _get_user(session, uuid.UUID(payload.user_id))
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid user_id") from exc
        if user.gems < TUTOR_HINT_COST:
            raise HTTPException(
                status.HTTP_402_PAYMENT_REQUIRED,
                f"a hint costs {TUTOR_HINT_COST} gems, you have {user.gems}",
            )
        user.gems -= TUTOR_HINT_COST
        inventory = dict(user.inventory or {})
        inventory["hints_used"] = int(inventory.get("hints_used", 0)) + 1
        user.inventory = inventory
        await session.commit()

    messages = build_tutor_messages(
        payload.question,
        student_choice=payload.student_choice,
        context=payload.context,
    )

    async def event_stream():
        async for event in stream_socratic_hint(messages, preferred_tier=TUTOR_TIER):
            yield encode_sse(event)
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Live 1v1 battle arena
# ---------------------------------------------------------------------------


class ArenaRoomRequest(BaseModel):
    node_id: str
    player_id: Optional[str] = None
    player_name: Optional[str] = Field(None, max_length=40)
    limit: int = Field(8, ge=1, le=30)
    # Optional caller-chosen code (4-8 alphanumerics) for a friend invite such
    # as "TEST01"; omitted = a random 6-character code.
    room_code: Optional[str] = Field(None, min_length=4, max_length=8)


async def _arena_questions_for_node(
    session: AsyncSession, node_id: uuid.UUID, limit: int
) -> tuple[str, list[dict[str, Any]]]:
    """Collect server-gradable questions from one node. Returns (title, questions)."""
    node = await session.get(LearningNode, node_id)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"node {node_id} not found")
    challenges = (
        await session.execute(
            select(Challenge).where(Challenge.node_id == node_id).limit(limit * 3)
        )
    ).scalars().all()
    questions: list[dict[str, Any]] = []
    for challenge in challenges:
        adapted = arena_question_from_challenge(
            challenge.type, challenge.payload_json or {}, seed=str(challenge.id)
        )
        if adapted is None:
            continue
        adapted["challenge_id"] = str(challenge.id)
        questions.append(adapted)
        if len(questions) >= limit:
            break
    return node.title, questions


@app.post("/api/arena/rooms", status_code=status.HTTP_201_CREATED)
async def create_arena_room(
    payload: ArenaRoomRequest, session: AsyncSession = Depends(get_session)
) -> dict:
    """Open a duel room from a learning node's server-gradable challenges."""
    try:
        node_uuid = uuid.UUID(payload.node_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid node_id") from exc
    title, questions = await _arena_questions_for_node(session, node_uuid, payload.limit)
    try:
        room = room_manager.create_room(questions, code=payload.room_code)
    except ArenaError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {
        "room_code": room.code,
        "node_id": str(node_uuid),
        "node_title": title,
        "questions": len(questions),
        "question_seconds": QUESTION_SECONDS,
        "status": room.status,
    }


@app.get("/api/arena/rooms/{room_code}")
async def arena_room_state(room_code: str) -> dict:
    try:
        return room_manager.get(room_code).public_state()
    except ArenaError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


class ArenaRewardRequest(BaseModel):
    player_id: str


ARENA_WIN_XP = 100
ARENA_WIN_GEMS = 25


@app.post("/api/arena/rooms/{room_code}/reward")
async def arena_reward(
    room_code: str,
    payload: ArenaRewardRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Pay the victor +100 XP and 25 gems, exactly once per room.

    Anonymous duels (player ids that are not real users) still get the podium
    acknowledgement, but nothing is written to the database.
    """
    try:
        room = room_manager.get(room_code)
    except ArenaError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if room.status != "finished":
        raise HTTPException(status.HTTP_409_CONFLICT, "the duel is still running")
    ranked = sorted(room.players.values(), key=lambda player: player.score, reverse=True)
    winner = ranked[0] if ranked else None
    if winner is None or winner.player_id != payload.player_id:
        return {
            "awarded": False,
            "reason": "not the winner",
            "winner": winner.player_id if winner else None,
        }
    if room.rewarded:
        return {"awarded": False, "reason": "already paid", "xp": 0, "gems": 0}
    room.rewarded = True

    awarded = {"awarded": True, "xp": ARENA_WIN_XP, "gems": ARENA_WIN_GEMS}
    try:
        user_id = uuid.UUID(payload.player_id)
    except ValueError:
        return {**awarded, "user_id": None}
    user = await session.get(User, user_id)
    if user is None:
        return {**awarded, "user_id": None}
    user.xp_points = (user.xp_points or 0) + ARENA_WIN_XP
    user.weekly_xp = (user.weekly_xp or 0) + ARENA_WIN_XP
    user.gems = (user.gems or 0) + ARENA_WIN_GEMS
    user.league_tier = _league_for_xp(user.xp_points)
    await session.commit()
    # ``xp``/``gems`` stay the *award* amounts so the podium can render
    # "+100 XP / +25 gems"; the post-payout balances ride along separately.
    return {
        **awarded,
        "user_id": str(user.id),
        "xp_points": user.xp_points,
        "weekly_xp": user.weekly_xp,
        "gems_balance": user.gems,
        "league_tier": user.league_tier,
    }


@app.websocket("/ws/arena/{room_code}")
async def arena_socket(websocket: WebSocket, room_code: str) -> None:
    """Synchronized duel socket. Fan-out is per-connection; Redis mirrors it.

    Client -> server: ``{"type": "answer", "selected": <int>}``
    Server -> client: ``start`` / ``question`` / ``state`` / ``answer_ack`` /
    ``reveal`` / ``finish`` (all JSON with a ``type`` discriminator).
    """
    player_id = websocket.query_params.get("player_id") or str(uuid.uuid4())
    player_name = websocket.query_params.get("name") or "لاعب"
    try:
        room = room_manager.get(room_code)
    except ArenaError as exc:
        await websocket.close(code=4404, reason=str(exc))
        return

    await websocket.accept()
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    try:
        player = await room_manager.join(room, player_id, player_name, queue)
    except ArenaError as exc:
        await websocket.send_json({"type": "error", "message": str(exc)})
        await websocket.close(code=4409, reason=str(exc))
        return

    await websocket.send_json(
        {"type": "welcome", "player_id": player.player_id, "room_code": room.code}
    )
    await room_manager.broadcast(room, room.public_state())

    async def pump() -> None:
        """Every room broadcast this connection is subscribed to."""
        while True:
            message = await queue.get()
            await websocket.send_json(message)

    pump_task = asyncio.create_task(pump())
    try:
        await room_manager.maybe_start(room)
        while True:
            message = await websocket.receive_json()
            await room_manager.handle_message(room, player_id, message)
    except WebSocketDisconnect:
        pass
    except ArenaError as exc:
        logger.warning("arena %s protocol error: %s", room.code, exc)
    finally:
        pump_task.cancel()
        room_manager.leave(room, player_id)
        await room_manager.broadcast(room, room.public_state())


# ---------------------------------------------------------------------------
# Gem shop & weekly leagues
# ---------------------------------------------------------------------------


class ShopPurchaseRequest(BaseModel):
    item_id: str = Field(..., max_length=60)


@app.get("/api/shop/items")
async def shop_items() -> dict:
    """Static catalog; prices are authoritative here, never in the client."""
    return {"items": list(SHOP_ITEMS)}


@app.post("/api/users/{user_id}/shop/purchase")
async def shop_purchase(
    user_id: uuid.UUID,
    payload: ShopPurchaseRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Buy an item with gems. Applies the mechanical effect server-side."""
    item = SHOP_ITEMS_BY_ID.get(payload.item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown item {payload.item_id}")
    user = await _get_user(session, user_id)
    if user.gems < item["price"]:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            f"insufficient gems: {item['id']} costs {item['price']}, you have {user.gems}",
        )
    user.gems -= item["price"]
    inventory = dict(user.inventory or {})
    inventory[item["id"]] = int(inventory.get(item["id"], 0)) + 1
    if item["effect"] == "streak_freeze":
        user.streak_freeze_active = True
    if item.get("duration_seconds"):
        # Timestamped buff: the client (and any future scoring path) reads this
        # to double XP for the next 30 minutes.
        inventory[f"{item['id']}_until"] = (
            utcnow() + timedelta(seconds=int(item["duration_seconds"]))
        ).isoformat()
    user.inventory = inventory
    await session.commit()
    return {
        "user_id": str(user.id),
        "item": item,
        "gems": user.gems,
        "inventory": inventory,
        "streak_freeze_active": user.streak_freeze_active,
    }


def _week_key(moment: datetime) -> str:
    """ISO year-week key for the weekly league cycle (Sunday rollover)."""
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def _next_sunday_midnight(moment: datetime) -> datetime:
    """UTC midnight of the coming Sunday (the rollover moment)."""
    days_ahead = (6 - moment.weekday()) % 7  # Monday=0 ... Sunday=6
    if days_ahead == 0:
        # Today is Sunday: the *next* cycle boundary is a week out, unless we
        # are still before midnight of the current day.
        days_ahead = 7
    target = (moment + timedelta(days=days_ahead)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return target


async def _maybe_rollover_leagues(session: AsyncSession) -> bool:
    """Close the weekly league cycle once per ISO week.

    Top 5 of every tier climb, bottom 5 drop, and ``weekly_xp`` resets. The
    cycle marker lives in ``app_meta`` so the sweep is idempotent across
    workers and restarts. Returns True when a rollover just happened.
    """
    now = utcnow()
    key = _week_key(now)
    marker = await session.get(AppMeta, "league_week")
    if marker is not None and marker.value == key:
        return False

    users = (await session.execute(select(User))).scalars().all()
    by_tier: dict[int, list[User]] = {}
    for user in users:
        by_tier.setdefault(user.league_tier or 0, []).append(user)
    for _, cohort in by_tier.items():
        cohort.sort(key=lambda u: (u.weekly_xp or 0), reverse=True)
        for index, user in enumerate(cohort):
            if index < LEAGUE_PROMOTION_ZONE:
                user.league_tier = min(user.league_tier + 1, len(LEAGUE_NAMES) - 1)
            elif index >= len(cohort) - LEAGUE_RELEGATION_ZONE and len(cohort) > LEAGUE_PROMOTION_ZONE:
                user.league_tier = max(user.league_tier - 1, 0)
            user.weekly_xp = 0
    if marker is None:
        session.add(AppMeta(key="league_week", value=key))
    else:
        marker.value = key
    await session.commit()
    logger.info("weekly league rollover complete for %s", key)
    return True


@app.get("/api/leaderboards/{user_id}")
async def leaderboard(
    user_id: uuid.UUID,
    cohort_size: int = Query(LEAGUE_COHORT_SIZE, ge=5, le=200),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The caller's weekly league: same-tier classmates ranked on ``weekly_xp``."""
    await _maybe_rollover_leagues(session)
    user = await _get_user(session, user_id)
    tier = user.league_tier or 0
    cohort = (
        await session.execute(
            select(User)
            .where(User.league_tier == tier)
            .order_by(User.weekly_xp.desc(), User.id.asc())
            .limit(cohort_size)
        )
    ).scalars().all()
    rows = [
        {
            "rank": index + 1,
            "user_id": str(member.id),
            "email": member.email,
            "weekly_xp": member.weekly_xp or 0,
            "is_me": member.id == user.id,
        }
        for index, member in enumerate(cohort)
    ]
    my_rank = next((row["rank"] for row in rows if row["is_me"]), None)
    zone = "retention"
    if my_rank is not None:
        if my_rank <= LEAGUE_PROMOTION_ZONE:
            zone = "promotion"
        elif my_rank > len(rows) - LEAGUE_RELEGATION_ZONE and len(rows) > LEAGUE_PROMOTION_ZONE:
            zone = "relegation"
    now = utcnow()
    return {
        "league_tier": tier,
        "league_name": LEAGUE_NAMES[min(tier, len(LEAGUE_NAMES) - 1)],
        "league_names": list(LEAGUE_NAMES),
        "weekly_xp": user.weekly_xp or 0,
        "my_rank": my_rank,
        "cohort_size": len(rows),
        "zone": zone,
        "promotion_zone": LEAGUE_PROMOTION_ZONE,
        "relegation_zone": LEAGUE_RELEGATION_ZONE,
        "week_key": _week_key(now),
        "rollover_at": _next_sunday_midnight(now),
        "rows": rows,
    }


@app.post("/api/leaderboards/rollover")
async def rollover_leagues(session: AsyncSession = Depends(get_session)) -> dict:
    """Force the weekly rollover (also runs lazily on every leaderboard read)."""
    ran = await _maybe_rollover_leagues(session)
    return {"rolled_over": ran, "week_key": _week_key(utcnow())}


# ===========================================================================
# Google-Classroom instructor hub, virality, exam crunch, monetization & push
# ===========================================================================


class ClassroomCreateRequest(BaseModel):
    instructor_id: str
    name: str = Field(..., min_length=1, max_length=200)
    # Optional metadata shown on the classroom card + share links.
    section: Optional[str] = Field(None, max_length=120)   # e.g. "Term 1 - 2026"
    description: Optional[str] = Field(None, max_length=500)


class ClassroomJoinRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=12)
    student_id: str


class PushSubscribeRequest(BaseModel):
    user_id: str
    subscription: dict[str, Any]
    user_agent: Optional[str] = Field(None, max_length=400)


class PushUnsubscribeRequest(BaseModel):
    endpoint: str = Field(..., min_length=8, max_length=2000)


class ExamCrunchRequest(BaseModel):
    user_id: str
    # Optional override; the engine otherwise scales the paper with the material.
    target: Optional[int] = Field(None, ge=EXAM_TARGET_MIN, le=EXAM_TARGET_MAX)


class ExamSubmitRequest(BaseModel):
    user_id: str
    # challenge_id -> selected option index
    answers: dict[str, Any] = Field(default_factory=dict)


class ProActivateRequest(BaseModel):
    months: int = Field(1, ge=1, le=24)
    provider: str = Field("manual", max_length=40)
    external_id: Optional[str] = Field(None, max_length=200)


PRO_PRICE_USD_CENTS = int(os.getenv("PRO_PRICE_USD_CENTS", "100"))
PRO_PERIOD_DAYS = int(os.getenv("PRO_PERIOD_DAYS", "30"))
ADMOB_REWARD_GEMS = int(os.getenv("ADMOB_REWARD_GEMS", "25"))
ADMOB_SSV_SECRET = os.getenv("ADMOB_SSV_SECRET", "").strip()
ADMOB_SSV_PUBLIC_KEY = os.getenv("ADMOB_SSV_PUBLIC_KEY", "").strip()


def _uuid_or_422(raw: Any, field: str = "user_id") -> uuid.UUID:
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"{field} must be a UUID"
        ) from exc


def _normalize_enrollment_code(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (raw or "").strip().upper())[:6]


def _course_summary(course: Course, *, enrolled: int = 0, instructor_email: str | None = None) -> dict:
    return {
        "id": str(course.id),
        "name": course.name,
        "is_classroom": bool(course.is_classroom),
        "enrollment_code": course.enrollment_code,
        "section": course.section,
        "description": course.description,
        "instructor_id": str(course.instructor_id) if course.instructor_id else None,
        "instructor_email": instructor_email,
        "share_code": _format_share_code(course.share_code) if course.share_code else None,
        "enrolled_count": enrolled,
        "created_at": course.created_at,
    }


async def _require_instructor(
    session: AsyncSession, course: Course, instructor_id: Optional[str]
) -> uuid.UUID:
    """403 unless the caller owns this classroom (roster data is private)."""
    if not course.is_classroom:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not a classroom")
    if not instructor_id:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "instructor_id is required"
        )
    caller = _uuid_or_422(instructor_id, "instructor_id")
    if course.instructor_id != caller and course.user_id != caller:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "only the classroom instructor can read this dashboard",
        )
    return caller


async def _classroom_progress(
    session: AsyncSession, course_id: uuid.UUID, student_id: uuid.UUID
) -> dict:
    """One student's progress inside one course (accuracy + completion)."""
    rows = (
        await session.execute(
            select(FsrsCard, Challenge.id)
            .join(Challenge, Challenge.id == FsrsCard.challenge_id)
            .join(LearningNode, LearningNode.id == Challenge.node_id)
            .join(Lecture, Lecture.id == LearningNode.lecture_id)
            .where(Lecture.course_id == course_id, FsrsCard.user_id == student_id)
        )
    ).all()
    total_available = await session.scalar(
        select(func.count())
        .select_from(Challenge)
        .join(LearningNode, LearningNode.id == Challenge.node_id)
        .join(Lecture, Lecture.id == LearningNode.lecture_id)
        .where(Lecture.course_id == course_id)
    )
    answered = [card for card, _cid in rows if (card.reps or 0) > 0]
    ratings = [card.last_rating for card in answered if card.last_rating is not None]
    accuracy = round(sum(1 for r in ratings if r >= 3) / len(ratings), 4) if ratings else 0.0
    total = int(total_available or 0)
    return {
        "cards_total": total,
        "answered": len(answered),
        "completion_percent": round(len(answered) / total * 100, 1) if total else 0.0,
        "accuracy": accuracy,
    }


@app.post("/api/classrooms", status_code=status.HTTP_201_CREATED)
async def create_classroom(payload: ClassroomCreateRequest, session: AsyncSession = Depends(get_session)) -> dict:
    """Open a Google-Classroom-style course with a 6-char enrollment code.

    The creator is promoted to ``role='instructor'``; the course is owned by
    them, flagged ``is_classroom`` and addressable by the code students share.
    """
    instructor_id = _uuid_or_422(payload.instructor_id, "instructor_id")
    user = await _get_user(session, instructor_id)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "name is required")

    user.role = "instructor"
    try:
        enrollment_code = await unique_enrollment_code(session)
    except RuntimeError as exc:  # pragma: no cover - astronomical collision
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    course = Course(
        user_id=instructor_id,
        name=name[:200],
        lms_type="classroom",
        is_classroom=True,
        enrollment_code=enrollment_code,
        instructor_id=instructor_id,
        share_code=await _unique_share_code(session),
        category="academic",
        section=(payload.section or "")[:120] or None,
        description=(payload.description or "")[:500] or None,
    )
    session.add(course)
    await session.flush()
    return {
        **_course_summary(course, instructor_email=user.email),
        "role": user.role,
    }


@app.get("/api/classrooms")
async def list_classrooms(
    user_id: uuid.UUID = Query(...), session: AsyncSession = Depends(get_session)
) -> dict:
    """Every classroom this account teaches or is enrolled in."""
    await _get_user(session, user_id)
    taught = await classrooms_for_instructor(session, user_id)
    enrolled = await courses_for_student(session, user_id)
    taught_out = []
    for course in taught:
        taught_out.append(
            _course_summary(
                course, enrolled=len(await enrolled_student_ids(session, course.id))
            )
        )
    return {
        "user_id": str(user_id),
        "taught": taught_out,
        "enrolled": [_course_summary(course) for course in enrolled],
    }


@app.post("/api/classrooms/join", status_code=status.HTTP_201_CREATED)
async def join_classroom(payload: ClassroomJoinRequest, session: AsyncSession = Depends(get_session)) -> dict:
    """Enroll a student with the class code (idempotent)."""
    student_id = _uuid_or_422(payload.student_id, "student_id")
    await _get_user(session, student_id)
    code = _normalize_enrollment_code(payload.code)
    if not code:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid class code")

    course = (
        await session.execute(
            select(Course).where(Course.enrollment_code == code).limit(1)
        )
    ).scalar_one_or_none()
    if course is None or not course.is_classroom:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no classroom with code {code}")

    existing = await enrollment_for(session, course.id, student_id)
    already = existing is not None
    if not already:
        session.add(Enrollment(course_id=course.id, student_id=student_id))
        await session.flush()
        # Tell the instructor (and any listening roster screen) that the class
        # grew, so the dashboard updates live like the student side does.
        if course.instructor_id:
            await dispatch(
                session,
                course.instructor_id,
                kind="classroom",
                title=f"👋 طالب جديد انضم إلى {course.name}",
                body=f"انضم طالب جديد بكود الفصل {code}.",
                url=f"/instructor/{course.id}",
                course_id=course.id,
            )
        await classroom_hub.publish(
            course.id,
            {
                "type": "student_joined",
                "student_id": str(student_id),
                "enrolled_count": len(await enrolled_student_ids(session, course.id)),
            },
        )

    instructor_email = None
    if course.instructor_id:
        instructor = await session.get(User, course.instructor_id)
        instructor_email = instructor.email if instructor else None
    return {
        **_course_summary(
            course,
            enrolled=len(await enrolled_student_ids(session, course.id)),
            instructor_email=instructor_email,
        ),
        "already_enrolled": already,
    }


@app.get("/api/classrooms/{course_id}/roster")
async def classroom_roster(
    course_id: uuid.UUID,
    instructor_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Class roster with live per-student stats (instructor-only)."""
    course = await session.get(Course, course_id)
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "course not found")
    await _require_instructor(session, course, str(instructor_id))

    student_ids = await enrolled_student_ids(session, course_id)
    roster: list[dict] = []
    total_challenges = await session.scalar(
        select(func.count())
        .select_from(Challenge)
        .join(LearningNode, LearningNode.id == Challenge.node_id)
        .join(Lecture, Lecture.id == LearningNode.lecture_id)
        .where(Lecture.course_id == course_id)
    )
    for student_id in student_ids:
        student = await session.get(User, student_id)
        if student is None:
            continue
        progress = await _classroom_progress(session, course_id, student_id)
        roster.append(
            {
                "student_id": str(student.id),
                "email": student.email,
                "xp_points": student.xp_points,
                "streak_count": student.streak_count,
                "last_active_at": student.last_active_at,
                "completion_percent": progress["completion_percent"],
                "accuracy": progress["accuracy"],
                "answered": progress["answered"],
            }
        )
    roster.sort(key=lambda row: (row["completion_percent"], row["accuracy"]), reverse=True)
    return {
        "course_id": str(course_id),
        "name": course.name,
        "enrollment_code": course.enrollment_code,
        "challenges_total": int(total_challenges or 0),
        "student_count": len(roster),
        "roster": roster,
    }


@app.get("/api/classrooms/{course_id}/insights")
async def classroom_insights(
    course_id: uuid.UUID,
    instructor_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Lecture performance: aggregated failure rates and the top failing concepts."""
    course = await session.get(Course, course_id)
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "course not found")
    await _require_instructor(session, course, str(instructor_id))

    rows = (
        await session.execute(
            select(
                Lecture.id,
                Lecture.title,
                LearningNode.title,
                FsrsCard.last_rating,
            )
            .select_from(Lecture)
            .join(LearningNode, LearningNode.lecture_id == Lecture.id)
            .join(Challenge, Challenge.node_id == LearningNode.id)
            .join(FsrsCard, FsrsCard.challenge_id == Challenge.id)
            .where(Lecture.course_id == course_id)
        )
    ).all()

    attempts = len(rows)
    correct = sum(1 for _l, _lt, _nt, rating in rows if rating is not None and rating >= 3)
    lectures: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []
    for lecture_id, lecture_title, node_title, rating in rows:
        key = str(lecture_id)
        bucket = lectures.setdefault(
            key,
            {
                "lecture_id": key,
                "lecture_title": lecture_title,
                "attempts": 0,
                "correct": 0,
                "struggling": {},
            },
        )
        bucket["attempts"] += 1
        if rating is not None and rating >= 3:
            bucket["correct"] += 1
        failures.append({"node_title": node_title, "correct": rating is not None and rating >= 3})
        if rating is None or rating < 3:
            bucket["struggling"][node_title] = bucket["struggling"].get(node_title, 0) + 1

    enriched = []
    for bucket in lectures.values():
        severity = sorted(bucket.pop("struggling").items(), key=lambda kv: kv[1], reverse=True)
        enriched.append(
            {
                **bucket,
                "accuracy": round(bucket["correct"] / bucket["attempts"], 4)
                if bucket["attempts"]
                else 0.0,
                "top_struggling": [
                    {"concept": name, "failures": count} for name, count in severity[:3]
                ],
            }
        )
    enriched.sort(key=lambda item: item["accuracy"])
    return {
        "course_id": str(course_id),
        "attempts": attempts,
        "accuracy": round(correct / attempts, 4) if attempts else 0.0,
        "lectures": enriched,
        "student_count": len(await enrolled_student_ids(session, course_id)),
    }


@app.get("/api/classrooms/{course_id}/grades.csv")
async def classroom_grades_csv(
    course_id: uuid.UUID,
    instructor_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
) -> PlainTextResponse:
    """1-click CSV export of the roster's scores and completion milestones."""
    course = await session.get(Course, course_id)
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "course not found")
    await _require_instructor(session, course, str(instructor_id))

    student_ids = await enrolled_student_ids(session, course_id)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "email",
            "xp_points",
            "streak_count",
            "challenges_answered",
            "challenges_total",
            "completion_percent",
            "accuracy",
            "last_active_at",
        ]
    )
    for student_id in student_ids:
        student = await session.get(User, student_id)
        if student is None:
            continue
        progress = await _classroom_progress(session, course_id, student_id)
        writer.writerow(
            [
                student.email,
                student.xp_points or 0,
                student.streak_count or 0,
                progress["answered"],
                progress["cards_total"],
                f"{progress['completion_percent']:.1f}",
                f"{progress['accuracy']:.4f}",
                student.last_active_at.isoformat() if student.last_active_at else "",
            ]
        )
    filename = re.sub(r"[^A-Za-z0-9_-]+", "-", course.name).strip("-") or "classroom"
    return PlainTextResponse(
        buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}-grades.csv"'},
    )


@app.get("/api/classrooms/{course_id}/updates")
async def classroom_updates(
    course_id: uuid.UUID,
    student_id: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """SSE stream: the student dashboard updates its chapter tree live.

    Emits ``connected`` first, then every ``lecture_added`` / ``student_joined``
    event for this classroom as the instructor posts material.
    """
    course = await session.get(Course, course_id)
    if course is None or not course.is_classroom:
        # A personal course has no classroom updates; answering with an endless
        # SSE stream would hang the client forever.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "classroom not found")
    client_id = student_id or f"anon-{uuid.uuid4()}"
    return StreamingResponse(
        classroom_hub.stream(course_id, client_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/classrooms/{course_id}/publish")
async def publish_classroom_update(
    course_id: uuid.UUID,
    instructor_id: uuid.UUID = Query(...),
    payload: Optional[dict] = None,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Manually re-broadcast (e.g. after renaming chapters) to every student."""
    course = await session.get(Course, course_id)
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "course not found")
    await _require_instructor(session, course, str(instructor_id))
    body = payload or {}
    delivered = await classroom_hub.publish(
        course_id,
        {
            "type": str(body.get("type") or "classroom_updated"),
            "lecture_title": body.get("lecture_title"),
            "message": body.get("message"),
        },
    )
    return {"course_id": str(course_id), "delivered": delivered}


# ---------------------------------------------------------------------------
# Viral class sharing (peer-to-peer, no LLM re-run) + creator royalties
# ---------------------------------------------------------------------------


async def _shared_course_or_404(session: AsyncSession, share_code: str) -> Course:
    """Resolve a join/share code to a course.

    Accepts either kind of code: the peer-share ``share_code`` (``CL-4F8A2B``
    or bare body) or a classroom ``enrollment_code`` (6 chars). Both columns
    are queried in one shot so ``/join/CS202A`` (enrollment code) and
    ``/join/CL-4F8A2B`` (share code) resolve the same way. Unknown codes
    return the friendly Arabic 404 the /join landing page displays.
    """
    code = _normalize_share_code(share_code)
    course = (
        await session.execute(
            select(Course)
            .where(or_(Course.share_code == code, Course.enrollment_code == code))
            .limit(1)
        )
    ).scalar_one_or_none()
    if course is None:
        # Second chance: a user-typed enrollment code may carry punctuation or
        # lowercase ("cs-202a"); normalize it like /api/classrooms/join does.
        loose = _normalize_enrollment_code(share_code)
        if loose and loose != code:
            course = (
                await session.execute(
                    select(Course)
                    .where(or_(Course.share_code == loose, Course.enrollment_code == loose))
                    .limit(1)
                )
            ).scalar_one_or_none()
    if course is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "رمز المشاركة غير صالح أو انتهت صلاحيته"
        )
    return course


async def _chapter_list_of(session: AsyncSession, course: Course) -> list[str]:
    """Ordered lecture titles of a course — the preview chapter list."""
    rows = await session.execute(
        select(Lecture.title)
        .where(Lecture.course_id == course.id)
        .order_by(Lecture.created_at)
    )
    return [title for (title,) in rows.all()]


@app.get("/api/join/{share_code}")
async def preview_shared_course(share_code: str, session: AsyncSession = Depends(get_session)) -> dict:
    """Public preview for the ``/join/{share_code}`` landing page.

    Resolves both share codes (``CL-...``) and classroom enrollment codes, and
    returns course metadata, the chapter list and instructor/creator info.
    Unknown codes get the friendly Arabic 404 the landing page renders.
    """
    course = await _shared_course_or_404(session, share_code)
    owner = await session.get(User, course.user_id)
    instructor = (
        await session.get(User, course.instructor_id)
        if course.instructor_id is not None
        else None
    )
    lectures = (
        await session.execute(
            select(func.count()).select_from(Lecture).where(Lecture.course_id == course.id)
        )
    ).scalar()
    challenges = (
        await session.execute(
            select(func.count())
            .select_from(Challenge)
            .join(LearningNode, LearningNode.id == Challenge.node_id)
            .join(Lecture, Lecture.id == LearningNode.lecture_id)
            .where(Lecture.course_id == course.id)
        )
    ).scalar()
    return {
        "course_id": str(course.id),
        "name": course.name,
        "category": course.category,
        "institution": course.institution,
        "major": course.major,
        "owner_handle": (owner.email.split("@")[0] if owner else "campuslingo"),
        "creator_earnings": (owner.creator_gems if owner else 0),
        "instructor_handle": (
            instructor.email.split("@")[0]
            if instructor
            else (owner.email.split("@")[0] if owner else None)
        ),
        "enrollment_code": (
            _normalize_enrollment_code(course.enrollment_code)
            if (course.is_classroom and course.enrollment_code)
            else None
        ),
        "chapters": await _chapter_list_of(session, course),
        "lectures": int(lectures or 0),
        "challenges": int(challenges or 0),
        "downloads_count": course.downloads_count or 0,
        "is_classroom": bool(course.is_classroom),
    }


@app.post("/api/join/{share_code}", status_code=status.HTTP_201_CREATED)
async def join_shared_course(
    share_code: str,
    payload: dict,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Join a course by its share or enrollment code.

    * Classroom code → enroll the student into the instructor's class (idempotent).
    * Peer share code → deep-clone the course into the caller's account — no
      inference is re-triggered.
    """
    user_id = _uuid_or_422(payload.get("user_id"))
    await _get_user(session, user_id)
    source = await _shared_course_or_404(session, share_code)

    # A classroom code enrolls (same flow as POST /api/classrooms/join) instead
    # of cloning, so the student follows the instructor's live syllabus.
    if source.is_classroom:
        existing = await enrollment_for(session, source.id, user_id)
        already = existing is not None
        if not already:
            session.add(Enrollment(course_id=source.id, student_id=user_id))
            await session.flush()
            if source.instructor_id:
                await dispatch(
                    session,
                    source.instructor_id,
                    kind="classroom",
                    title=f"👋 طالب جديد انضم إلى {source.name}",
                    body="انضم طالب جديد عبر رابط المشاركة.",
                    url=f"/instructor/{source.id}",
                    course_id=source.id,
                )
            await classroom_hub.publish(
                source.id,
                {
                    "type": "student_joined",
                    "student_id": str(user_id),
                    "enrolled_count": len(await enrolled_student_ids(session, source.id)),
                },
            )
        instructor_email = None
        if source.instructor_id:
            instructor = await session.get(User, source.instructor_id)
            instructor_email = instructor.email if instructor else None
        return {
            "course_id": str(source.id),
            "name": source.name,
            "joined": True,
            "joined_kind": "classroom",
            "already_enrolled": already,
            "is_classroom": True,
            "lectures_cloned": 0,
            "nodes_cloned": 0,
            "challenges_cloned": 0,
            "cards_seeded": 0,
            "source_course_id": str(source.id),
            "instructor_email": instructor_email,
        }

    result = await clone_course_tree(session, source, user_id, seed_cards=True)
    source.downloads_count = (source.downloads_count or 0) + 1

    # Tell the creator their material travelled — the viral loop's reward beat.
    if source.user_id != user_id:
        await dispatch(
            session,
            source.user_id,
            kind="classroom",
            title=f"🚀 انضم طالب إلى «{source.name}»",
            body=f"قام زميل بنسخ مقررك — وستكسب {CREATOR_ROYALTY_GEMS} جوهرة لكل تحدي يكمله.",
            url=f"/profile",
            course_id=source.id,
        )
    return {**result, "source_course_id": str(source.id), "name": source.name}


# ---------------------------------------------------------------------------
# Exam Crunch: cheatsheet + diagnostic timed mock exam
# ---------------------------------------------------------------------------


async def _course_exam_candidates(
    session: AsyncSession, course_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Every gradable challenge in a course, tagged with its chapter."""
    rows = (
        await session.execute(
            select(
                Challenge.id,
                Challenge.type,
                Challenge.payload_json,
                LearningNode.id,
                LearningNode.title,
                Lecture.title,
            )
            .select_from(Challenge)
            .join(LearningNode, LearningNode.id == Challenge.node_id)
            .join(Lecture, Lecture.id == LearningNode.lecture_id)
            .where(Lecture.course_id == course_id)
            .order_by(Lecture.created_at, LearningNode.order_index)
        )
    ).all()
    return [
        {
            "challenge_id": str(challenge_id),
            "type": ctype,
            "payload_json": payload or {},
            "node_id": str(node_id),
            "node_title": node_title,
            "chapter": lecture_title,
        }
        for challenge_id, ctype, payload, node_id, node_title, lecture_title in rows
    ]


class _DigestChunk:
    """Tiny adapter so ``build_digest`` can read .title/.content attributes."""

    def __init__(self, title: str, content: str) -> None:
        self.title = title
        self.content = content


async def _course_digest(session: AsyncSession, course_id: uuid.UUID) -> str:
    """Chapter-by-chapter text digest used for the cheatsheet prompt."""
    rows = (
        await session.execute(
            select(LearningNode.title, LearningNode.content_summary, Lecture.title)
            .select_from(LearningNode)
            .join(Lecture, Lecture.id == LearningNode.lecture_id)
            .where(Lecture.course_id == course_id)
            .order_by(Lecture.created_at, LearningNode.order_index)
        )
    ).all()
    # build_digest reads chunk.title/chunk.content; passing pre-rendered
    # strings produced '### <built-in method title of str object>' garbage.
    chunks = [
        _DigestChunk(f"{lecture_title} — {node_title}", summary or "")
        for node_title, summary, lecture_title in rows
    ]
    return build_digest(chunks)


@app.post("/api/courses/{course_id}/exam-crunch", status_code=status.HTTP_201_CREATED)
async def exam_crunch(
    course_id: uuid.UUID,
    payload: ExamCrunchRequest,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Build the crunch sheet and a diagnostic timed mock exam for a course.

    * An ACTIVE exam for this user+course is returned as-is (``200 OK``) so a
      double-tap or a page refresh resumes it instead of raising 409.
    * A course without gradable material answers ``400`` with a friendly
      ``insufficient_content`` body — never an unhandled 409.
    """
    user_id = _uuid_or_422(payload.user_id)
    await _get_user(session, user_id)
    course = await session.get(Course, course_id)
    if course is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "course not found")

    # Cached beat: an already-active exam resumes rather than conflicting.
    active = (
        await session.execute(
            select(ExamSession)
            .where(
                ExamSession.course_id == course_id,
                ExamSession.user_id == user_id,
                ExamSession.status == "active",
            )
            .order_by(ExamSession.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if active is not None and (active.questions or []):
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            # jsonable_encoder handles the raw started_at datetime that
            # JSONResponse's json.dumps cannot serialize.
            content=jsonable_encoder({
                "exam_session_id": str(active.id),
                "course_id": str(course_id),
                "course_name": course.name,
                "duration_seconds": active.duration_seconds,
                "question_count": len(active.questions or []),
                "chapters": sorted(
                    {str(q.get("chapter") or "") for q in (active.questions or [])}
                ),
                "questions": public_exam_questions(active.questions or []),
                "cheatsheet": active.cheatsheet,
                "cheatsheet_source": "cached",
                "cheatsheet_error": None,
                "target_range": [EXAM_TARGET_MIN, EXAM_TARGET_MAX],
                "started_at": active.started_at,
                "cached": True,
            }),
        )

    candidates = await _course_exam_candidates(session, course_id)
    questions = assemble_mock_exam(candidates, target=payload.target)
    if not questions:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "status": "insufficient_content",
                "message": "المقرر يحتاج إلى محاضرة واحدة مكتملة على الأقل لتوليد الامتحان الشامل",
            },
        )

    digest = await _course_digest(session, course_id)
    cheatsheet = await synthesize_cheatsheet(digest, course.name)

    session_row = ExamSession(
        user_id=user_id,
        course_id=course_id,
        status="active",
        duration_seconds=EXAM_DURATION_SECONDS,
        questions=questions,
        answers={},
        cheatsheet=cheatsheet["markdown"],
        total=len(questions),
    )
    session.add(session_row)
    await session.flush()

    return {
        "exam_session_id": str(session_row.id),
        "course_id": str(course_id),
        "course_name": course.name,
        "duration_seconds": EXAM_DURATION_SECONDS,
        "question_count": len(questions),
        "chapters": sorted({str(q.get("chapter") or "") for q in questions}),
        "questions": public_exam_questions(questions),
        "cheatsheet": cheatsheet["markdown"],
        "cheatsheet_source": cheatsheet["source"],
        "cheatsheet_error": cheatsheet["error"],
        "target_range": [EXAM_TARGET_MIN, EXAM_TARGET_MAX],
        "started_at": session_row.started_at,
    }


async def _exam_or_404(
    session: AsyncSession, exam_id: uuid.UUID, user_id: uuid.UUID
) -> ExamSession:
    row = await session.get(ExamSession, exam_id)
    if row is None or row.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "exam session not found")
    return row


def _seconds_remaining(row: ExamSession) -> int:
    started = row.started_at
    if started is not None and started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if started is None:
        return row.duration_seconds
    elapsed = (utcnow() - started).total_seconds()
    return max(0, int(row.duration_seconds - elapsed))


@app.get("/api/exam-sessions/{exam_id}")
async def get_exam_session(
    exam_id: uuid.UUID,
    user_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The exam paper (answers stripped) plus the synchronized countdown."""
    row = await _exam_or_404(session, exam_id, user_id)
    return {
        "exam_session_id": str(row.id),
        "course_id": str(row.course_id),
        "status": row.status,
        "total": row.total,
        "duration_seconds": row.duration_seconds,
        "seconds_remaining": _seconds_remaining(row),
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "questions": public_exam_questions(row.questions or []),
        "cheatsheet": row.cheatsheet,
        "score": row.score,
        "grade": row.grade,
        "readiness": row.readiness,
    }


@app.post("/api/exam-sessions/{exam_id}/submit")
async def submit_exam(
    exam_id: uuid.UUID,
    payload: ExamSubmitRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Grade a submitted mock exam: letter grade, readiness and the heatmap."""
    user_id = _uuid_or_422(payload.user_id)
    user = await _get_user(session, user_id)
    row = await _exam_or_404(session, exam_id, user_id)
    if row.status != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "this exam was already submitted")

    result = grade_exam(row.questions or [], payload.answers)
    row.answers = payload.answers
    row.score = result["score"]
    row.total = result["total"]
    row.grade = result["grade"]
    row.readiness = result["readiness"]
    row.status = "graded"
    row.finished_at = utcnow()

    # Diagnostic XP: smaller than a live drill so the exam cannot replace
    # spaced repetition, but large enough to make the crunch session count.
    xp = result["score"] * EXAM_XP_PER_CORRECT
    gems = result["score"] * EXAM_GEMS_PER_CORRECT
    user.xp_points = (user.xp_points or 0) + xp
    user.weekly_xp = (user.weekly_xp or 0) + xp
    user.gems = (user.gems or 0) + gems
    user.league_tier = _league_for_xp(user.xp_points)
    await session.flush()

    return {
        "exam_session_id": str(row.id),
        "course_id": str(row.course_id),
        "xp_awarded": xp,
        "gems_awarded": gems,
        "xp_points": user.xp_points,
        "gems": user.gems,
        **result,
    }


@app.get("/api/exam-sessions/{exam_id}/summary")
async def exam_summary(
    exam_id: uuid.UUID,
    user_id: uuid.UUID = Query(...),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Post-exam report: grade, readiness, chapter heatmap and weak spots."""
    row = await _exam_or_404(session, exam_id, user_id)
    if row.status != "graded":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "submit the exam before opening the summary"
        )
    course = await session.get(Course, row.course_id)
    result = grade_exam(row.questions or [], row.answers or {})
    return {
        "exam_session_id": str(row.id),
        "course_id": str(row.course_id),
        "course_name": course.name if course else "",
        "cheatsheet": row.cheatsheet,
        "duration_seconds": row.duration_seconds,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "weak_spot_count": len(result["weak_spots"]),
        **result,
    }


# ---------------------------------------------------------------------------
# Web Push + notification inbox
# ---------------------------------------------------------------------------


@app.get("/api/push/public-key")
async def push_public_key() -> dict:
    """VAPID public key for ``pushManager.subscribe`` (null when unconfigured)."""
    return {
        "public_key": VAPID_PUBLIC_KEY or None,
        "push_available": push_available(),
        "configured": bool(VAPID_PUBLIC_KEY and VAPID_PUBLIC_KEY.strip()),
    }


@app.post("/api/push/subscribe", status_code=status.HTTP_201_CREATED)
async def push_subscribe(payload: PushSubscribeRequest, session: AsyncSession = Depends(get_session)) -> dict:
    """Store (or refresh) a browser/PWA push endpoint for this user."""
    user_id = _uuid_or_422(payload.user_id)
    await _get_user(session, user_id)
    endpoint = str(payload.subscription.get("endpoint") or "").strip()
    if not endpoint:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "subscription.endpoint is required")
    keys = payload.subscription.get("keys") or {}

    existing = (
        await session.execute(
            select(PushSubscription).where(PushSubscription.endpoint == endpoint).limit(1)
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = PushSubscription(
            user_id=user_id,
            endpoint=endpoint,
            keys=keys if isinstance(keys, dict) else {},
            user_agent=(payload.user_agent or None),
        )
        session.add(existing)
    else:  # a re-subscribe can move the endpoint to another account
        existing.user_id = user_id
        existing.keys = keys if isinstance(keys, dict) else existing.keys
        existing.user_agent = payload.user_agent or existing.user_agent
    await session.flush()
    return {
        "subscription_id": str(existing.id),
        "user_id": str(user_id),
        "push_available": push_available(),
    }


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(payload: PushUnsubscribeRequest, session: AsyncSession = Depends(get_session)) -> dict:
    existing = (
        await session.execute(
            select(PushSubscription)
            .where(PushSubscription.endpoint == payload.endpoint.strip())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        await session.delete(existing)
    return {"removed": existing is not None}


@app.get("/api/users/{user_id}/notifications")
async def list_notifications(
    user_id: uuid.UUID,
    limit: int = Query(30, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The in-app inbox (always populated, even when Web Push is unavailable)."""
    await _get_user(session, user_id)
    rows = (
        await session.execute(
            select(Notification)
            .where(Notification.user_id == user_id)
            .order_by(Notification.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    unread = await session.scalar(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
    )
    return {
        "user_id": str(user_id),
        "unread": int(unread or 0),
        "push_available": push_available(),
        "items": [
            {
                "id": str(row.id),
                "kind": row.kind,
                "title": row.title,
                "body": row.body,
                "url": row.url,
                "push_status": row.push_status,
                "read": row.read_at is not None,
                "created_at": row.created_at,
            }
            for row in rows
        ],
    }


@app.post("/api/notifications/{notification_id}/read")
async def mark_notification_read(
    notification_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> dict:
    row = await session.get(Notification, notification_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "notification not found")
    if row.read_at is None:
        row.read_at = utcnow()
    await session.flush()
    return {"id": str(row.id), "read": True}


@app.post("/api/retention/sweep")
async def run_retention_sweep(session: AsyncSession = Depends(get_session)) -> dict:
    """Run the streak-saver / FSRS-decay sweep now (ops + test trigger)."""
    counts = await retention_sweep(session)
    return {"ok": True, **counts}


# ---------------------------------------------------------------------------
# Monetization: entitlements, $1 Pro Pass, AdMob rewarded SSV
# ---------------------------------------------------------------------------


def _entitlements(user: User) -> dict:
    pro = user.pro_active
    return {
        "user_id": str(user.id),
        "role": user.role,
        "is_pro": bool(user.is_pro),
        "pro_active": pro,
        "pro_expires_at": user.pro_expires_at,
        "show_ads": not pro,
        "unlimited_uploads": pro,
        "free_daily_uploads": FREE_DAILY_UPLOADS,
        "creator_gems": user.creator_gems or 0,
        "price_usd_cents": PRO_PRICE_USD_CENTS,
        "period_days": PRO_PERIOD_DAYS,
        "perks": [
            "zero_ads" if pro else "ads_enabled",
            "priority_processing" if pro else "standard_processing",
            "offline_cheatsheets" if pro else "online_cheatsheets_only",
        ],
    }


@app.get("/api/users/{user_id}/entitlements")
async def get_entitlements(
    user_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> dict:
    """What the client may show: ads, upload limits and offline perks."""
    user = await _get_user(session, user_id)
    return _entitlements(user)


@app.post("/api/users/{user_id}/pro")
async def activate_pro(
    user_id: uuid.UUID,
    payload: ProActivateRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Activate/extend the $1 Pro Pass.

    This is the **entitlement hook**: a real checkout (Stripe / Play Billing)
    calls it from its webhook once payment is captured. It is deliberately not
    a self-serve grant in production — put it behind the provider's signature
    (see ``/api/monetization/admob-ssv`` for the verification pattern).
    """
    user = await _get_user(session, user_id)
    now = utcnow()
    base = user.pro_expires_at
    if base is not None and base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    start = base if (base is not None and base > now) else now
    user.pro_expires_at = start + timedelta(days=PRO_PERIOD_DAYS * payload.months)
    user.is_pro = True
    await session.flush()
    return {
        **_entitlements(user),
        "activated": True,
        "provider": payload.provider,
        "external_id": payload.external_id,
        "months": payload.months,
    }


@app.post("/api/monetization/admob-ssv")
async def admob_ssv(request: Request, session: AsyncSession = Depends(get_session)) -> dict:
    """Google AdMob Server-Side Verification callback for rewarded videos.

    AdMob GETs this URL with the reward payload plus ``signature``/``key_id``.
    Verification is **fail-closed**: with neither ``ADMOB_SSV_SECRET`` (HMAC of
    the sorted query string, for self-hosted testing) nor ``ADMOB_SSV_PUBLIC_KEY``
    (Google's rotating public key, which additionally needs the optional
    ``cryptography`` package) configured, the endpoint returns 503 and grants
    nothing. The credited user id travels in ``custom_data``.
    """
    params = dict(request.query_params)
    signature = params.pop("signature", "") or params.pop("sig", "")
    key_id = params.get("key_id", "")
    if not signature:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing signature")

    # The signed payload is every query parameter except the signature itself,
    # serialised as sorted ``key=value`` pairs — so ``key_id`` stays in the
    # canonical string (it only selects which public key to load).
    canonical = "&".join(f"{k}={params[k]}" for k in sorted(params))

    verified = False
    if ADMOB_SSV_SECRET:
        expected = hmac.new(
            ADMOB_SSV_SECRET.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        verified = hmac.compare_digest(expected, signature)
    elif ADMOB_SSV_PUBLIC_KEY:
        try:  # pragma: no cover - requires the optional cryptography package
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
            import base64 as _b64

            public_key = serialization.load_pem_public_key(
                _ADMOB_KEY_PEM_CACHE.get(key_id, ADMOB_SSV_PUBLIC_KEY).encode("utf-8")
            )
            public_key.verify(  # type: ignore[union-attr]
                _b64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)),
                canonical.encode("utf-8"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            verified = True
        except ImportError:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "AdMob public-key verification requires the 'cryptography' package",
            )
        except Exception:  # noqa: BLE001 - any crypto failure is a rejection
            verified = False
    else:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "AdMob SSV is not configured (set ADMOB_SSV_SECRET or ADMOB_SSV_PUBLIC_KEY)",
        )

    if not verified:
        logger.warning("AdMob SSV rejected: bad signature (key_id=%s)", key_id)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid AdMob signature")

    custom = params.get("custom_data") or params.get("user_id") or ""
    try:
        user_id = uuid.UUID(custom)
    except (TypeError, ValueError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "custom_data must be a user id")
    user = await _get_user(session, user_id)

    try:
        reward = int(params.get("reward_amount") or ADMOB_REWARD_GEMS)
    except ValueError:
        reward = ADMOB_REWARD_GEMS
    reward = max(0, min(reward, 500))
    user.gems = (user.gems or 0) + reward
    inventory = dict(user.inventory or {})
    inventory["rewarded_ads_watched"] = int(inventory.get("rewarded_ads_watched", 0)) + 1
    inventory["bonus_gems_from_ads"] = int(inventory.get("bonus_gems_from_ads", 0)) + reward
    user.inventory = inventory
    await dispatch(
        session,
        user.id,
        kind="classroom",
        title=f"🎬 +{reward} جوهرة من إعلان مكافأة",
        body="شكراً لمشاهدة الفيديو — الجواهر أضيفت إلى رصيدك.",
        url="/shop",
    )
    await session.flush()
    return {
        "ok": True,
        "user_id": str(user.id),
        "gems_awarded": reward,
        "gems": user.gems,
        "transaction_id": params.get("transaction_id"),
        "ad_unit": params.get("ad_unit"),
    }


# PEM keys for AdMob's rotating signer, keyed by key_id (optional).
_ADMOB_KEY_PEM_CACHE: dict[str, str] = {}
EXAM_XP_PER_CORRECT = int(os.getenv("EXAM_XP_PER_CORRECT", "5"))
EXAM_GEMS_PER_CORRECT = int(os.getenv("EXAM_GEMS_PER_CORRECT", "1"))


if __name__ == "__main__":  # pragma: no cover - dev entrypoint
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=bool(os.getenv("RELOAD")),
    )
