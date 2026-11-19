"""Event bus for streaming agent activity to dashboards.

In-process fan-out by default; when REDIS_URL is set, events go through Redis pub/sub so
every Cloud Run instance sees every event.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)
CHANNEL = "agentic-trader:events"

# The agent run that the current coroutine belongs to (so tools can tag their events).
current_run_id: ContextVar[str | None] = ContextVar("current_run_id", default=None)


class EventBus:
    def __init__(self, redis_url: str | None = None) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._redis_url = redis_url
        self._redis = None
        self._listener: asyncio.Task | None = None

    async def start(self) -> None:
        if not self._redis_url:
            return
        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
            await self._redis.ping()
            self._listener = asyncio.create_task(self._listen())
            log.info("Event bus using Redis pub/sub")
        except Exception as exc:  # pragma: no cover - depends on infra
            log.warning("Redis unavailable (%s); falling back to in-process events", exc)
            self._redis = None

    async def stop(self) -> None:
        if self._listener:
            self._listener.cancel()
        if self._redis:
            await self._redis.aclose()

    async def _listen(self) -> None:  # pragma: no cover - depends on infra
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(CHANNEL)
        async for msg in pubsub.listen():
            if msg.get("type") == "message":
                self._fanout(json.loads(msg["data"]))

    def _fanout(self, event: dict) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def publish(self, type_: str, **data: Any) -> None:
        event = {
            "type": type_,
            "run_id": data.pop("run_id", None) or current_run_id.get(),
            "ts": datetime.now(timezone.utc).isoformat(),
            **data,
        }
        if self._redis:
            await self._redis.publish(CHANNEL, json.dumps(event, default=str))
        else:
            self._fanout(event)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)
