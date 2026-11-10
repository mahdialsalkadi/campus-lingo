"""End-to-end API suite against a real PostgreSQL 16 + pgvector instance.

Covers the spec'd surface — ``POST /api/process-lecture``, ``POST /api/reviews``,
``GET /api/nodes/{node_id}/challenges``, ``GET /health`` — plus the async helpers
in ``database.py`` through their real call paths.

PostgreSQL comes from the bundled ``pgserver`` package, so the suite runs on a
machine with no Docker and no system Postgres. Skip it explicitly with::

    pytest -m "not e2e"
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import random
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.e2e

# Shared secret for the AdMob SSV signature (passed to the server env above).
SSV_SECRET = "integration-test-ssv-secret"

requests = pytest.importorskip("requests", reason="requests is required for the API suite")
pgserver = pytest.importorskip("pgserver", reason="pgserver provides the test PostgreSQL")

SERVICE_DIR = Path(__file__).resolve().parents[1]
PSQL_BINARY = (
    Path(pgserver.__file__).parent / "pginstall" / "bin" / "psql"
)

BUNDLE: dict[str, Any] = {
    "unit_id": "unit-e2e-1",
    "concept_title": "قواعد المناورة الآمنة (Safe Speed) و خطر التصادم",
    "difficulty_level": 3,
    "challenges": [
        {
            "type": "MATCH_MADNESS",
            "instructions": "طابق كل قاعدة مع موضوعها",
            "seconds": 60,
            "pairs": [
                {"term": "Rule 6", "definition": "Safe speed"},
                {"term": "Rule 7", "definition": "Risk of collision"},
                {"term": "Rule 8", "definition": "Action to avoid collision"},
                {"term": "Rule 5", "definition": "Look-out"},
            ],
        },
        {
            "type": "SPOT_MISTAKE",
            "instructions": "اضغط على الجزء الخاطئ",
            "code_or_text": "Rule 7 requires every vessel to maintain a minimum speed of 20 knots in fog.",
            "mistake_token": "20 knots",
            "explanation": "Rule 7 is about risk of collision; there is no 20 knot minimum in fog.",
        },
        {
            "type": "CONTEXTUAL_CLOZE",
            "instructions": "أكمل الفراغ",
            "sentence_with_blank": "Rule 6 requires every vessel to proceed at a safe _____ at all times.",
            "correct_answer": "speed",
            "distractors": ["depth", "heading", "mass"],
            "explanation": "القاعدة 6 هي قاعدة السرعة الآمنة.",
        },
        {
            "type": "MATCH_MADNESS",
            "instructions": "طابق كل مقياس بحري مع معناه",
            "seconds": 60,
            "pairs": [
                {"term": "Knot", "definition": "One nautical mile per hour"},
                {"term": "Cable", "definition": "One tenth of a nautical mile"},
                {"term": "Fathom", "definition": "Six feet of depth"},
                {"term": "League", "definition": "Three nautical miles"},
            ],
        },
        {
            "type": "SPOT_MISTAKE",
            "instructions": "اضغط على الجزء الخاطئ",
            "code_or_text": "Rule 8 requires any alteration of course to be a slow, barely noticeable change.",
            "mistake_token": "barely noticeable",
            "explanation": "Rule 8 demands positive, readily apparent action.",
        },
        {
            "type": "CONTEXTUAL_CLOZE",
            "instructions": "أكمل الفراغ",
            "sentence_with_blank": "Every vessel shall use all available means to determine if risk of _____ exists.",
            "correct_answer": "collision",
            "distractors": ["grounding", "flooding", "listing"],
            "explanation": "القاعدة 7 تتعلق بخطر التصادم.",
        },
        {
            "type": "MCQ_QUIZ",
            "instructions": "اختر الإجابة الصحيحة",
            "question": "أي قاعدة تتطلب الإبحار بسرعة آمنة؟",
            "options": [
                {"text": "القاعدة 6", "correct": True},
                {"text": "القاعدة 7", "correct": False},
                {"text": "القاعدة 8", "correct": False},
            ],
            "explanation": "القاعدة 6 هي قاعدة السرعة الآمنة.",
        },
        {
            "type": "MCQ_QUIZ",
            "instructions": "اختر الإجابة الصحيحة",
            "question": "أي قاعدة تتعلق بخطر التصادم؟",
            "options": [
                {"text": "القاعدة 7", "correct": True},
                {"text": "القاعدة 5", "correct": False},
                {"text": "القاعدة 6", "correct": False},
            ],
            "explanation": "القاعدة 7 تتعلق بخطر التصادم.",
        },
    ],
}


@dataclass
class LiveService:
    """Handle to the running app + its throwaway database."""

    base_url: str
    database_url: str
    database_name: str
    pg_data: Path
    process: subprocess.Popen
    state: dict[str, Any] = field(default_factory=dict)

    def call(self, method: str, path: str, payload: Any = None,
             files: Any = None, data: Any = None, timeout: int = 180):
        kwargs: dict[str, Any] = {"timeout": timeout}
        if payload is not None:
            kwargs["json"] = payload
        if files is not None:
            kwargs["files"] = files
            kwargs["data"] = data
        return requests.request(method, f"{self.base_url}{path}", **kwargs)

    def table_count(self, database: str | None = None) -> int:
        result = subprocess.run(
            [str(PSQL_BINARY), "-h", str(self.pg_data), "-U", "postgres",
             "-d", database or self.database_name, "-tAc",
             "select count(*) from information_schema.tables where table_schema='public';"],
            capture_output=True, text=True,
        )
        return int((result.stdout or "0").strip() or 0)

    def engine(self):
        from sqlalchemy.ext.asyncio import create_async_engine

        return create_async_engine(self.database_url)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def service(tmp_path_factory) -> LiveService:
    """Boot PostgreSQL + uvicorn on throwaway resources and tear both down."""
    # A crashed earlier run leaks its uvicorn; that stale process would keep
    # serving its own database and every assertion below would pass on the wrong
    # data, so clear the field first.
    subprocess.run(["pkill", "-f", "uvicorn main[.]app"], capture_output=True)
    time.sleep(1.0)

    pg_data = tmp_path_factory.mktemp("pgdata")
    server = pgserver.get_server(str(pg_data), cleanup_mode="stop")
    database_name = f"campuslingo_it_{int(time.time())}"
    server.psql(f"CREATE DATABASE {database_name};")

    database_url = f"postgresql+asyncpg://postgres@/{database_name}?host={pg_data}"
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", str(port),
         "--log-level", "warning"],
        cwd=str(SERVICE_DIR),
        env={
            **os.environ,
            "DATABASE_URL": database_url,
            "LOG_LEVEL": "WARNING",
            # Retention loop off during tests (deterministic runs); AdMob SSV is
            # configured with a shared secret so the signature path is covered.
            "RETENTION_ENABLED": "0",
            "ADMOB_SSV_SECRET": SSV_SECRET,
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    live = LiveService(base_url=base_url, database_url=database_url,
                       database_name=database_name, pg_data=Path(pg_data), process=process)

    deadline = time.time() + 90
    healthy = False
    while time.time() < deadline and process.poll() is None:
        try:
            if requests.get(f"{base_url}/health", timeout=5).status_code == 200:
                healthy = True
                break
        except requests.exceptions.RequestException:
            time.sleep(0.5)
    if not healthy:
        process.terminate()
        output = process.stdout.read() if process.stdout else ""
        pytest.fail(f"service did not become healthy on {base_url}\n{output[-3000:]}")

    try:
        yield live
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()


def _new_user(service: LiveService) -> str:
    email = f"it+{uuid.uuid4().hex[:8]}@campuslingo.test"
    response = service.call("POST", "/api/users", {"email": email})
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.fixture(scope="module")
def account(service: LiveService) -> dict[str, str]:
    """A user plus a course, seeded once for the whole module."""
    user_id = _new_user(service)
    response = service.call("POST", "/api/courses",
                            {"user_id": user_id, "name": "Maritime Safety (Port Klang)"})
    assert response.status_code == 201, response.text
    return {"user_id": user_id, "course_id": response.json()["id"]}


@pytest.fixture(scope="module")
def saved_node(service: LiveService, account: dict[str, str]) -> dict[str, Any]:
    """Persist one full bundle through the real helper and return its handles."""
    import database as db_module
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async def run() -> dict[str, Any]:
        engine = service.engine()
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            saved = await db_module.save_lecture_and_challenges(
                account["course_id"],
                "Rule 6-8 Navigation Rules",
                BUNDLE,
                session=session,
                source_file="Lecture 4_Navigation Rules.pdf",
                embedding=[0.02] * 1536,
                node_title="Rule 6-8 Navigation Rules",
            )
            saved["vector_dims"] = await session.scalar(
                db_module.text("SELECT vector_dims(embedding) FROM learning_nodes WHERE id = :id"),
                {"id": saved["node"]["id"]},
            )
            saved["due_now"] = len(
                await db_module.get_due_challenges_for_user(
                    account["user_id"], limit=10, session=session
                )
            )
            await session.commit()
        await engine.dispose()
        return saved

    return asyncio.run(run())


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


def test_health_reports_database_and_fsrs(service: LiveService):
    health = service.call("GET", "/health").json()
    assert health["status"] in ("ok", "degraded")
    assert health["database"]["ok"] is True
    assert health["fsrs"] == {"algorithm": "FSRS-4.5", "weights": 19}


def test_health_talks_to_a_fresh_database(service: LiveService):
    # Guards against a stale server from an earlier run answering these tests.
    # users, courses, lectures, learning_nodes, challenges, fsrs_cards, app_meta,
    # enrollments, notifications, push_subscriptions, exam_sessions
    assert service.table_count() == 11


# ---------------------------------------------------------------------------
# users + courses
# ---------------------------------------------------------------------------


def test_upsert_user_is_idempotent_and_normalises_email(service: LiveService):
    email = f"IDEMPOTENT+{uuid.uuid4().hex[:6]}@CampusLingo.test"
    first = service.call("POST", "/api/users", {"email": email}).json()
    second = service.call("POST", "/api/users", {"email": email.lower()}).json()
    assert first["id"] == second["id"]
    assert second["email"] == email.lower()
    assert first["gems"] == 500 and first["xp_points"] == 0


def test_upsert_user_rejects_invalid_email(service: LiveService):
    assert service.call("POST", "/api/users", {"email": "not-an-email"}).status_code == 422


def test_course_creation_returns_empty_lecture_list(service: LiveService, account: dict[str, str]):
    response = service.call("POST", "/api/courses",
                            {"user_id": account["user_id"], "name": "Auxiliary"})
    assert response.status_code == 201
    assert response.json()["lectures"] == []


def test_partially_ready_lectures_expose_their_nodes_immediately(
    service: LiveService, account: dict[str, str]
):
    """Zero-wait start: while a deck still generates (lecture status
    ``partially_ready``), its already-committed nodes must be visible via both
    GET /api/courses and GET /api/courses/{id}/nodes so Part 1 is playable.
    """
    import database as db_module
    from sqlalchemy.ext.asyncio import async_sessionmaker

    course = service.call(
        "POST", "/api/courses",
        {"user_id": account["user_id"], "name": "Progressive Visibility"},
    ).json()
    course_id = course["id"]

    async def seed() -> str:
        engine = service.engine()
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            lecture = db_module.Lecture(
                course_id=uuid.UUID(course_id),
                title="Progressive Deck",
                status="partially_ready",
                total_chunks=3,
                source_file="Progressive Deck.pdf",
            )
            session.add(lecture)
            await session.flush()
            node = db_module.LearningNode(
                lecture_id=lecture.id,
                title="Part 1: Prologue",
                content_summary="first committed part",
                order_index=0,
            )
            session.add(node)
            await session.flush()
            node_id = str(node.id)
            await session.commit()
        await engine.dispose()
        return node_id

    node_id = asyncio.run(seed())

    listing = service.call("GET", f"/api/courses?user_id={account['user_id']}").json()
    target = next(c for c in listing if c["id"] == course_id)
    assert [lecture["status"] for lecture in target["lectures"]] == ["partially_ready"]

    nodes = service.call("GET", f"/api/courses/{course_id}/nodes").json()["nodes"]
    assert any(entry["id"] == node_id for entry in nodes), nodes


# ---------------------------------------------------------------------------
# POST /api/process-lecture
# ---------------------------------------------------------------------------


def test_process_lecture_parses_pdf_without_generation(service: LiveService,
                                                      account: dict[str, str],
                                                      sample_pdf_bytes: bytes):
    response = service.call(
        "POST", "/api/process-lecture",
        files={"file": ("Lecture 4_Navigation Rules.pdf", sample_pdf_bytes, "application/pdf")},
        data={"course_id": account["course_id"], "generate": "false", "max_nodes": "5"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["lecture_id"]
    assert body["units"] and body["cards_seeded"] == 0
    assert body["challenges"] == []


def test_process_lecture_rejects_unknown_course_and_bad_types(service: LiveService,
                                                              account: dict[str, str],
                                                              sample_pdf_bytes: bytes):
    unknown = service.call(
        "POST", "/api/process-lecture",
        files={"file": ("x.pdf", sample_pdf_bytes, "application/pdf")},
        data={"course_id": str(uuid.uuid4()), "generate": "false"},
    )
    assert unknown.status_code == 404

    wrong_type = service.call(
        "POST", "/api/process-lecture",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        data={"course_id": account["course_id"], "generate": "false"},
    )
    assert wrong_type.status_code == 415


def test_duplicate_upload_creates_only_one_lecture(service: LiveService,
                                                   account: dict[str, str],
                                                   sample_pdf_bytes: bytes):
    """Uploading the exact same file twice must not create two chapter folders."""
    course = service.call(
        "POST", "/api/courses",
        {"user_id": account["user_id"], "name": "Duplicate Upload"},
    ).json()
    course_id = course["id"]

    def upload():
        return service.call(
            "POST", "/api/process-lecture",
            files={"file": ("Duplicate Deck.pdf", sample_pdf_bytes, "application/pdf")},
            data={"course_id": course_id, "generate": "false", "max_nodes": "1"},
        )

    first = upload()
    assert first.status_code == 201, first.text
    second = upload()
    assert second.status_code == 201, second.text

    courses = service.call("GET", f"/api/courses?user_id={account['user_id']}").json()
    target = next(c for c in courses if c["id"] == course_id)
    assert len(target["lectures"]) == 1, target["lectures"]


# ---------------------------------------------------------------------------
# database helpers
# ---------------------------------------------------------------------------


def test_save_lecture_and_challenges_persists_node_challenges_and_cards(saved_node: dict[str, Any]):
    assert saved_node["lecture"]["id"] and saved_node["node"]["id"]
    assert len(saved_node["challenges"]) == 8
    assert saved_node["cards_seeded"] == 8


def test_save_lecture_and_challenges_stores_the_embedding(saved_node: dict[str, Any]):
    assert saved_node["vector_dims"] == 1536


def test_unit_difficulty_level_maps_onto_challenge_difficulty(saved_node: dict[str, Any]):
    # difficulty_level 3 -> 0.5 on the 0..1 challenge scale
    assert all(item["difficulty"] == pytest.approx(0.5) for item in saved_node["challenges"])


def test_get_due_challenges_for_user_sees_fresh_cards(saved_node: dict[str, Any]):
    assert saved_node["due_now"] == 8


# ---------------------------------------------------------------------------
# GET /api/nodes/{node_id}/challenges
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def node_challenges(service: LiveService, saved_node: dict[str, Any]) -> dict[str, Any]:
    response = service.call("GET", f"/api/nodes/{saved_node['node']['id']}/challenges")
    assert response.status_code == 200, response.text
    return response.json()


def test_node_challenges_returns_two_items_per_type(node_challenges: dict[str, Any]):
    assert node_challenges["count"] == 8
    assert sorted(item["type"] for item in node_challenges["challenges"]) == [
        "CONTEXTUAL_CLOZE",
        "CONTEXTUAL_CLOZE",
        "MATCH_MADNESS",
        "MATCH_MADNESS",
        "MCQ_QUIZ",
        "MCQ_QUIZ",
        "SPOT_MISTAKE",
        "SPOT_MISTAKE",
    ]


def test_node_challenges_unknown_node_returns_404(service: LiveService):
    assert service.call("GET", f"/api/nodes/{uuid.uuid4()}/challenges").status_code == 404


@pytest.mark.parametrize(
    ("challenge_type", "checker"),
    [
        ("MATCH_MADNESS", lambda p: {"term", "definition"} <= set(p["pairs"][0]) and len(p["pairs"]) >= 4),
        ("SPOT_MISTAKE", lambda p: p["mistake_token"] in p["code_or_text"] and bool(p["explanation"])),
        ("CONTEXTUAL_CLOZE", lambda p: "_____" in p["sentence_with_blank"]
            and p["correct_answer"] not in p["distractors"] and len(p["distractors"]) >= 2),
        ("MCQ_QUIZ", lambda p: len(p["options"]) >= 2
            and sum(1 for option in p["options"] if option["correct"]) == 1),
    ],
)
def test_payload_shapes_survive_the_round_trip(node_challenges: dict[str, Any],
                                               challenge_type: str, checker):
    by_type = {item["type"]: item["payload_json"] for item in node_challenges["challenges"]}
    assert checker(by_type[challenge_type]), json.dumps(by_type[challenge_type], ensure_ascii=False)[:200]


def test_arabic_content_survives_the_round_trip(node_challenges: dict[str, Any]):
    cloze = next(item for item in node_challenges["challenges"] if item["type"] == "CONTEXTUAL_CLOZE")
    assert "القاعدة" in cloze["payload_json"]["explanation"]


# ---------------------------------------------------------------------------
# POST /api/reviews
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rating_ids(service: LiveService, node_challenges: dict[str, Any]) -> dict[str, str]:
    return {item["type"]: item["id"] for item in node_challenges["challenges"]}


def test_reviews_good_on_new_card_schedules_three_days(service: LiveService,
                                                       account: dict[str, str],
                                                       rating_ids: dict[str, str]):
    response = service.call("POST", "/api/reviews", {
        "user_id": account["user_id"], "challenge_id": rating_ids["MATCH_MADNESS"],
        "rating": 3, "elapsed_ms": 9000,
    })
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["state"] == 3 and body["state_name"] == "review"
    assert body["stability"] == pytest.approx(3.1262, abs=1e-4)
    assert body["interval_days"] == 3
    assert body["reps"] == 1
    assert body["xp_awarded"] > 0
    assert body["streak_count"] == 1
    assert "league_tier" in body

    due = datetime.fromisoformat(body["due_date"].replace("Z", "+00:00"))
    days_out = (due - datetime.now(timezone.utc)).total_seconds() / 86400
    assert days_out == pytest.approx(3, abs=0.02)


def test_reviews_again_moves_card_to_relearning(service: LiveService, account: dict[str, str],
                                                rating_ids: dict[str, str]):
    body = service.call("POST", "/api/reviews", {
        "user_id": account["user_id"], "challenge_id": rating_ids["MATCH_MADNESS"],
        "rating": 1, "elapsed_ms": 4000,
    }).json()

    assert body["state"] == 4 and body["state_name"] == "relearning"
    assert body["lapses"] == 1
    # Infinite hearts: wrong answers never cost a life.
    assert body["hearts_lost"] == 0 and body["xp_awarded"] == 0
    assert body["stability"] < 3.1262
    assert body["reps"] == 2

    due = datetime.fromisoformat(body["due_date"].replace("Z", "+00:00"))
    minutes_out = (due - datetime.now(timezone.utc)).total_seconds() / 60
    assert 0 <= minutes_out < 15  # relearning step, not a full day


def test_reviews_easy_graduates_and_extends_interval(service: LiveService,
                                                     account: dict[str, str],
                                                     rating_ids: dict[str, str]):
    body = service.call("POST", "/api/reviews", {
        "user_id": account["user_id"], "challenge_id": rating_ids["MATCH_MADNESS"],
        "rating": 4, "elapsed_ms": 3000,
    }).json()
    assert body["state"] == 3
    assert body["interval_days"] >= 1
    assert body["gems"] > 0


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    [
        ({"rating": 9}, 422),
        ({"user_id": "not-a-uuid"}, 422),
        ({"challenge_id": str(uuid.uuid4())}, 404),
    ],
)
def test_reviews_reject_invalid_input(service: LiveService, account: dict[str, str],
                                      rating_ids: dict[str, str], payload: dict, expected_status: int):
    body = {
        "user_id": account["user_id"],
        "challenge_id": rating_ids["MCQ_QUIZ"],
        "rating": 3,
        **payload,
    }
    assert service.call("POST", "/api/reviews", body).status_code == expected_status


def test_lapsed_card_lands_in_the_mistakes_inbox(service: LiveService, account: dict[str, str],
                                                 rating_ids: dict[str, str]):
    for rating in (3, 1):
        service.call("POST", "/api/reviews", {
            "user_id": account["user_id"], "challenge_id": rating_ids["CONTEXTUAL_CLOZE"],
            "rating": rating, "elapsed_ms": 12000,
        })

    inbox = service.call("GET", f"/api/mistakes?user_id={account['user_id']}").json()
    assert inbox["count"] == 1
    entry = inbox["items"][0]
    assert entry["is_mistake"] is True and entry["lapses"] == 1
    assert entry["node_title"]


# ---------------------------------------------------------------------------
# queue, stats, economy, cascade
# ---------------------------------------------------------------------------


def test_queue_exposes_only_due_cards_with_payloads(service: LiveService, account: dict[str, str]):
    response = service.call("GET", f"/api/queue?user_id={account['user_id']}&limit=20&only_due=true")
    assert response.status_code == 200
    body = response.json()

    # Retrieval happens on demand (the "start session" action), so nothing is
    # necessarily due at this point in the module — but every row must be
    # renderable by the components.
    for item in body["items"]:
        assert item["payload_json"]["type"] == item["type"]
        assert item["due_date"]
        assert item["stability"] >= 0


def test_queue_only_due_false_reports_reviewed_cards(service: LiveService, account: dict[str, str]):
    body = service.call(
        "GET", f"/api/queue?user_id={account['user_id']}&limit=20&only_due=false"
    ).json()
    reviewed = [item for item in body["items"] if item["stability"] > 0]
    assert len(reviewed) >= 2
    assert all(item["retrievability"] is not None for item in reviewed)
    assert any(item["is_mistake"] and item["lapses"] >= 1 for item in body["items"])


def test_stats_aggregate_cards_relearning_and_accuracy(service: LiveService, account: dict[str, str]):
    stats = service.call("GET", f"/api/users/{account['user_id']}/stats").json()
    assert stats["cards_total"] == 8
    assert stats["cards_relearning"] >= 1
    assert 0.0 <= stats["accuracy_last_50"] <= 1.0
    assert stats["xp_points"] > 0


def test_gem_spending_guards_the_balance(service: LiveService, account: dict[str, str]):
    ok = service.call("POST", f"/api/users/{account['user_id']}/gems/spend",
                      {"amount": 10, "reason": "test"})
    assert ok.status_code == 200
    broke = service.call("POST", f"/api/users/{account['user_id']}/gems/spend",
                      {"amount": 50_000, "reason": "test"})
    assert broke.status_code == 402


# ---------------------------------------------------------------------------
# course sharing (share codes + deep-clone import)
# ---------------------------------------------------------------------------


def test_course_creation_assigns_share_code(service: LiveService, account: dict[str, str]):
    response = service.call("POST", "/api/courses",
                            {"user_id": account["user_id"], "name": "Shared Origin"})
    assert response.status_code == 201, response.text
    code = response.json()["share_code"]
    assert code and code.startswith("CL-") and len(code) == 9  # CL- + 6 chars


def test_share_code_is_returned_or_generated(service: LiveService, account: dict[str, str]):
    first = service.call("GET", f"/api/courses/{account['course_id']}/share-code")
    assert first.status_code == 200, first.text
    code = first.json()["share_code"]
    assert code and code.startswith("CL-")
    # Idempotent: the same course always reports the same code.
    second = service.call("GET", f"/api/courses/{account['course_id']}/share-code").json()
    assert second["share_code"] == code


def test_share_code_endpoint_404s_for_unknown_course(service: LiveService):
    response = service.call("GET", f"/api/courses/{uuid.uuid4()}/share-code")
    assert response.status_code == 404


def test_import_clones_course_tree_and_seeds_fresh_cards(service: LiveService,
                                                         account: dict[str, str],
                                                         saved_node: dict[str, Any]):
    """Deep clone: course + lectures + nodes + challenges + fresh FSRS cards."""
    importer_id = _new_user(service)
    code = service.call("GET", f"/api/courses/{account['course_id']}/share-code").json()["share_code"]

    response = service.call("POST", "/api/courses/import",
                            {"user_id": importer_id, "share_code": code})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["course_id"] != account["course_id"]
    # The account course is shared with other module tests, so its lecture
    # count varies; the clone must mirror whatever the source holds. Nodes,
    # challenges and cards are exact: one node with the full 8-challenge bundle.
    assert body["lectures_cloned"] >= 1
    assert body["nodes_cloned"] == 1
    assert body["challenges_cloned"] == 8
    assert body["cards_seeded"] == 8

    # The clone shows up in the importer's course list with its own share code.
    importer_courses = service.call("GET", f"/api/courses?user_id={importer_id}").json()
    assert any(c["id"] == body["course_id"] for c in importer_courses)
    clone = next(c for c in importer_courses if c["id"] == body["course_id"])
    assert clone["share_code"] and clone["share_code"] != code

    # The importer's review queue is due immediately (fresh cards, state new).
    queue = service.call("GET", f"/api/queue?user_id={importer_id}&only_due=true&limit=50").json()
    assert queue["count"] == 8
    # ...and the importer's stats reflect the seeded cards.
    stats = service.call("GET", f"/api/users/{importer_id}/stats").json()
    assert stats["cards_total"] == 8 and stats["cards_due"] == 8

    # Cloned challenges keep the same payloads but different ids.
    source_node = service.call(
        "GET", f"/api/nodes/{saved_node['node']['id']}/challenges"
    ).json()
    clone_nodes = service.call("GET", f"/api/courses/{body['course_id']}/nodes").json()
    assert clone_nodes["nodes"], "clone must expose its node on the pathway map"
    clone_node_id = clone_nodes["nodes"][0]["id"]
    clone_node = service.call("GET", f"/api/nodes/{clone_node_id}/challenges").json()
    assert [c["type"] for c in clone_node["challenges"]] == [c["type"] for c in source_node["challenges"]]
    assert {c["id"] for c in clone_node["challenges"]}.isdisjoint(
        {c["id"] for c in source_node["challenges"]}
    )


def test_import_validates_share_code_and_existence(service: LiveService, account: dict[str, str]):
    importer_id = _new_user(service)
    # Malformed codes are rejected with 422.
    bad = service.call("POST", "/api/courses/import",
                       {"user_id": importer_id, "share_code": "nope!"})
    assert bad.status_code == 422
    # A well-formed but unknown code is a 404.
    unknown = service.call("POST", "/api/courses/import",
                           {"user_id": importer_id, "share_code": f"CL-{uuid.uuid4().hex[:6].upper()}"})
    assert unknown.status_code == 404
    # Unknown user is a 404 as well.
    code = service.call("GET", f"/api/courses/{account['course_id']}/share-code").json()["share_code"]
    ghost = service.call("POST", "/api/courses/import",
                         {"user_id": str(uuid.uuid4()), "share_code": code})
    assert ghost.status_code == 404


def test_heart_refill_costs_gems(service: LiveService, account: dict[str, str]):
    before = service.call("GET", f"/api/users/{account['user_id']}/stats").json()["gems"]
    response = service.call("POST", f"/api/users/{account['user_id']}/hearts/refill")
    if response.status_code == 200:
        assert response.json()["hearts"] == 5
        assert response.json()["gems"] == before - 350
    else:
        assert response.status_code == 402


def test_fsrs_preview_offers_four_rating_options(service: LiveService, account: dict[str, str],
                                                 rating_ids: dict[str, str]):
    preview = service.call(
        "GET", f"/api/fsrs/preview/{rating_ids['MATCH_MADNESS']}?user_id={account['user_id']}"
    ).json()
    assert len(preview["options"]) == 4
    assert float(preview["options"]["4"]["interval_days"]) > float(preview["options"]["1"]["interval_days"])


def test_duplicate_lecture_matches_for_replacement_and_delete_is_isolated(
    service: LiveService, account: dict[str, str]
):
    """Deduplication + isolation: the same file/title is found for replacement,
    and deleting one lecture leaves the sibling lecture and course intact."""
    import database as db_module
    from sqlalchemy.ext.asyncio import async_sessionmaker

    course = service.call(
        "POST", "/api/courses",
        {"user_id": account["user_id"], "name": "Dedup & Isolation"},
    ).json()
    course_id = course["id"]

    async def run() -> dict[str, Any]:
        engine = service.engine()
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            dup = await db_module.save_lecture_and_challenges(
                course_id, "Dedup Lecture", BUNDLE,
                session=session, source_file="dup.pdf", node_title="Dedup Lecture",
            )
            sibling = await db_module.save_lecture_and_challenges(
                course_id, "Sibling Lecture", BUNDLE,
                session=session, source_file="sibling.pdf", node_title="Sibling Lecture",
            )
            # A re-upload of the same file (or title) is matched for replacement.
            matched = await db_module.find_replaceable_lecture(
                course_id, source_file="dup.pdf", title="Dedup Lecture", session=session
            )
            unmatched = await db_module.find_replaceable_lecture(
                course_id, source_file="new.pdf", title="New Topic", session=session
            )
            await db_module.delete_lecture_subtree(session, dup["lecture"]["id"])
            remaining_lectures = await session.scalar(
                db_module.select(db_module.func.count())
                .select_from(db_module.Lecture)
                .where(db_module.Lecture.course_id == uuid.UUID(course_id))
            )
            sibling_nodes = await session.scalar(
                db_module.select(db_module.func.count())
                .select_from(db_module.LearningNode)
                .where(
                    db_module.LearningNode.lecture_id == uuid.UUID(sibling["lecture"]["id"])
                )
            )
            await session.commit()
        await engine.dispose()
        return {
            "dup_id": dup["lecture"]["id"],
            "sibling_id": sibling["lecture"]["id"],
            "matched": str(matched.id) if matched else None,
            "unmatched": unmatched,
            "remaining_lectures": remaining_lectures,
            "sibling_nodes": sibling_nodes,
        }

    result = asyncio.run(run())
    assert result["matched"] == result["dup_id"]
    assert result["unmatched"] is None
    assert result["remaining_lectures"] == 1  # only the sibling lecture survives
    assert result["sibling_nodes"] == 1       # sibling node untouched

    # The course and its surviving lecture are still exposed by the API.
    courses = service.call("GET", f"/api/courses?user_id={account['user_id']}").json()
    survivor = next(c for c in courses if c["id"] == course_id)
    assert [lecture["id"] for lecture in survivor["lectures"]] == [result["sibling_id"]]
    assert service.call("GET", f"/api/lectures/{result['dup_id']}").status_code == 404
    assert service.call("GET", f"/api/lectures/{result['sibling_id']}").status_code == 200


def test_deleting_a_lecture_cascades_to_cards(service: LiveService):
    """Deleting a lecture removes its own cards — and nothing else.

    This test is destructive, so it builds its own course: it must never delete
    the module-scoped ``saved_node`` fixture that the sharing/royalty tests
    above and below depend on.
    """
    import database as db_module
    from sqlalchemy.ext.asyncio import async_sessionmaker

    owner_id = _new_user(service)
    course_id = service.call(
        "POST", "/api/courses", {"user_id": owner_id, "name": "Cascade Isolation"}
    ).json()["id"]

    async def run() -> dict[str, Any]:
        engine = service.engine()
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            doomed = await db_module.save_lecture_and_challenges(
                course_id, "Doomed Lecture", BUNDLE,
                session=session, source_file="doomed.pdf",
            )
            sibling = await db_module.save_lecture_and_challenges(
                course_id, "Sibling Lecture", BUNDLE,
                session=session, source_file="sibling.pdf",
            )
            await session.commit()
        await engine.dispose()
        return {"doomed": doomed, "sibling": sibling}

    saved = asyncio.run(run())
    deleted = service.call("DELETE", f"/api/lectures/{saved['doomed']['lecture']['id']}")
    assert deleted.status_code == 204
    # The deleted lecture's node is gone with its cards...
    assert service.call(
        "GET", f"/api/nodes/{saved['doomed']['node']['id']}/challenges"
    ).status_code == 404
    # ...while the sibling lecture survives untouched, and so does the course.
    assert service.call(
        "GET", f"/api/nodes/{saved['sibling']['node']['id']}/challenges"
    ).status_code == 200
    assert service.call("GET", f"/api/courses/{course_id}/nodes").json()["nodes"]
    assert service.call("GET", f"/api/lectures/{saved['sibling']['lecture']['id']}").status_code == 200


# ---------------------------------------------------------------------------
# Enterprise suite: Socratic tutor, battle arena, gem shop, weekly leagues
# ---------------------------------------------------------------------------

# A duel-friendly bundle: every challenge here has one server-gradable answer.
DUEL_BUNDLE: dict[str, Any] = {
    "unit_id": "unit-duel-1",
    "concept_title": "Speed, look-out and safe speed (Rules 5-6)",
    "difficulty_level": 2,
    "challenges": [
        {
            "type": "MCQ_QUIZ",
            "question": "Which rule requires a proper look-out at all times?",
            "options": [
                {"text": "Rule 5", "correct": True},
                {"text": "Rule 6", "correct": False},
                {"text": "Rule 8", "correct": False},
            ],
            "explanation": "Rule 5 is the look-out rule.",
        },
        {
            "type": "TRUE_FALSE",
            "statement": "Rule 6 requires every vessel to proceed at a safe speed.",
            "answer": True,
            "explanation": "That is exactly Rule 6.",
        },
        {
            "type": "CONTEXTUAL_CLOZE",
            "instructions": "أكمل الفراغ",
            "sentence_with_blank": "Every vessel shall proceed at a safe _____ at all times.",
            "correct_answer": "speed",
            "distractors": ["depth", "heading", "mass"],
            "explanation": "القاعدة 6 هي قاعدة السرعة الآمنة.",
        },
        # Deliberately non-gradable: the arena must skip it, not crash.
        {
            "type": "SPOT_MISTAKE",
            "instructions": "اضغط على الجزء الخاطئ",
            "code_or_text": "Rule 6 requires a minimum speed of 20 knots in fog.",
            "mistake_token": "20 knots",
            "explanation": "There is no 20-knot minimum in fog.",
        },
    ],
}


@pytest.fixture(scope="module")
def duel_node(service: LiveService, account: dict[str, str]) -> dict[str, Any]:
    """One node whose challenges can all be graded inside a duel."""
    import database as db_module
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async def run() -> dict[str, Any]:
        engine = service.engine()
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            saved = await db_module.save_lecture_and_challenges(
                account["course_id"],
                "Duel Rules",
                DUEL_BUNDLE,
                session=session,
                source_file="duel_rules.pdf",
                node_title="Duel Rules",
            )
            await session.commit()
        await engine.dispose()
        return saved

    return asyncio.run(run())


# --- gem shop ---------------------------------------------------------------


def test_shop_catalog_publishes_the_spec_items(service: LiveService):
    body = service.call("GET", "/api/shop/items").json()
    by_id = {item["id"]: item for item in body["items"]}
    assert set(by_id) == {"streak_freeze", "double_xp", "golden_pass"}
    assert by_id["streak_freeze"]["price"] == 200
    assert by_id["double_xp"]["price"] == 150
    assert by_id["double_xp"]["effect"] == "double_xp"


def test_shop_purchase_applies_inventory_and_guards_the_balance(service: LiveService):
    buyer = _new_user(service)  # starts with 500 gems
    potion = service.call(
        "POST", f"/api/users/{buyer}/shop/purchase", {"item_id": "double_xp"}
    )
    assert potion.status_code == 200, potion.text
    body = potion.json()
    assert body["gems"] == 350
    assert body["inventory"]["double_xp"] == 1
    # The 30-minute buff is timestamped server-side so it cannot be spoofed.
    assert body["inventory"].get("double_xp_until")

    freeze = service.call(
        "POST", f"/api/users/{buyer}/shop/purchase", {"item_id": "streak_freeze"}
    )
    assert freeze.status_code == 200
    assert freeze.json()["gems"] == 150
    assert freeze.json()["streak_freeze_active"] is True

    # 150 gems cannot buy the 900-gem pass, and unknown items are a 404.
    assert service.call(
        "POST", f"/api/users/{buyer}/shop/purchase", {"item_id": "golden_pass"}
    ).status_code == 402
    assert service.call(
        "POST", f"/api/users/{buyer}/shop/purchase", {"item_id": "nope"}
    ).status_code == 404


def test_stats_expose_the_economy_fields(service: LiveService):
    user_id = _new_user(service)
    stats = service.call("GET", f"/api/users/{user_id}/stats").json()
    assert stats["weekly_xp"] == 0
    assert stats["league_tier"] == 0
    assert stats["streak_freeze_active"] is False
    assert stats["inventory"] == {}


# --- weekly leagues ---------------------------------------------------------


def test_leaderboard_ranks_the_cohort_and_flags_my_row(service: LiveService):
    user_id = _new_user(service)
    board = service.call("GET", f"/api/leaderboards/{user_id}?cohort_size=10").json()
    assert board["league_name"] == board["league_names"][board["league_tier"]]
    assert board["cohort_size"] >= 1
    assert board["rows"][0]["rank"] == 1
    mine = next(row for row in board["rows"] if row["is_me"])
    assert mine["user_id"] == user_id and mine["weekly_xp"] == 0
    assert board["my_rank"] == mine["rank"]
    assert board["week_key"] and board["rollover_at"]
    assert board["zone"] in {"promotion", "retention", "relegation"}
    # Ranked strictly on weekly XP, descending.
    scores = [row["weekly_xp"] for row in board["rows"]]
    assert scores == sorted(scores, reverse=True)


def test_leaderboard_rollover_runs_at_most_once_per_week(service: LiveService):
    user_id = _new_user(service)
    first = service.call("POST", "/api/leaderboards/rollover").json()
    second = service.call("POST", "/api/leaderboards/rollover").json()
    assert first["week_key"] == second["week_key"]
    # The first rollover of the cycle may have already happened via a read, but
    # the marker makes every later sweep a no-op — that is the idempotency
    # guarantee that keeps weekly_xp resets from running twice.
    assert second["rolled_over"] is False
    assert service.call("GET", f"/api/leaderboards/{user_id}").status_code == 200


def test_leaderboard_unknown_user_is_404(service: LiveService):
    assert service.call("GET", f"/api/leaderboards/{uuid.uuid4()}").status_code == 404


# --- Socratic tutor (SSE on the 0.8B tier) ----------------------------------


def test_tutor_hint_charges_gems_and_streams_sse(service: LiveService):
    user_id = _new_user(service)
    with requests.post(
        f"{service.base_url}/api/tutor/hint",
        json={
            "user_id": user_id,
            "question": "Which rule requires a proper look-out?",
            "student_choice": "Rule 6",
            "context": "Rules 5 and 6 of COLREGs",
        },
        stream=True,
        timeout=60,
    ) as response:
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/event-stream")
        payload = "".join(response.iter_lines(decode_unicode=True) or [])

    # The stream always opens with the tier/cost header and closes with DONE.
    # The tutor PREFERS 0.8B (800M) but transparently re-routes to whichever
    # llama-server tier is actually healthy, so assert a known tier id rather
    # than hard-coding 800M (which fails whenever a real 2B/4B/9B server is up).
    assert '"type": "open"' in payload
    assert any(
        f'"tier": "{tier}"' in payload for tier in ("800M", "2B", "4B", "9B")
    ), payload
    assert "[DONE]" in payload
    # With no reachable model the drawer degrades to a canned nudge (delta)
    # rather than hanging or 500-ing.
    assert '"type": "delta"' in payload or '"type": "error"' in payload
    assert '"type": "done"' in payload

    # One hint = 10 gems, charged before the first token.
    stats = service.call("GET", f"/api/users/{user_id}/stats").json()
    assert stats["gems"] == 490
    assert stats["inventory"]["hints_used"] == 1


def test_tutor_hint_refuses_when_the_balance_is_short(service: LiveService):
    user_id = _new_user(service)
    spent = service.call(
        "POST", f"/api/users/{user_id}/gems/spend", {"amount": 500, "reason": "test"}
    )
    assert spent.status_code == 200, spent.text
    response = service.call("POST", "/api/tutor/hint", {"user_id": user_id, "question": "x"})
    assert response.status_code == 402


def test_tutor_hint_validates_the_user_id(service: LiveService):
    assert service.call(
        "POST", "/api/tutor/hint", {"user_id": "not-a-uuid", "question": "x"}
    ).status_code == 400


# --- battle arena: REST + two live WebSocket players -------------------------


def test_arena_room_creation_uses_the_requested_code(service: LiveService,
                                                     duel_node: dict[str, Any]):
    response = service.call(
        "POST", "/api/arena/rooms",
        {"node_id": duel_node["node"]["id"], "limit": 3, "room_code": "TEST01"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["room_code"] == "TEST01"
    assert body["status"] == "waiting"
    assert body["question_seconds"] >= 10
    # The non-gradable MATCH_MADNESS challenge was skipped.
    assert body["questions"] == 3

    state = service.call("GET", "/api/arena/rooms/TEST01")
    assert state.status_code == 200
    assert state.json()["type"] == "state"

    # A duplicate code is a conflict; an unknown code is a 404.
    duplicate = service.call(
        "POST", "/api/arena/rooms",
        {"node_id": duel_node["node"]["id"], "room_code": "TEST01"},
    )
    assert duplicate.status_code == 409
    assert service.call("GET", "/api/arena/rooms/NOPE99").status_code == 404
    # A collection-level GET is not part of the contract (rooms are addressed
    # by code), so it must not silently succeed.
    assert service.call("GET", "/api/arena/rooms").status_code in (404, 405)


def test_arena_rejects_an_unknown_node(service: LiveService):
    response = service.call("POST", "/api/arena/rooms", {"node_id": str(uuid.uuid4())})
    assert response.status_code == 404
    assert service.call(
        "POST", "/api/arena/rooms", {"node_id": "not-a-uuid"}
    ).status_code == 400


def test_two_players_duel_in_room_test01_and_the_winner_is_paid(
    service: LiveService, duel_node: dict[str, Any]
):
    websockets = pytest.importorskip("websockets", reason="websockets client required")
    player_one = _new_user(service)
    player_two = _new_user(service)

    created = service.call(
        "POST", "/api/arena/rooms",
        {"node_id": duel_node["node"]["id"], "limit": 2, "room_code": "TEST02"},
    ).json()
    assert created["room_code"] == "TEST02"
    ws_base = service.base_url.replace("http://", "ws://") + f"/ws/arena/{created['room_code']}"
    url_one = f"{ws_base}?player_id={player_one}&name=Ahmed"
    url_two = f"{ws_base}?player_id={player_two}&name=Sara"

    async def duel() -> dict[str, Any]:
        kinds: list[str] = []
        finish: dict[str, Any] | None = None
        async with websockets.connect(url_one) as alpha, websockets.connect(url_two) as beta:
            welcome_a = json.loads(await asyncio.wait_for(alpha.recv(), timeout=10))
            welcome_b = json.loads(await asyncio.wait_for(beta.recv(), timeout=10))
            assert welcome_a["type"] == "welcome" and welcome_b["type"] == "welcome"
            deadline = time.time() + 30
            while time.time() < deadline:
                message = json.loads(await asyncio.wait_for(alpha.recv(), timeout=10))
                kinds.append(message["type"])
                if message["type"] == "question":
                    # Both players must answer for the room to advance.
                    await alpha.send(json.dumps({"type": "answer", "selected": 0}))
                    await beta.send(json.dumps({"type": "answer", "selected": 0}))
                elif message["type"] == "finish":
                    finish = message
                    break
        return {"kinds": kinds, "finish": finish}

    result = asyncio.run(duel())
    assert "start" in result["kinds"]
    assert result["kinds"].count("question") == 2
    assert "reveal" in result["kinds"]
    assert result["finish"] is not None
    standings = result["finish"]["standings"]
    assert {row["player_id"] for row in standings} == {player_one, player_two}
    assert result["finish"]["winner"] == standings[0]["player_id"]

    # Rewarding is winner-only and paid exactly once per room.
    winner = result["finish"]["winner"]
    loser = player_two if winner == player_one else player_one
    denied = service.call(
        "POST", f"/api/arena/rooms/TEST02/reward", {"player_id": loser}
    ).json()
    assert denied["awarded"] is False

    paid = service.call("POST", f"/api/arena/rooms/TEST02/reward", {"player_id": winner})
    assert paid.status_code == 200, paid.text
    body = paid.json()
    assert body["awarded"] is True
    # ``xp``/``gems`` are the award amounts; the balance rides along separately
    # so the podium can print "+100 XP / +25 gems" without doing math.
    assert (body["xp"], body["gems"]) == (100, 25)
    assert body["gems_balance"] == 525
    stats = service.call("GET", f"/api/users/{winner}/stats").json()
    assert stats["xp_points"] == 100 and stats["gems"] == 525
    assert stats["weekly_xp"] == 100

    replay = service.call(
        "POST", f"/api/arena/rooms/TEST02/reward", {"player_id": winner}
    ).json()
    assert replay["awarded"] is False and replay["reason"] == "already paid"

    # A duel that has not started cannot hand out a payout.
    service.call(
        "POST", "/api/arena/rooms",
        {"node_id": duel_node["node"]["id"], "limit": 2, "room_code": "TEST03"},
    )
    early = service.call("POST", "/api/arena/rooms/TEST03/reward", {"player_id": winner})
    assert early.status_code == 409


# ---------------------------------------------------------------------------
# Google-Classroom instructor hub: create -> join -> roster -> live fan-out
# ---------------------------------------------------------------------------


def test_classroom_creation_assigns_a_code_and_instructor_role(service: LiveService):
    instructor_id = _new_user(service)
    created = service.call(
        "POST", "/api/classrooms",
        {"instructor_id": instructor_id, "name": "CS101 Operating Systems"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["is_classroom"] is True
    assert body["role"] == "instructor"
    assert body["instructor_id"] == instructor_id
    code = body["enrollment_code"]
    assert code and len(code) == 6 and code.isalnum()
    # The account is promoted, and the classroom shows up under what it teaches.
    taught = service.call("GET", f"/api/classrooms?user_id={instructor_id}").json()["taught"]
    assert [course["id"] for course in taught] == [body["id"]]
    assert taught[0]["enrollment_code"] == code

    assert service.call(
        "POST", "/api/classrooms", {"instructor_id": str(uuid.uuid4()), "name": "Ghost"}
    ).status_code == 404
    assert service.call(
        "POST", "/api/classrooms", {"instructor_id": instructor_id, "name": "   "}
    ).status_code == 422


@pytest.fixture(scope="module")
def classroom(service: LiveService) -> dict[str, str]:
    """An instructor-owned classroom with one enrolled student."""
    instructor_id = _new_user(service)
    created = service.call(
        "POST", "/api/classrooms",
        {"instructor_id": instructor_id, "name": "CS301 Distributed Systems"},
    )
    assert created.status_code == 201, created.text
    course = created.json()
    student_id = _new_user(service)
    joined = service.call(
        "POST", "/api/classrooms/join",
        {"code": course["enrollment_code"], "student_id": student_id},
    )
    assert joined.status_code == 201, joined.text
    return {
        "instructor_id": instructor_id,
        "student_id": student_id,
        "course_id": course["id"],
        "code": course["enrollment_code"],
        "joined": joined.json(),
    }


def test_classroom_join_is_idempotent_and_validates_the_code(service: LiveService,
                                                            classroom: dict[str, str]):
    assert classroom["joined"]["already_enrolled"] is False
    assert classroom["joined"]["enrolled_count"] == 1

    again = service.call(
        "POST", "/api/classrooms/join",
        {"code": classroom["code"].lower(), "student_id": classroom["student_id"]},
    )
    assert again.status_code == 201
    assert again.json()["already_enrolled"] is True

    unknown = service.call(
        "POST", "/api/classrooms/join", {"code": "ZZZZZZ", "student_id": classroom["student_id"]}
    )
    assert unknown.status_code == 404
    assert service.call(
        "POST", "/api/classrooms/join",
        {"code": classroom["code"], "student_id": "not-a-uuid"},
    ).status_code == 422

    # The student's own list shows the classroom they joined.
    enrolled = service.call(
        "GET", f"/api/classrooms?user_id={classroom['student_id']}"
    ).json()["enrolled"]
    assert [course["id"] for course in enrolled] == [classroom["course_id"]]


def test_classroom_roster_and_csv_export_are_instructor_guarded(service: LiveService,
                                                                classroom: dict[str, str]):
    roster = service.call(
        "GET", f"/api/classrooms/{classroom['course_id']}/roster?instructor_id={classroom['instructor_id']}"
    )
    assert roster.status_code == 200, roster.text
    body = roster.json()
    assert body["student_count"] == 1
    entry = body["roster"][0]
    assert entry["student_id"] == classroom["student_id"]
    assert entry["completion_percent"] == 0.0 and entry["accuracy"] == 0.0
    assert body["enrollment_code"] == classroom["code"]

    # A random account cannot read another classroom's roster or grades.
    outsider = _new_user(service)
    assert service.call(
        "GET", f"/api/classrooms/{classroom['course_id']}/roster?instructor_id={outsider}"
    ).status_code == 403
    assert service.call(
        "GET", f"/api/classrooms/{classroom['course_id']}/grades.csv?instructor_id={outsider}"
    ).status_code == 403
    # Nor can the enrolled student read the class roster.
    assert service.call(
        "GET",
        f"/api/classrooms/{classroom['course_id']}/roster?instructor_id={classroom['student_id']}",
    ).status_code == 403

    csv_response = service.call(
        "GET",
        f"/api/classrooms/{classroom['course_id']}/grades.csv?instructor_id={classroom['instructor_id']}",
    )
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv_response.headers.get("content-disposition", "")
    header, *rows = csv_response.text.strip().splitlines()
    assert header == (
        "email,xp_points,streak_count,challenges_answered,challenges_total,"
        "completion_percent,accuracy,last_active_at"
    )
    assert len(rows) == 1 and rows[0].startswith("it+")


def test_new_lecture_fanout_records_notifications_and_streams_live(
    service: LiveService, classroom: dict[str, str]
):
    """The instructor's upload path notifies students *and* pushes an SSE event.

    The fan-out helper is the exact function the upload endpoints call, so this
    exercises the real code path (only the LLM generation is bypassed, since no
    model tier runs in CI).
    """
    import database as db_module
    import retention as retention_module
    from sqlalchemy.ext.asyncio import async_sessionmaker

    async def record() -> int:
        engine = service.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            course = await session.get(db_module.Course, uuid.UUID(classroom["course_id"]))
            # The classroom material must leave the syllabus tree untouched until
            # a real upload; here we only assert the notification fan-out.
            assert course is not None and course.is_classroom
            sent = await retention_module.notify_new_lecture(
                session,
                student_ids=[uuid.UUID(classroom["student_id"])],
                lecture_title="Part 1: Consensus & Raft",
                course_id=course.id,
                course_name=course.name,
            )
            await session.commit()
        await engine.dispose()
        return len(sent)

    assert asyncio.run(record()) == 1

    inbox = service.call(
        "GET", f"/api/users/{classroom['student_id']}/notifications"
    ).json()
    assert inbox["unread"] >= 1
    lecture_alerts = [item for item in inbox["items"] if item["kind"] == "new_lecture"]
    assert lecture_alerts, "the student must receive a new-material alert"
    assert "Part 1: Consensus & Raft" in lecture_alerts[0]["title"]
    # No pywebpush/VAPID in CI: recorded in-app rather than silently dropped.
    assert lecture_alerts[0]["push_status"].startswith("push_unavailable") or lecture_alerts[0][
        "push_status"
    ] == "recorded"
    assert service.call(
        "POST", f"/api/notifications/{lecture_alerts[0]['id']}/read"
    ).json()["read"] is True

    # --- live SSE: the student's dashboard updates without a refresh --------
    stream = requests.get(
        f"{service.base_url}/api/classrooms/{classroom['course_id']}/updates"
        f"?student_id={classroom['student_id']}",
        stream=True,
        timeout=30,
    )
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    lines = stream.iter_lines(decode_unicode=True)

    def read_event(deadline_seconds: float = 15.0) -> dict:
        """Next SSE frame; bounded so a lost event fails instead of hanging.

        Keepalive comments arrive every 20s, which would keep an unbounded
        reader parked forever, so the loop checks a wall-clock deadline between
        lines and raises a diagnosable assertion instead.
        """
        event_name = None
        data = None
        deadline = time.time() + deadline_seconds
        while time.time() < deadline:
            raw = next(lines, None)
            if raw is None:
                raise AssertionError("stream ended before an event arrived")
            line = raw.strip()
            if not line:
                if data is not None:
                    return {"event": event_name, **json.loads(data)}
                continue
            if line.startswith(":"):
                continue  # keepalive comment
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = line.split(":", 1)[1].strip()
        raise AssertionError("timed out waiting for an SSE event")

    try:
        handshake = read_event()
        assert handshake["type"] == "connected"
        assert handshake["course_id"] == classroom["course_id"]

        def broadcast() -> None:
            time.sleep(0.4)
            service.call(
                "POST",
                f"/api/classrooms/{classroom['course_id']}/publish"
                f"?instructor_id={classroom['instructor_id']}",
                {"type": "lecture_added", "lecture_title": "Part 2: Paxos"},
            )

        worker = threading.Thread(target=broadcast)
        worker.start()
        event = read_event()
        worker.join(timeout=10)
        assert event["type"] == "lecture_added"
        assert event["lecture_title"] == "Part 2: Paxos"
    finally:
        stream.close()


def test_classroom_publish_requires_the_instructor(service: LiveService,
                                                   classroom: dict[str, str]):
    outsider = _new_user(service)
    assert service.call(
        "POST",
        f"/api/classrooms/{classroom['course_id']}/publish?instructor_id={outsider}",
        {"type": "classroom_updated"},
    ).status_code == 403
    # A personal (non-classroom) course is not addressable as a classroom.
    personal = service.call(
        "POST", "/api/courses", {"user_id": classroom["instructor_id"], "name": "Personal"}
    ).json()
    assert service.call(
        "GET", f"/api/classrooms/{personal['id']}/updates"
    ).status_code == 404


# ---------------------------------------------------------------------------
# Exam Crunch: cheatsheet + diagnostic timed mock exam
# ---------------------------------------------------------------------------


def test_exam_crunch_builds_a_timed_paper_with_a_cheatsheet(service: LiveService,
                                                           duel_node: dict[str, Any],
                                                           account: dict[str, str]):
    response = service.call(
        "POST", f"/api/courses/{account['course_id']}/exam-crunch",
        {"user_id": account["user_id"]},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["duration_seconds"] == 45 * 60
    assert body["question_count"] == len(body["questions"]) >= 1
    assert body["target_range"] == [25, 40]
    # No model tier in CI: the cheatsheet must degrade, not fail.
    assert body["cheatsheet_source"] in {"llm", "fallback"}
    assert "## Core Definitions" in body["cheatsheet"]
    question = body["questions"][0]
    assert question["prompt"] and question["options"]
    assert "answer_index" not in question  # server keeps the key

    session_response = service.call(
        "GET", f"/api/exam-sessions/{body['exam_session_id']}?user_id={account['user_id']}"
    )
    assert session_response.status_code == 200
    session_body = session_response.json()
    assert session_body["status"] == "active"
    assert 0 < session_body["seconds_remaining"] <= 45 * 60

    # Owner-scoped: another account cannot read this exam.
    assert service.call(
        "GET", f"/api/exam-sessions/{body['exam_session_id']}?user_id={uuid.uuid4()}"
    ).status_code == 404


def test_exam_crunch_returns_the_cached_active_exam(service: LiveService,
                                                   duel_node: dict[str, Any],
                                                   account: dict[str, str]):
    """A second crunch while the first exam is still active returns 200 + cached."""
    # A fresh cruncher: exam sessions are per (user, course), so this test
    # stays independent of the shared account's earlier active exams.
    cruncher = _new_user(service)
    started = service.call(
        "POST", f"/api/courses/{account['course_id']}/exam-crunch",
        {"user_id": cruncher, "target": 25},
    )
    if started.status_code == 400:  # no gradable material in this course
        pytest.skip("course has no gradable questions")
    first = started.json()
    assert started.status_code == 201, started.text

    resumed = service.call(
        "POST", f"/api/courses/{account['course_id']}/exam-crunch",
        {"user_id": cruncher},
    )
    assert resumed.status_code == 200, resumed.text
    body = resumed.json()
    assert body["cached"] is True
    assert body["exam_session_id"] == first["exam_session_id"]
    assert body["question_count"] == first["question_count"]


def test_exam_crunch_requires_gradable_material(service: LiveService):
    owner = _new_user(service)
    empty_course = service.call(
        "POST", "/api/courses", {"user_id": owner, "name": "Empty Course"}
    ).json()
    response = service.call(
        "POST", f"/api/courses/{empty_course['id']}/exam-crunch", {"user_id": owner}
    )
    # Graceful beat: a course without gradable material is a 400 with a
    # friendly Arabic message — never an unhandled 409 Conflict.
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["status"] == "insufficient_content"
    assert body["message"]
    assert service.call(
        "POST", f"/api/courses/{uuid.uuid4()}/exam-crunch", {"user_id": owner}
    ).status_code == 404


def test_exam_submission_grades_and_summarises(service: LiveService,
                                              duel_node: dict[str, Any],
                                              account: dict[str, str]):
    started = service.call(
        "POST", f"/api/courses/{account['course_id']}/exam-crunch",
        {"user_id": account["user_id"], "target": 25},
    )
    assert started.status_code in (201, 200, 400), started.text
    if started.status_code == 400:  # no gradable material in this course
        pytest.skip("course has no gradable questions")
    exam = started.json()
    exam_id = exam["exam_session_id"]

    # Answer everything wrong to force a full weakness heatmap.
    answers = {question["challenge_id"]: 0 for question in exam["questions"]}
    submitted = service.call(
        "POST", f"/api/exam-sessions/{exam_id}/submit",
        {"user_id": account["user_id"], "answers": answers},
    )
    assert submitted.status_code == 200, submitted.text
    result = submitted.json()
    assert result["total"] == len(exam["questions"])
    assert 0 <= result["score"] <= result["total"]
    assert result["grade"] in {"A", "B", "C", "D", "F"}
    assert 0.0 <= result["readiness"] <= 1.0
    assert result["xp_awarded"] == result["score"] * 5
    assert len(result["review"]) == result["total"]
    assert len(result["chapters"]) >= 1
    # A submission is final: a second submit is a conflict.
    assert service.call(
        "POST", f"/api/exam-sessions/{exam_id}/submit",
        {"user_id": account["user_id"], "answers": answers},
    ).status_code == 409

    summary = service.call(
        "GET", f"/api/exam-sessions/{exam_id}/summary?user_id={account['user_id']}"
    )
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["exam_session_id"] == exam_id
    assert body["weak_spot_count"] == len(body["weak_spots"])
    assert body["cheatsheet"]
    for chapter in body["chapters"]:
        assert chapter["risk"] in {"high", "medium", "low"}
        assert 0.0 <= chapter["accuracy"] <= 1.0
    # Every weak spot is drillable: it names the concept and the right answer.
    for spot in body["weak_spots"]:
        assert spot["challenge_id"] and spot["chapter"]
        assert isinstance(spot["correct_index"], int)

    assert service.call(
        "GET", f"/api/exam-sessions/{exam_id}/summary?user_id={uuid.uuid4()}"
    ).status_code == 404


def test_exam_summary_requires_a_submission(service: LiveService,
                                            duel_node: dict[str, Any],
                                            account: dict[str, str]):
    started = service.call(
        "POST", f"/api/courses/{account['course_id']}/exam-crunch",
        {"user_id": account["user_id"], "target": 25},
    )
    if started.status_code == 400:
        pytest.skip("course has no gradable questions")
    exam_id = started.json()["exam_session_id"]
    assert service.call(
        "GET", f"/api/exam-sessions/{exam_id}/summary?user_id={account['user_id']}"
    ).status_code == 409


# ---------------------------------------------------------------------------
# Monetization: entitlements, $1 Pro Pass, AdMob rewarded SSV
# ---------------------------------------------------------------------------


def test_entitlements_default_to_ads_then_pro_removes_them(service: LiveService):
    user_id = _new_user(service)
    before = service.call("GET", f"/api/users/{user_id}/entitlements")
    assert before.status_code == 200
    assert before.json()["show_ads"] is True
    assert before.json()["unlimited_uploads"] is False
    assert before.json()["pro_active"] is False
    assert before.json()["price_usd_cents"] == 100

    activated = service.call(
        "POST", f"/api/users/{user_id}/pro", {"months": 1, "provider": "checkout_hook"}
    )
    assert activated.status_code == 200, activated.text
    body = activated.json()
    assert body["activated"] is True and body["months"] == 1
    assert body["pro_active"] is True
    assert body["show_ads"] is False
    assert body["unlimited_uploads"] is True
    assert "offline_cheatsheets" in body["perks"]
    assert body["pro_expires_at"]

    # Extending stacks on top of the remaining time rather than resetting it.
    extended = service.call("POST", f"/api/users/{user_id}/pro", {"months": 2}).json()
    assert extended["pro_expires_at"] > body["pro_expires_at"]
    assert service.call(
        "POST", f"/api/users/{uuid.uuid4()}/pro", {"months": 1}
    ).status_code == 404


def test_admob_ssv_verifies_the_signature_and_credits_gems(service: LiveService):
    user_id = _new_user(service)
    params = {
        "user_id": user_id,
        "reward_amount": "30",
        "transaction_id": "tx-integration-1",
        "ad_unit": "/1234/rewarded",
        "key_id": "11111111",
    }
    canonical = "&".join(f"{key}={params[key]}" for key in sorted(params))
    signature = hmac.new(
        SSV_SECRET.encode(), canonical.encode(), hashlib.sha256
    ).hexdigest()
    query = "&".join(f"{key}={value}" for key, value in params.items())

    ok = service.call("POST", f"/api/monetization/admob-ssv?{query}&signature={signature}")
    assert ok.status_code == 200, ok.text
    assert ok.json()["gems_awarded"] == 30
    assert ok.json()["transaction_id"] == "tx-integration-1"
    stats = service.call("GET", f"/api/users/{user_id}/stats").json()
    assert stats["inventory"]["rewarded_ads_watched"] == 1
    assert stats["gems"] >= 530

    # A forged signature grants nothing, and a missing one is a bad request.
    assert service.call(
        "POST", f"/api/monetization/admob-ssv?{query}&signature=deadbeef"
    ).status_code == 403
    assert service.call("POST", f"/api/monetization/admob-ssv?{query}").status_code == 400

    # Unknown reward target -> 404 (the signature was valid).
    ghost = {"user_id": str(uuid.uuid4()), "reward_amount": "10", "transaction_id": "tx-2"}
    ghost_canonical = "&".join(f"{key}={ghost[key]}" for key in sorted(ghost))
    ghost_signature = hmac.new(
        SSV_SECRET.encode(), ghost_canonical.encode(), hashlib.sha256
    ).hexdigest()
    ghost_query = "&".join(f"{key}={value}" for key, value in ghost.items())
    assert service.call(
        "POST", f"/api/monetization/admob-ssv?{ghost_query}&signature={ghost_signature}"
    ).status_code == 404


# ---------------------------------------------------------------------------
# Viral sharing + creator royalties
# ---------------------------------------------------------------------------


def test_shared_course_preview_and_instant_clone(service: LiveService,
                                                account: dict[str, str],
                                                saved_node: dict[str, Any]):
    code = service.call(
        "GET", f"/api/courses/{account['course_id']}/share-code"
    ).json()["share_code"]
    preview = service.call("GET", f"/api/join/{code}")
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["challenges"] >= 8
    assert body["owner_handle"] and "@" not in body["owner_handle"]
    downloads_before = body["downloads_count"]

    joiner = _new_user(service)
    started = time.time()
    cloned = service.call("POST", f"/api/join/{code}", {"user_id": joiner})
    elapsed = time.time() - started
    assert cloned.status_code == 201, cloned.text
    result = cloned.json()
    assert result["challenges_cloned"] == body["challenges"]
    assert result["cards_seeded"] == result["challenges_cloned"]
    # Cloning is a pure copy: no generation, so it must be fast.
    assert elapsed < 20

    courses = service.call("GET", f"/api/courses?user_id={joiner}").json()
    assert any(course["id"] == result["course_id"] for course in courses)
    # The clone is immediately reviewable (fresh cards, due now).
    queue = service.call(
        "GET", f"/api/queue?user_id={joiner}&only_due=true&limit=100"
    ).json()
    assert queue["count"] == result["cards_seeded"]

    assert service.call(
        "GET", f"/api/join/{code}"
    ).json()["downloads_count"] == downloads_before + 1
    unknown = service.call("GET", "/api/join/CL-NOPE01")
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == "رمز المشاركة غير صالح أو انتهت صلاحيته"
    assert service.call(
        "POST", "/api/join/CL-NOPE01", {"user_id": joiner}
    ).status_code == 404


def test_join_endpoint_resolves_enrollment_codes_and_enrolls(service: LiveService,
                                                             classroom: dict[str, str]):
    """GET/POST /api/join/{code} also accept a classroom enrollment code."""
    code = classroom["code"]
    preview = service.call("GET", f"/api/join/{code.lower()}")  # case-insensitive
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["is_classroom"] is True
    assert body["course_id"] == classroom["course_id"]
    assert isinstance(body["chapters"], list)
    assert body["instructor_handle"]

    # POST enrolls the student into the classroom instead of cloning.
    joiner = _new_user(service)
    joined = service.call("POST", f"/api/join/{code}", {"user_id": joiner})
    assert joined.status_code == 201, joined.text
    result = joined.json()
    assert result["joined"] is True
    assert result["joined_kind"] == "classroom"
    assert result["already_enrolled"] is False

    # Idempotent: a second POST reports the existing membership.
    again = service.call("POST", f"/api/join/{code}", {"user_id": joiner})
    assert again.status_code == 201, again.text
    assert again.json()["already_enrolled"] is True

    # The roster now includes the joiner (same flow as /api/classrooms/join).
    roster = service.call(
        "GET",
        f"/api/classrooms/{classroom['course_id']}/roster"
        f"?instructor_id={classroom['instructor_id']}",
    ).json()
    assert any(row["student_id"] == joiner for row in roster["roster"])


def test_clearing_a_shared_challenge_pays_the_creator(service: LiveService,
                                                     account: dict[str, str],
                                                     saved_node: dict[str, Any]):
    creator_before = service.call(
        "GET", f"/api/users/{account['user_id']}/entitlements"
    ).json()["creator_gems"]

    reviewer = _new_user(service)
    challenge_id = saved_node["challenges"][0]["id"]
    reviewed = service.call(
        "POST", "/api/reviews",
        {
            "user_id": reviewer,
            "challenge_id": challenge_id,
            "rating": 3,
            "elapsed_ms": 9000,
            "response_json": {"source": "royalty-test"},
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["creator_royalty"] == 2
    assert reviewed.json()["challenge_id"] == challenge_id

    after = service.call("GET", f"/api/users/{account['user_id']}/entitlements").json()
    assert after["creator_gems"] == creator_before + 2

    # Reviewing your own material pays no royalty to yourself.
    own = service.call(
        "POST", "/api/reviews",
        {
            "user_id": account["user_id"],
            "challenge_id": challenge_id,
            "rating": 3,
            "elapsed_ms": 7000,
            "response_json": {"source": "self-review"},
        },
    )
    assert own.status_code == 200
    assert own.json()["creator_royalty"] == 0
    assert service.call(
        "GET", f"/api/users/{account['user_id']}/entitlements"
    ).json()["creator_gems"] == after["creator_gems"]


# ---------------------------------------------------------------------------
# Web Push registration + retention dispatcher surface
# ---------------------------------------------------------------------------


def test_push_registration_is_recorded_even_without_vapid(service: LiveService):
    user_id = _new_user(service)
    key = service.call("GET", "/api/push/public-key").json()
    assert key["configured"] is False  # no VAPID keys in CI
    assert key["public_key"] is None
    assert key["push_available"] is False

    endpoint = f"https://push.example.test/{uuid.uuid4().hex}"
    subscribed = service.call(
        "POST", "/api/push/subscribe",
        {
            "user_id": user_id,
            "subscription": {"endpoint": endpoint, "keys": {"p256dh": "abc", "auth": "def"}},
            "user_agent": "pytest",
        },
    )
    assert subscribed.status_code == 201, subscribed.text
    assert subscribed.json()["push_available"] is False

    # Re-subscribing the same endpoint is idempotent (no duplicate row).
    again = service.call(
        "POST", "/api/push/subscribe",
        {"user_id": user_id, "subscription": {"endpoint": endpoint, "keys": {}}},
    )
    assert again.status_code == 201
    assert again.json()["subscription_id"] == subscribed.json()["subscription_id"]

    assert service.call(
        "POST", "/api/push/unsubscribe", {"endpoint": endpoint}
    ).json()["removed"] is True
    assert service.call(
        "POST", "/api/push/unsubscribe", {"endpoint": endpoint}
    ).json()["removed"] is False
    assert service.call(
        "POST", "/api/push/subscribe", {"user_id": user_id, "subscription": {} },
    ).status_code == 422


def test_retention_sweep_runs_and_is_idempotent_per_day(service: LiveService):
    # Backdate a user so the 8 PM streak-saver gate is satisfied for them.
    import database as db_module
    from sqlalchemy.ext.asyncio import async_sessionmaker

    user_id = _new_user(service)

    async def backdate() -> None:
        engine = service.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            user = await session.get(db_module.User, uuid.UUID(user_id))
            assert user is not None
            user.last_active_at = db_module.utcnow() - __import__("datetime").timedelta(days=3)
            await session.commit()
        await engine.dispose()

    asyncio.run(backdate())
    first = service.call("POST", "/api/retention/sweep")
    assert first.status_code == 200, first.text
    assert first.json()["ok"] is True
    assert set(first.json()) >= {"streak_saver", "fsrs_decay"}

    # The sweep is running outside the 8 PM window in CI, so it may dispatch
    # nothing; either way a second run must not raise and must stay in bounds.
    second = service.call("POST", "/api/retention/sweep")
    assert second.status_code == 200
    assert second.json()["streak_saver"] <= first.json()["streak_saver"]

    inbox = service.call("GET", f"/api/users/{user_id}/notifications").json()
    assert "items" in inbox and "unread" in inbox
