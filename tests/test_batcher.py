"""Tests for :mod:`mirror_mirage.batcher`.

Per the documented contract, the batcher flushes a per-CDN buffer to the
queue under three conditions: quiet-window expiry, size cap, or age cap.
Per-CDN buffers are independent: a flood on one CDN must not delay
flushes for another.

We use a fake queue that records every ``enqueue`` call so we can assert
on the timing and shape of batches without touching SQLite.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from mirror_mirage.batcher import Batcher
from mirror_mirage.config import DaemonConfig, RetryConfig
from mirror_mirage.mapper import PurgeTarget


@dataclass
class FakeQueue:
    """Records every enqueue call (cdn_name, frozenset(urls), monotonic_t)."""

    calls: list[tuple[str, frozenset[str], float]] = field(default_factory=list)
    _next_id: int = 0

    async def enqueue(self, cdn_name: str, urls: list[str]) -> int:
        self.calls.append((cdn_name, frozenset(urls), asyncio.get_event_loop().time()))
        self._next_id += 1
        return self._next_id


@pytest.fixture
def fast_cfg() -> DaemonConfig:
    """Sub-second timings so tests stay fast."""
    return DaemonConfig(
        batch_quiet_window_seconds=0.05,
        batch_max_size=5,
        batch_max_age_seconds=0.5,
        queue_db="/tmp/unused.db",  # never opened
        retry=RetryConfig(),
    )


async def _run_batcher_for(batcher: Batcher, seconds: float) -> None:
    """Run the batcher coordinator for a bounded time and then cancel."""
    task = asyncio.create_task(batcher.run())
    try:
        await asyncio.sleep(seconds)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestQuietWindow:
    async def test_single_submit_flushes_after_quiet_window(self, fast_cfg: DaemonConfig) -> None:
        q = FakeQueue()
        b = Batcher(q, fast_cfg)
        task = asyncio.create_task(b.run())
        try:
            await b.submit(PurgeTarget("fastly_main", "https://x/a"))
            # Within the quiet window: no flush yet.
            await asyncio.sleep(fast_cfg.batch_quiet_window_seconds / 2)
            assert q.calls == []
            # After the quiet window: one flush.
            await asyncio.sleep(fast_cfg.batch_quiet_window_seconds * 2)
            assert len(q.calls) == 1
            cdn, urls, _ = q.calls[0]
            assert cdn == "fastly_main"
            assert urls == frozenset({"https://x/a"})
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def test_burst_coalesces_into_one_batch(self, fast_cfg: DaemonConfig) -> None:
        q = FakeQueue()
        b = Batcher(q, fast_cfg)
        task = asyncio.create_task(b.run())
        try:
            for i in range(4):  # below batch_max_size=5
                await b.submit(PurgeTarget("fastly_main", f"https://x/{i}"))
            await asyncio.sleep(fast_cfg.batch_quiet_window_seconds * 3)
            assert len(q.calls) == 1
            _, urls, _ = q.calls[0]
            assert len(urls) == 4
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


class TestSizeCap:
    async def test_size_cap_triggers_early_flush(self, fast_cfg: DaemonConfig) -> None:
        q = FakeQueue()
        b = Batcher(q, fast_cfg)
        task = asyncio.create_task(b.run())
        try:
            # batch_max_size = 5; submitting 5 should flush without waiting
            # the full quiet window.
            for i in range(fast_cfg.batch_max_size):
                await b.submit(PurgeTarget("fastly_main", f"https://x/{i}"))
            # Wait far less than the quiet window.
            await asyncio.sleep(fast_cfg.batch_quiet_window_seconds / 5)
            assert len(q.calls) == 1
            _, urls, _ = q.calls[0]
            assert len(urls) == fast_cfg.batch_max_size
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


class TestAgeCap:
    async def test_age_cap_forces_flush_during_continuous_load(
        self, fast_cfg: DaemonConfig
    ) -> None:
        q = FakeQueue()
        b = Batcher(q, fast_cfg)
        task = asyncio.create_task(b.run())
        try:
            # Steady drip just under the quiet window: would keep resetting
            # the timer forever, but age cap forces a flush.
            urls_seen: list[str] = []
            start = asyncio.get_event_loop().time()
            i = 0
            while asyncio.get_event_loop().time() - start < fast_cfg.batch_max_age_seconds * 2:
                url = f"https://x/{i}"
                await b.submit(PurgeTarget("fastly_main", url))
                urls_seen.append(url)
                i += 1
                await asyncio.sleep(fast_cfg.batch_quiet_window_seconds / 2)
            assert len(q.calls) >= 1
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


class TestPerCdnIsolation:
    async def test_one_cdns_burst_doesnt_delay_another(self, fast_cfg: DaemonConfig) -> None:
        q = FakeQueue()
        b = Batcher(q, fast_cfg)
        task = asyncio.create_task(b.run())
        try:
            # CDN A: keep its timer resetting.
            async def keep_a_busy() -> None:
                for i in range(20):
                    await b.submit(PurgeTarget("a", f"https://a/{i}"))
                    await asyncio.sleep(fast_cfg.batch_quiet_window_seconds / 4)

            busy = asyncio.create_task(keep_a_busy())
            # CDN B: one URL, then silence.
            await b.submit(PurgeTarget("b", "https://b/only"))
            await asyncio.sleep(fast_cfg.batch_quiet_window_seconds * 3)
            await busy
            # B's batch should have flushed despite A's continuous activity.
            cdns = {call[0] for call in q.calls}
            assert "b" in cdns
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


class TestFlushAll:
    async def test_flush_all_drains_every_buffer(self, fast_cfg: DaemonConfig) -> None:
        q = FakeQueue()
        b = Batcher(q, fast_cfg)
        task = asyncio.create_task(b.run())
        try:
            await b.submit(PurgeTarget("a", "https://a/1"))
            await b.submit(PurgeTarget("b", "https://b/1"))
            await b.flush_all()
            cdns = {call[0] for call in q.calls}
            assert cdns == {"a", "b"}
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def test_duplicate_urls_in_same_batch_dedup(self, fast_cfg: DaemonConfig) -> None:
        q = FakeQueue()
        b = Batcher(q, fast_cfg)
        task = asyncio.create_task(b.run())
        try:
            for _ in range(3):
                await b.submit(PurgeTarget("a", "https://a/same"))
            await b.flush_all()
            assert len(q.calls) == 1
            _, urls, _ = q.calls[0]
            assert urls == frozenset({"https://a/same"})
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
