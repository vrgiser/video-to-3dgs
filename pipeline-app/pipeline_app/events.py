"""In-process event bus for progress streaming (SSE fanout)."""
from __future__ import annotations

import asyncio
import queue
import threading
from collections import defaultdict
from typing import Iterator

from .schemas import ProgressUpdate


class EventBus:
    """Thread-safe pub/sub keyed by job_id.

    Workers (running in background threads) call publish(); SSE handlers
    (running in the FastAPI event loop) call subscribe() and iterate.

    Backed by asyncio.Queue — calls from threads use run_coroutine_threadsafe.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, job_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        with self._lock:
            self._subscribers[job_id].append(q)
        return q

    def unsubscribe(self, job_id: str, q: asyncio.Queue) -> None:
        with self._lock:
            subs = self._subscribers.get(job_id, [])
            if q in subs:
                subs.remove(q)

    async def _publish_async(self, job_id: str, event: dict) -> None:
        with self._lock:
            subs = list(self._subscribers.get(job_id, []))
        for q in subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Drop oldest; client is slow. Keeps memory bounded.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    pass

    def publish_threadsafe(
        self, job_id: str, event: dict, loop: asyncio.AbstractEventLoop
    ) -> None:
        asyncio.run_coroutine_threadsafe(
            self._publish_async(job_id, event), loop
        )


# Module-level singleton (one per API process; sufficient for MVP)
bus = EventBus()