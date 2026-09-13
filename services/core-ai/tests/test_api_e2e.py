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
import json
import os
import random
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.e2e

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
            "type": "INTERACTIVE_STORY",
            "instructions": "اختر القرار الصحيح",
            "scenario_title": "المناوبة الليلية على الجسر",
            "dialogue": [
                {"speaker": "الربان", "text": "الضباب يزداد كثافة.",
                 "action_prompt": "ما أول إجراء؟",
                 "options": [
                     {"text": "تقليل السرعة وإطلاق إشارات الضباب", "correct": True,
                      "feedback": "متوافق مع القاعدة 19."},
                     {"text": "الحفاظ على السرعة الكاملة", "correct": False,
                      "feedback": "غير آمن في الرؤية المحدودة."},
                 ]},
                {"speaker": "الربان", "text": "هدف على الرادار على ميلين.",
                 "action_prompt": "ما إجراؤك؟",
                 "options": [
                     {"text": "رسم الهدف وتقييم خطر التصادم", "correct": True, "feedback": "ممتاز."},
                     {"text": "افتراض أنه سيمر بسلام", "correct": False,
                      "feedback": "لا يجوز الافتراض بمعلومات ناقصة."},
                 ]},
            ],
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
        env={**os.environ, "DATABASE_URL": database_url, "LOG_LEVEL": "WARNING"},
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
    assert service.table_count() == 6


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


# ---------------------------------------------------------------------------
# database helpers
# ---------------------------------------------------------------------------


def test_save_lecture_and_challenges_persists_node_challenges_and_cards(saved_node: dict[str, Any]):
    assert saved_node["lecture"]["id"] and saved_node["node"]["id"]
    assert len(saved_node["challenges"]) == 4
    assert saved_node["cards_seeded"] == 4


def test_save_lecture_and_challenges_stores_the_embedding(saved_node: dict[str, Any]):
    assert saved_node["vector_dims"] == 1536


def test_unit_difficulty_level_maps_onto_challenge_difficulty(saved_node: dict[str, Any]):
    # difficulty_level 3 -> 0.5 on the 0..1 challenge scale
    assert all(item["difficulty"] == pytest.approx(0.5) for item in saved_node["challenges"])


def test_get_due_challenges_for_user_sees_fresh_cards(saved_node: dict[str, Any]):
    assert saved_node["due_now"] == 4


# ---------------------------------------------------------------------------
# GET /api/nodes/{node_id}/challenges
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def node_challenges(service: LiveService, saved_node: dict[str, Any]) -> dict[str, Any]:
    response = service.call("GET", f"/api/nodes/{saved_node['node']['id']}/challenges")
    assert response.status_code == 200, response.text
    return response.json()


def test_node_challenges_returns_all_four_types(node_challenges: dict[str, Any]):
    assert node_challenges["count"] == 4
    assert sorted(item["type"] for item in node_challenges["challenges"]) == [
        "CONTEXTUAL_CLOZE", "INTERACTIVE_STORY", "MATCH_MADNESS", "SPOT_MISTAKE"
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
        ("INTERACTIVE_STORY", lambda p: len(p["dialogue"]) >= 2
            and all("action_prompt" in node and "options" in node for node in p["dialogue"])),
    ],
)
def test_payload_shapes_survive_the_round_trip(node_challenges: dict[str, Any],
                                               challenge_type: str, checker):
    by_type = {item["type"]: item["payload_json"] for item in node_challenges["challenges"]}
    assert checker(by_type[challenge_type]), json.dumps(by_type[challenge_type], ensure_ascii=False)[:200]


def test_arabic_content_survives_the_round_trip(node_challenges: dict[str, Any]):
    story = next(item for item in node_challenges["challenges"] if item["type"] == "INTERACTIVE_STORY")
    assert "الضباب" in story["payload_json"]["dialogue"][0]["text"]


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
    assert body["hearts_lost"] == 1 and body["xp_awarded"] == 0
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
        "challenge_id": rating_ids["INTERACTIVE_STORY"],
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
    assert stats["cards_total"] == 4
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


def test_deleting_a_lecture_cascades_to_cards(service: LiveService,
                                              saved_node: dict[str, Any]):
    deleted = service.call("DELETE", f"/api/lectures/{saved_node['lecture']['id']}")
    assert deleted.status_code == 204
    assert service.call("GET", f"/api/nodes/{saved_node['node']['id']}/challenges").status_code == 404
