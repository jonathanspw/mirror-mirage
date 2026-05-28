"""Per-CDN debouncing and bulk-batching of purge targets.

The batcher accumulates :class:`~mirror_mirage.mapper.PurgeTarget` instances in
a per-CDN buffer and flushes them to the persistent queue under three
conditions:

1. **Quiet window** — ``batch_quiet_window_seconds`` of inactivity on
   that CDN.
2. **Size cap** — buffer reaches ``batch_max_size`` URLs.
3. **Age cap** — the oldest URL in the buffer has been waiting longer
   than ``batch_max_age_seconds``.

Each CDN's buffer and timing are independent: a flood on one CDN never
delays flushes for another.

Tests inject a fake queue (any object with ``async def enqueue(cdn_name,
urls)``) to verify behavior without touching SQLite.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from mirror_mirage.config import DaemonConfig
from mirror_mirage.mapper import PurgeTarget

_log = logging.getLogger("mirror_mirage.batcher")


class EnqueueSink(Protocol):
    """The subset of :class:`~mirror_mirage.queue.PersistentQueue` the batcher uses."""

    async def enqueue(self, cdn_name: str, urls: list[str]) -> int: ...


class Batcher:
    """Coalesces purge targets and submits batches to the queue."""

    def __init__(self, queue: EnqueueSink, daemon_cfg: DaemonConfig) -> None:
        self._queue = queue
        self._cfg = daemon_cfg
        self._buffers: dict[str, set[str]] = {}
        self._first_added: dict[str, float] = {}
        self._last_added: dict[str, float] = {}
        self._wake: dict[str, asyncio.Event] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    async def submit(self, target: PurgeTarget) -> None:
        """Record one purge target. Wakes the per-CDN debounce task."""
        cdn = target.cdn_name
        async with self._lock:
            if cdn not in self._buffers:
                self._buffers[cdn] = set()
                self._wake[cdn] = asyncio.Event()
                self._tasks[cdn] = asyncio.create_task(
                    self._debounce_loop(cdn),
                    name=f"mirror-mirage-batcher-{cdn}",
                )
            buf = self._buffers[cdn]
            now = asyncio.get_event_loop().time()
            if not buf:
                self._first_added[cdn] = now
            buf.add(target.url)
            self._last_added[cdn] = now
            self._wake[cdn].set()
            if len(buf) >= self._cfg.batch_max_size:
                await self._flush_locked(cdn)

    async def run(self) -> None:
        """Run the batcher's coordinator until canceled.

        Spawns one debounce task per CDN as targets first arrive for it.
        Cancellation drains all buffers via :meth:`flush_all` before
        returning, so no work is lost on graceful shutdown.
        """
        try:
            await asyncio.Event().wait()  # block until canceled
        except asyncio.CancelledError:
            for task in list(self._tasks.values()):
                task.cancel()
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
            try:
                await self.flush_all()
            except Exception:
                _log.exception("event=batcher_shutdown_flush_error")
            raise

    async def flush_all(self) -> None:
        """Flush every non-empty buffer immediately, regardless of timers."""
        async with self._lock:
            for cdn in list(self._buffers):
                await self._flush_locked(cdn)

    async def _flush_locked(self, cdn: str) -> None:
        """Flush ``cdn``'s buffer. Caller must hold ``self._lock``."""
        buf = self._buffers.get(cdn)
        if not buf:
            return
        urls = list(buf)
        buf.clear()
        self._first_added.pop(cdn, None)
        self._last_added.pop(cdn, None)
        await self._queue.enqueue(cdn, urls)

    async def _debounce_loop(self, cdn: str) -> None:
        wake = self._wake[cdn]
        while True:
            await wake.wait()
            wake.clear()
            while True:
                async with self._lock:
                    buf = self._buffers.get(cdn)
                    if not buf:
                        break
                    now = asyncio.get_event_loop().time()
                    quiet_at = self._last_added[cdn] + self._cfg.batch_quiet_window_seconds
                    age_at = self._first_added[cdn] + self._cfg.batch_max_age_seconds
                    wait = min(quiet_at, age_at) - now

                if wait <= 0:
                    async with self._lock:
                        await self._flush_locked(cdn)
                    break

                try:
                    await asyncio.wait_for(wake.wait(), wait)
                    wake.clear()
                    # New activity arrived; reassess deadlines.
                except TimeoutError:
                    async with self._lock:
                        await self._flush_locked(cdn)
                    break
