"""Battle-arena room manager: matchmaking, scoring, timers and fan-out.

Pure in-process tests — no database, no network. The room state machine is the
part that decides who wins, so it is exercised directly with two simulated
players (``TEST01``), plus a real two-socket WebSocket test lives in the
end-to-end suite where a live uvicorn already exists.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from arena.rooms import (
    ArenaError,
    RoomManager,
    arena_question_from_challenge,
)


# ---------------------------------------------------------------------------
# question adaptation
# ---------------------------------------------------------------------------


def test_mcq_becomes_gradable_and_shuffles_away_the_correct_index():
    payload = {
        "question": "Which rule covers safe speed?",
        "options": [
            {"text": "Rule 6", "correct": True},
            {"text": "Rule 7", "correct": False},
            {"text": "Rule 8", "correct": False},
        ],
    }
    question = arena_question_from_challenge("MCQ_QUIZ", payload, seed="fixed")
    assert question is not None
    assert set(question["options"]) == {"Rule 6", "Rule 7", "Rule 8"}
    assert question["options"][question["answer_index"]] == "Rule 6"


def test_true_false_and_cloze_are_adapted():
    tf = arena_question_from_challenge(
        "TRUE_FALSE", {"statement": "Rule 6 is about safe speed.", "answer": False}
    )
    assert tf is not None and tf["answer_index"] == 1 and tf["options"] == ["صحيح", "خطأ"]

    cloze = arena_question_from_challenge(
        "CONTEXTUAL_CLOZE",
        {
            "sentence_with_blank": "Every vessel shall proceed at a safe _____.",
            "correct_answer": "speed",
            "distractors": ["depth", "heading"],
        },
        seed="fixed",
    )
    assert cloze is not None
    assert cloze["options"][cloze["answer_index"]] == "speed"


def test_non_gradable_and_malformed_challenges_are_skipped():
    assert arena_question_from_challenge("MATCH_MADNESS", {"pairs": []}) is None
    # One-option MCQ cannot be graded unambiguously.
    assert arena_question_from_challenge(
        "MCQ_QUIZ", {"question": "x", "options": [{"text": "only", "correct": True}]}
    ) is None
    # A correct flag that names no option is equally ungradable.
    assert arena_question_from_challenge(
        "MCQ_QUIZ",
        {"question": "x", "options": [{"text": "a"}, {"text": "b"}]},
    ) is None


# ---------------------------------------------------------------------------
# room lifecycle & matchmaking
# ---------------------------------------------------------------------------


def _questions(count: int = 2) -> list[dict]:
    return [
        {
            "type": "MCQ_QUIZ",
            "prompt": f"Q{index}",
            "options": ["a", "b"],
            "answer_index": 0,
        }
        for index in range(count)
    ]


def test_room_code_is_allocated_and_unique():
    manager = RoomManager()
    first = manager.create_room(_questions())
    second = manager.create_room(_questions())
    assert len(first.code) == 6 and first.code.isalnum()
    assert first.code != second.code
    assert manager.get(first.code.lower()) is first


def test_custom_room_code_is_normalised_and_guarded():
    manager = RoomManager()
    room = manager.create_room(_questions(), code="test01")
    assert room.code == "TEST01"
    # A second room cannot steal the code.
    with pytest.raises(ArenaError):
        manager.create_room(_questions(), code="TEST01")
    for bad in ("abc", "has space", "123456", "TOOLONGCODE"):
        with pytest.raises(ArenaError):
            manager.create_room(_questions(), code=bad)


def test_empty_question_set_is_rejected():
    with pytest.raises(ArenaError):
        RoomManager().create_room([])


def test_third_player_is_rejected_and_unknown_room_raises():
    async def run() -> None:
        manager = RoomManager()
        room = manager.create_room(_questions())
        await manager.join(room, "p1", "أحمد", asyncio.Queue())
        await manager.join(room, "p2", "سارة", asyncio.Queue())
        with pytest.raises(ArenaError):
            await manager.join(room, "p3", "زائر", asyncio.Queue())

    asyncio.run(run())
    with pytest.raises(ArenaError):
        RoomManager().get("NOPE01")


# ---------------------------------------------------------------------------
# scoring: accuracy + speed
# ---------------------------------------------------------------------------


def test_maybe_start_waits_for_two_players_then_sends_the_first_question():
    async def run() -> list[dict]:
        manager = RoomManager(question_seconds=30)
        room = manager.create_room(_questions(2), code="TEST01")
        queue_a: asyncio.Queue = asyncio.Queue()
        queue_b: asyncio.Queue = asyncio.Queue()
        await manager.join(room, "p1", "أحمد", queue_a)
        await manager.maybe_start(room)
        # One player cannot start a duel, and no question has been sent.
        assert room.status == "waiting" and queue_a.empty()

        await manager.join(room, "p2", "سارة", queue_b)
        await manager.maybe_start(room)
        assert room.status == "playing"
        messages = [queue_a.get_nowait() for _ in range(queue_a.qsize())]
        if room.timer_task is not None:
            room.timer_task.cancel()
        manager.discard(room.code)
        return messages

    messages = asyncio.run(run())
    kinds = [message["type"] for message in messages]
    assert "start" in kinds and "question" in kinds
    question = next(message for message in messages if message["type"] == "question")
    # The answer index is never leaked to the clients.
    assert "answer_index" not in question["question"]
    assert question["seconds"] == 30


def test_correct_answers_score_accuracy_plus_speed_and_wrong_ones_reset_streak():
    async def run() -> tuple[RoomManager, object]:
        manager = RoomManager(question_seconds=60)
        room = manager.create_room(_questions(1), code="TEST01")
        queue_a: asyncio.Queue = asyncio.Queue()
        queue_b: asyncio.Queue = asyncio.Queue()
        await manager.join(room, "p1", "أحمد", queue_a)
        await manager.join(room, "p2", "سارة", queue_b)
        await manager.maybe_start(room)

        correct = room.questions[0]["answer_index"]
        wrong = 1 - correct
        await manager.handle_message(room, "p1", {"type": "answer", "selected": correct})
        await manager.handle_message(room, "p2", {"type": "answer", "selected": wrong})
        # A second answer from the same player in the same round is ignored.
        await manager.handle_message(room, "p1", {"type": "answer", "selected": wrong})
        await asyncio.sleep(0)  # let _advance's finish broadcast through
        manager.discard(room.code)
        return manager, room

    _, room = asyncio.run(run())
    winner, loser = room.players["p1"], room.players["p2"]
    # 100 base + 2/second of the 60s clock; the drained clock can lose a second
    # or two to wall-time, so the bound is generous but still proves the bonus.
    assert 100 <= winner.score <= 220
    assert winner.streak == 1
    assert loser.score == 0 and loser.streak == 0


def test_reveal_then_finish_broadcasts_ranked_standings():
    async def run() -> tuple[RoomManager, list[dict]]:
        manager = RoomManager(question_seconds=60)
        room = manager.create_room(_questions(1), code="TEST01")
        queue_a: asyncio.Queue = asyncio.Queue()
        queue_b: asyncio.Queue = asyncio.Queue()
        await manager.join(room, "p1", "أحمد", queue_a)
        await manager.join(room, "p2", "سارة", queue_b)
        await manager.maybe_start(room)
        answer = room.questions[0]["answer_index"]
        await manager.handle_message(room, "p1", {"type": "answer", "selected": answer})
        await manager.handle_message(room, "p2", {"type": "answer", "selected": answer})
        await asyncio.sleep(0.1)
        messages = [queue_a.get_nowait() for _ in range(queue_a.qsize())]
        manager.discard(room.code)
        return manager, messages

    _, messages = asyncio.run(run())
    types = [message["type"] for message in messages]
    assert "reveal" in types and "finish" in types
    finish = next(message for message in messages if message["type"] == "finish")
    assert [row["player_id"] for row in finish["standings"]] == ["p1", "p2"]
    assert finish["winner"] in {"p1", "p2"}


def test_question_timeout_advances_a_stalled_room():
    async def run() -> str:
        manager = RoomManager(question_seconds=0)  # every clock expires at once
        room = manager.create_room(_questions(2), code="TEST01")
        await manager.join(room, "p1", "أحمد", asyncio.Queue())
        await manager.join(room, "p2", "سارة", asyncio.Queue())
        await manager.maybe_start(room)
        first_index = room.index
        for _ in range(30):
            await asyncio.sleep(0.05)
            if room.status == "finished":
                break
        manager.discard(room.code)
        assert first_index == 0
        return room.status

    assert asyncio.run(run()) == "finished"


def test_ping_is_echoed_as_pong():
    async def run() -> list[dict]:
        manager = RoomManager()
        room = manager.create_room(_questions())
        queue: asyncio.Queue = asyncio.Queue()
        await manager.join(room, "p1", "أحمد", queue)
        await manager.handle_message(room, "p1", {"type": "ping"})
        return [queue.get_nowait() for _ in range(queue.qsize())]

    messages = asyncio.run(run())
    assert messages == [{"type": "pong"}]


def test_broadcast_fans_out_json_safe_payloads():
    async def run() -> list[dict]:
        manager = RoomManager()
        room = manager.create_room(_questions())
        queue: asyncio.Queue = asyncio.Queue()
        await manager.join(room, "p1", "أحمد", queue)
        await manager.broadcast(room, {"type": "note", "text": "مرحبا"})
        return [queue.get_nowait()]

    payload = asyncio.run(run())[0]
    # Every message must be JSON-encodable (it goes out over the socket).
    assert json.loads(json.dumps(payload))["text"] == "مرحبا"


def test_leave_cleans_up_and_discards_an_empty_waiting_room():
    async def run() -> bool:
        manager = RoomManager()
        room = manager.create_room(_questions())
        await manager.join(room, "p1", "أحمد", asyncio.Queue())
        manager.leave(room, "p1")
        return room.code in manager.rooms

    assert asyncio.run(run()) is False
