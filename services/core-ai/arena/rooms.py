"""Live 1v1 battle arena.

Room state lives in-process (one uvicorn worker owns a room); fan-out to every
connected socket is done with per-connection :class:`asyncio.Queue`. When a
``REDIS_URL`` is configured *and* the optional ``redis`` client is importable,
every broadcast is additionally published on the ``arena:{code}`` channel so a
second worker can mirror the feed — the in-process queue path always stays
active, so the arena works with zero extra dependencies.

Scoring follows the spec: **accuracy + speed**. A correct answer is worth 100
points plus 2 points for every second left on the 60-second clock.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import string
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("campuslingo.arena")

QUESTION_SECONDS = int(os.getenv("ARENA_QUESTION_SECONDS", "60"))
ROOM_TTL_SECONDS = int(os.getenv("ARENA_ROOM_TTL_SECONDS", str(15 * 60)))
ROOM_CODE_ALPHABET = string.ascii_uppercase + string.digits
ROOM_CODE_LENGTH = 6

# Optional Redis pub/sub. Guarded import: the arena must run with zero new deps.
try:  # pragma: no cover - exercised only when redis is installed
    import redis.asyncio as _redis_async  # type: ignore
except Exception:  # noqa: BLE001
    _redis_async = None


class ArenaError(RuntimeError):
    """Room missing, full, or a protocol violation."""


@dataclass
class Player:
    player_id: str
    name: str
    score: int = 0
    streak: int = 0
    answered: bool = False
    connected: bool = True


@dataclass
class Room:
    code: str
    questions: list[dict[str, Any]]
    players: dict[str, Player] = field(default_factory=dict)
    subscribers: dict[str, asyncio.Queue] = field(default_factory=dict)
    index: int = 0
    status: str = "waiting"  # waiting | playing | finished
    deadline: float = 0.0
    created_at: float = field(default_factory=time.time)
    timer_task: Optional[asyncio.Task] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # One payout per room: the victor's XP/gems are granted exactly once even
    # if both clients race to POST /reward when the podium appears.
    rewarded: bool = False

    def public_state(self) -> dict[str, Any]:
        return {
            "type": "state",
            "room_code": self.code,
            "status": self.status,
            "index": self.index,
            "total": len(self.questions),
            "seconds_left": max(0, round(self.deadline - time.time())) if self.deadline else 0,
            "players": [
                {
                    "player_id": player.player_id,
                    "name": player.name,
                    "score": player.score,
                    "streak": player.streak,
                    "answered": player.answered,
                }
                for player in self.players.values()
            ],
        }


def _shuffled(options: list[str], rng: random.Random) -> list[str]:
    copy = list(options)
    rng.shuffle(copy)
    return copy


def arena_question_from_challenge(
    challenge_type: str, payload: dict[str, Any], *, seed: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """Adapt a stored challenge into a server-gradable arena question.

    Only challenge types with a single, server-checkable answer are eligible
    (MCQ, True/False, Cloze); the rest return ``None`` and are skipped. Returns
    ``{"challenge_id", "type", "prompt", "options", "answer_index"}`` with the
    options already shuffled so the correct index is not positional.
    """
    rng = random.Random(seed)
    if challenge_type == "MCQ_QUIZ":
        options = [option.get("text", "") for option in payload.get("options", [])]
        correct = next(
            (index for index, option in enumerate(payload.get("options", [])) if option.get("correct")),
            None,
        )
        if correct is None or len(options) < 2:
            return None
        answer_text = options[correct]
        shuffled = _shuffled(options, rng)
        return {
            "type": "MCQ_QUIZ",
            "prompt": payload.get("question", ""),
            "options": shuffled,
            "answer_index": shuffled.index(answer_text),
        }
    if challenge_type == "TRUE_FALSE":
        statement = payload.get("statement", "")
        answer = bool(payload.get("answer"))
        options = ["صحيح", "خطأ"]
        return {
            "type": "TRUE_FALSE",
            "prompt": statement,
            "options": options,
            "answer_index": 0 if answer else 1,
        }
    if challenge_type == "CONTEXTUAL_CLOZE":
        answer = payload.get("correct_answer", "")
        options = [answer, *payload.get("distractors", [])]
        if not answer or len(options) < 2:
            return None
        shuffled = _shuffled(options, rng)
        return {
            "type": "CONTEXTUAL_CLOZE",
            "prompt": payload.get("sentence_with_blank", ""),
            "options": shuffled,
            "answer_index": shuffled.index(answer),
        }
    return None


class RoomManager:
    """Owns every live room and the per-room scoring state machine."""

    def __init__(self, question_seconds: int = QUESTION_SECONDS) -> None:
        self.rooms: dict[str, Room] = {}
        self.question_seconds = question_seconds
        self._redis: Any = None
        self._redis_url = os.getenv("REDIS_URL", "").strip()

    # -- lifecycle ---------------------------------------------------------
    def create_room(
        self, questions: list[dict[str, Any]], code: Optional[str] = None
    ) -> Room:
        """Open a room, optionally under a caller-chosen code (e.g. ``TEST01``).

        A custom code is normalised to upper case and must be 4-8 alphanumerics
        with at least one letter; it is rejected when already taken so two
        duels can never share a code.
        """
        if not questions:
            raise ArenaError("no gradable challenges available for a duel")
        self._prune()
        if code:
            candidate = code.strip().upper()
            if not (4 <= len(candidate) <= 8):
                raise ArenaError("room code must be 4-8 characters")
            if not all(char in ROOM_CODE_ALPHABET for char in candidate):
                raise ArenaError("room code may only contain letters and digits")
            if not any(char.isalpha() for char in candidate):
                raise ArenaError("room code must contain at least one letter")
            if candidate in self.rooms:
                raise ArenaError(f"room {candidate} already exists")
            room_code = candidate
        else:
            for _ in range(50):
                room_code = "".join(
                    random.choice(ROOM_CODE_ALPHABET) for _ in range(ROOM_CODE_LENGTH)
                )
                if room_code not in self.rooms:
                    break
            else:  # pragma: no cover - astronomically unlikely
                raise ArenaError("could not allocate a room code")
        room = Room(code=room_code, questions=questions)
        self.rooms[room_code] = room
        logger.info("arena room %s created with %d questions", room_code, len(questions))
        return room

    def get(self, code: str) -> Room:
        room = self.rooms.get(code.upper())
        if room is None:
            raise ArenaError(f"room {code} not found")
        return room

    def discard(self, code: str) -> None:
        room = self.rooms.pop(code.upper(), None)
        if room and room.timer_task and not room.timer_task.done():
            room.timer_task.cancel()

    def _prune(self) -> None:
        now = time.time()
        for code, room in list(self.rooms.items()):
            if now - room.created_at > ROOM_TTL_SECONDS and room.status != "playing":
                self.discard(code)

    # -- fan-out -----------------------------------------------------------
    async def _publish_redis(self, code: str, message: dict[str, Any]) -> None:
        """Mirror a broadcast on Redis (no-op without redis/REDIS_URL)."""
        if not (_redis_async and self._redis_url):
            return
        try:
            if self._redis is None:
                self._redis = _redis_async.from_url(self._redis_url)
            await self._redis.publish(f"arena:{code}", json.dumps(message, default=str))
        except Exception as exc:  # noqa: BLE001 - fan-out is best-effort
            logger.warning("arena redis publish failed: %s", exc)

    async def broadcast(self, room: Room, message: dict[str, Any]) -> None:
        for queue in list(room.subscribers.values()):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:  # pragma: no cover - defensive
                pass
        await self._publish_redis(room.code, message)

    # -- connection --------------------------------------------------------
    async def join(
        self, room: Room, player_id: str, name: str, queue: asyncio.Queue
    ) -> Player:
        if room.status == "finished":
            raise ArenaError("this duel is already over")
        existing = room.players.get(player_id)
        if existing is None:
            if len(room.players) >= 2:
                raise ArenaError("room is full")
            existing = Player(player_id=player_id, name=(name or "Player")[:40])
            room.players[player_id] = existing
        existing.connected = True
        room.subscribers[player_id] = queue
        return existing

    def leave(self, room: Room, player_id: str) -> None:
        room.subscribers.pop(player_id, None)
        player = room.players.get(player_id)
        if player is not None:
            player.connected = False
        if room.status == "waiting" and not any(p.connected for p in room.players.values()):
            self.discard(room.code)

    # -- game loop ---------------------------------------------------------
    async def maybe_start(self, room: Room) -> None:
        async with room.lock:
            if room.status != "waiting" or len(room.players) < 2:
                return
            room.status = "playing"
            room.index = 0
            await self.broadcast(room, {"type": "start", "total": len(room.questions)})
            await self._send_question(room)

    async def _send_question(self, room: Room) -> None:
        question = room.questions[room.index]
        room.deadline = time.time() + self.question_seconds
        for player in room.players.values():
            player.answered = False
        await self.broadcast(
            room,
            {
                "type": "question",
                "index": room.index,
                "total": len(room.questions),
                "question": {
                    "type": question["type"],
                    "prompt": question["prompt"],
                    "options": question["options"],
                },
                "seconds": self.question_seconds,
            },
        )
        await self.broadcast(room, room.public_state())
        if room.timer_task and not room.timer_task.done():
            room.timer_task.cancel()
        room.timer_task = asyncio.create_task(self._question_timeout(room, room.index))

    async def _question_timeout(self, room: Room, index: int) -> None:
        try:
            await asyncio.sleep(self.question_seconds)
        except asyncio.CancelledError:
            return
        async with room.lock:
            if room.status != "playing" or room.index != index:
                return

        await self._advance(room)

    async def handle_message(self, room: Room, player_id: str, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "answer":
            await self._handle_answer(room, player_id, int(message.get("selected", -1)))
        elif kind == "ping":
            await self.broadcast(room, {"type": "pong"})

    async def _handle_answer(self, room: Room, player_id: str, selected: int) -> None:
        async with room.lock:
            player = room.players.get(player_id)
            if player is None or room.status != "playing" or player.answered:
                return
            question = room.questions[room.index]
            remaining = max(0, int(round(room.deadline - time.time())))
            correct = selected == question["answer_index"]
            gained = 0
            if correct:
                player.streak += 1
                gained = 100 + remaining * 2
            else:
                player.streak = 0
            player.score += gained
            player.answered = True
            await self.broadcast(
                room,
                {
                    "type": "answer_ack",
                    "player_id": player_id,
                    "correct": correct,
                    "gained": gained,
                    "seconds_left": remaining,
                },
            )
            await self.broadcast(room, room.public_state())
            if all(p.answered for p in room.players.values()):
                if room.timer_task and not room.timer_task.done():
                    room.timer_task.cancel()
                await self._advance(room)

    async def _advance(self, room: Room) -> None:
        """Reveal the correct answer, then move on or finish."""
        question = room.questions[room.index]
        await self.broadcast(
            room,
            {
                "type": "reveal",
                "index": room.index,
                "answer_index": question["answer_index"],
            },
        )
        if room.index + 1 >= len(room.questions):
            await self._finish(room)
            return
        room.index += 1
        await asyncio.sleep(1.2)  # short beat between questions
        await self._send_question(room)

    async def _finish(self, room: Room) -> None:
        room.status = "finished"
        ranked = sorted(room.players.values(), key=lambda p: p.score, reverse=True)
        winner = ranked[0] if ranked else None
        await self.broadcast(
            room,
            {
                "type": "finish",
                "winner": winner.player_id if winner else None,
                "standings": [
                    {"player_id": p.player_id, "name": p.name, "score": p.score}
                    for p in ranked
                ],
            },
        )
        logger.info("arena room %s finished", room.code)


# Process-wide singleton used by the FastAPI app.
room_manager = RoomManager()
