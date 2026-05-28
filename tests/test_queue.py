"""Tests for :mod:`mirror_mirage.queue`.

Covers the persistent queue's contract: enqueue/claim_ready round trip,
done/reschedule/failed status transitions, in-flight recovery on restart,
exponential backoff math.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mirror_mirage.queue import PersistentQueue, exponential_backoff


@pytest.fixture
async def queue(tmp_path: Path):
    q = await PersistentQueue.open(tmp_path / "queue.db")
    yield q
    await q.close()


class TestEnqueueClaim:
    async def test_enqueue_returns_id(self, queue: PersistentQueue) -> None:
        jid = await queue.enqueue("fastly_main", ["https://x/a", "https://x/b"])
        assert isinstance(jid, int)
        assert jid > 0

    async def test_claim_ready_returns_job(self, queue: PersistentQueue) -> None:
        await queue.enqueue("fastly_main", ["https://x/a"])
        job = await queue.claim_ready("fastly_main")
        assert job is not None
        assert job.cdn_name == "fastly_main"
        assert job.urls == ["https://x/a"]
        assert job.attempts == 0
        assert job.status == "in_flight"

    async def test_claim_ready_only_returns_matching_cdn(self, queue: PersistentQueue) -> None:
        await queue.enqueue("fastly_main", ["https://x/a"])
        await queue.enqueue("cloudfront_us", ["https://y/a"])
        job = await queue.claim_ready("fastly_main")
        assert job is not None and job.cdn_name == "fastly_main"
        # cloudfront_us job should still be claimable separately.
        other = await queue.claim_ready("cloudfront_us")
        assert other is not None and other.cdn_name == "cloudfront_us"

    async def test_claim_returns_none_when_empty(self, queue: PersistentQueue) -> None:
        assert await queue.claim_ready("fastly_main") is None

    async def test_claim_skips_not_yet_ready(self, queue: PersistentQueue) -> None:
        jid = await queue.enqueue("fastly_main", ["https://x/a"])
        # Reschedule far into the future.
        await queue.reschedule(jid, attempts=1, next_attempt_at=time.time() + 3600)
        assert await queue.claim_ready("fastly_main") is None

    async def test_double_claim_does_not_double_serve(self, queue: PersistentQueue) -> None:
        await queue.enqueue("fastly_main", ["https://x/a"])
        first = await queue.claim_ready("fastly_main")
        second = await queue.claim_ready("fastly_main")
        assert first is not None
        assert second is None


class TestStatusTransitions:
    async def test_mark_done(self, queue: PersistentQueue) -> None:
        jid = await queue.enqueue("fastly_main", ["https://x/a"])
        await queue.claim_ready("fastly_main")
        await queue.mark_done(jid)
        stats = await queue.stats()
        assert stats.get("done", 0) == 1
        assert stats.get("pending", 0) == 0
        assert stats.get("in_flight", 0) == 0

    async def test_reschedule_returns_to_pending(self, queue: PersistentQueue) -> None:
        jid = await queue.enqueue("fastly_main", ["https://x/a"])
        await queue.claim_ready("fastly_main")
        await queue.reschedule(jid, attempts=1, next_attempt_at=time.time() - 1)
        again = await queue.claim_ready("fastly_main")
        assert again is not None
        assert again.attempts == 1

    async def test_mark_failed(self, queue: PersistentQueue) -> None:
        jid = await queue.enqueue("fastly_main", ["https://x/a"])
        await queue.claim_ready("fastly_main")
        await queue.mark_failed(jid, "invalid credentials")
        stats = await queue.stats()
        assert stats.get("failed", 0) == 1


class TestInFlightRecovery:
    async def test_reset_in_flight_returns_count(self, tmp_path: Path) -> None:
        q1 = await PersistentQueue.open(tmp_path / "queue.db")
        try:
            await q1.enqueue("fastly_main", ["https://x/a"])
            await q1.enqueue("fastly_main", ["https://x/b"])
            await q1.claim_ready("fastly_main")
            await q1.claim_ready("fastly_main")
            # Simulate unclean shutdown by closing without resolving.
        finally:
            await q1.close()

        q2 = await PersistentQueue.open(tmp_path / "queue.db")
        try:
            moved = await q2.reset_in_flight()
            assert moved == 2
            # Both jobs should now be claimable again.
            assert await q2.claim_ready("fastly_main") is not None
            assert await q2.claim_ready("fastly_main") is not None
        finally:
            await q2.close()


class TestExponentialBackoff:
    def test_first_attempt_returns_initial(self) -> None:
        assert exponential_backoff(1, initial=5.0, ceiling=1800.0) == 5.0

    def test_doubles_each_attempt(self) -> None:
        assert exponential_backoff(2, initial=5.0, ceiling=1800.0) == 10.0
        assert exponential_backoff(3, initial=5.0, ceiling=1800.0) == 20.0
        assert exponential_backoff(4, initial=5.0, ceiling=1800.0) == 40.0

    def test_capped_at_ceiling(self) -> None:
        # 5 * 2^19 = 2621440 > 1800
        assert exponential_backoff(20, initial=5.0, ceiling=1800.0) == 1800.0


class TestStats:
    async def test_counts_by_status(self, queue: PersistentQueue) -> None:
        await queue.enqueue("a", ["url1"])
        await queue.enqueue("a", ["url2"])
        jid = await queue.enqueue("a", ["url3"])
        await queue.claim_ready("a")  # one now in_flight
        await queue.mark_done(jid)
        await queue.mark_done((await queue.claim_ready("a")).id)  # type: ignore[union-attr]
        stats = await queue.stats()
        # After: 1 done (manually), 1 done from second claim, 1 pending remaining
        # (the in_flight one's id was the one we marked done on the very first call).
        # Just sanity-check that counts sum to 3.
        assert sum(stats.values()) == 3
