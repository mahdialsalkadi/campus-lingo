"""Real-time classroom update fan-out (instructor -> enrolled students).

An instructor's upload has to appear on every enrolled student's dashboard
without a refresh. The transport is Server-Sent Events, because it is
one-directional, proxy-friendly and needs no client library:

    GET /api/classrooms/{course_id}/updates   ->  text/event-stream

Fan-out is in-process: every connected student owns an :class:`asyncio.Queue`
and ``publish`` drops the event into each one. When ``REDIS_URL`` is configured
*and* the optional ``redis`` client is importable, the same payload is mirrored
on the ``classroom:{course_id}:updates`` channel so a second worker can relay
it — dashboards work with zero extra dependencies either way.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

logger = logging.getLogger("campuslingo.classroom")

# Optional Redis mirror; guarded import keeps the service dependency-free.
try:  # pragma: no cover - depends on the deployment
    import redis.asyncio as _redis_async  # type: ignore
except Exception:  # noqa: BLE001
    _redis_async = None

CHANNEL_TEMPLATE = "classroom:{course_id}:updates"
QUEUE_MAXSIZE = 128


def channel_for(course_id: Any) -> str:
    return CHANNEL_TEMPLATE.format(course_id=course_id)


class ClassroomHub:
    """Per-course subscriber registry + publisher."""

    def __init__(self) -> None:
        self._subscribers: dict[str, dict[str, asyncio.Queue]] = {}
        self._redis: Any = None
        self._redis_url = os.getenv("REDIS_URL", "").strip()

    # -- subscription ------------------------------------------------------
    def subscribe(self, course_id: Any, client_id: str) -> asyncio.Queue:
        key = str(course_id)
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._subscribers.setdefault(key, {})[client_id] = queue
        logger.debug("classroom %s: %s subscribed", key, client_id)
        return queue

    def unsubscribe(self, course_id: Any, client_id: str) -> None:
        key = str(course_id)
        bucket = self._subscribers.get(key)
        if not bucket:
            return
        bucket.pop(client_id, None)
        if not bucket:
            self._subscribers.pop(key, None)

    def subscriber_count(self, course_id: Any) -> int:
        return len(self._subscribers.get(str(course_id), {}))

    # -- publishing --------------------------------------------------------
    async def _publish_redis(self, course_id: Any, event: dict[str, Any]) -> None:
        if not (_redis_async and self._redis_url):
            return
        try:
            if self._redis is None:
                self._redis = _redis_async.from_url(self._redis_url)
            await self._redis.publish(
                channel_for(course_id), json.dumps(event, default=str)
            )
        except Exception as exc:  # noqa: BLE001 - mirroring is best-effort
            logger.warning("classroom redis publish failed: %s", exc)

    async def publish(self, course_id: Any, event: dict[str, Any]) -> int:
        """Deliver an event to every listener; returns how many got it."""
        payload = {
            **event,
            "course_id": str(course_id),
            "at": event.get("at") or datetime.now(timezone.utc).isoformat(),
        }
        delivered = 0
        for queue in list(self._subscribers.get(str(course_id), {}).values()):
            try:
                queue.put_nowait(payload)
                delivered += 1
            except asyncio.QueueFull:  # pragma: no cover - slow consumer
                pass
        await self._publish_redis(course_id, payload)
        logger.info("classroom %s: published %s to %d listener(s)",
                    course_id, payload.get("type"), delivered)
        return delivered

    # -- SSE adaptation ----------------------------------------------------
    async def stream(
        self, course_id: Any, client_id: str, *, keepalive_seconds: float = 20.0
    ) -> AsyncIterator[str]:
        """Yield SSE frames: the ``connected`` handshake, then events.

        A periodic comment frame keeps intermediary proxies from closing an
        idle stream, and ``asyncio.CancelledError`` (client disconnect) unwinds
        cleanly through the ``finally`` so the registry never leaks.
        """
        queue = self.subscribe(course_id, client_id)
        try:
            yield _sse(
                {
                    "type": "connected",
                    "course_id": str(course_id),
                    "client_id": client_id,
                }
            )
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=keepalive_seconds)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _sse(event)
        finally:
            self.unsubscribe(course_id, client_id)


def _sse(event: dict[str, Any]) -> str:
    """Encode one SSE frame (``event:`` name plus JSON ``data:`` line)."""
    name = str(event.get("type") or "message")
    return f"event: {name}\ndata: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


# Process-wide singleton used by the FastAPI app.
classroom_hub = ClassroomHub()
