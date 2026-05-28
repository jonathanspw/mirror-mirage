"""Top-level orchestrator.

Wires together :class:`~mirror_mirage.watcher.InotifyWatcher`,
:class:`~mirror_mirage.filter.EventFilter`, :class:`~mirror_mirage.mapper.Mapper`,
:class:`~mirror_mirage.batcher.Batcher`, :class:`~mirror_mirage.queue.PersistentQueue`,
and one provider worker per CDN. Handles graceful shutdown on SIGTERM /
SIGINT and emits ``sd_notify("READY=1")`` once the watcher is online.
"""

from __future__ import annotations

import asyncio
import logging
import time

from mirror_mirage.batcher import Batcher
from mirror_mirage.config import Config, RetryConfig
from mirror_mirage.filter import EventFilter
from mirror_mirage.mapper import Mapper
from mirror_mirage.providers import build_providers
from mirror_mirage.providers.base import CdnProvider, PurgeResult
from mirror_mirage.queue import PersistentQueue, exponential_backoff
from mirror_mirage.watcher import InotifyWatcher

_log = logging.getLogger("mirror_mirage.daemon")

_WORKER_IDLE_POLL_SECONDS = 0.2
_URLS_LOG_LIMIT = 5


def _urls_field(urls: list[str], max_show: int = _URLS_LOG_LIMIT) -> str:
    """Render a URL list for a one-line log field.

    Small batches show every URL; larger batches show the first ``max_show``
    plus a ``+N more`` suffix so a single log line stays grep-friendly even
    for big rsync pushes. The full list always remains in the SQLite queue
    (``urls_json`` on the corresponding row in ``/var/lib/mirror-mirage/queue.db``).
    """
    if len(urls) <= max_show:
        return ",".join(urls)
    return ",".join(urls[:max_show]) + f",+{len(urls) - max_show}-more"


async def _queue_worker(
    queue: PersistentQueue,
    cdn_name: str,
    provider: CdnProvider,
    retry: RetryConfig,
) -> None:
    """Drain the persistent queue for one CDN."""
    while True:
        job = await queue.claim_ready(cdn_name)
        if job is None:
            await asyncio.sleep(_WORKER_IDLE_POLL_SECONDS)
            continue
        attempt = job.attempts + 1
        _log.info(
            "event=purge_attempt cdn=%s job_id=%d batch_size=%d attempt=%d",
            cdn_name,
            job.id,
            len(job.urls),
            attempt,
        )
        start = time.monotonic()
        try:
            result = await provider.purge(job.urls)
        except Exception as e:
            result = PurgeResult(ok=False, retryable=True, message=f"provider raised: {e}")
        latency_ms = (time.monotonic() - start) * 1000.0

        if result.ok:
            _log.info(
                "event=purge_ok cdn=%s job_id=%d batch_size=%d latency_ms=%.0f urls=%s",
                cdn_name,
                job.id,
                len(job.urls),
                latency_ms,
                _urls_field(job.urls),
            )
            await queue.mark_done(job.id)
            continue

        if result.retryable and attempt < retry.max_attempts:
            backoff = exponential_backoff(
                attempt,
                initial=retry.initial_backoff_seconds,
                ceiling=retry.max_backoff_seconds,
            )
            _log.warning(
                "event=purge_retry cdn=%s job_id=%d attempt=%d backoff_s=%.2f error=%r urls=%s",
                cdn_name,
                job.id,
                attempt,
                backoff,
                result.message,
                _urls_field(job.urls),
            )
            await queue.reschedule(
                job.id,
                attempts=attempt,
                next_attempt_at=time.time() + backoff,
            )
        else:
            _log.error(
                "event=purge_failed cdn=%s job_id=%d attempts=%d error=%r urls=%s",
                cdn_name,
                job.id,
                attempt,
                result.message,
                _urls_field(job.urls),
            )
            await queue.mark_failed(job.id, result.message)


async def _pipeline(
    watcher: InotifyWatcher,
    filter_: EventFilter,
    mapper: Mapper,
    batcher: Batcher,
) -> None:
    """Pump inotify events through filter → mapper → batcher."""
    async for mask_name, abs_path, _watch_root in watcher:
        event = filter_.normalize(mask_name, abs_path)
        if event is None:
            continue
        for target in mapper.map(event):
            await batcher.submit(target)


async def run(cfg: Config, *, dry_run: bool = False) -> None:
    """Run the daemon until canceled.

    Composes the pipeline described in :mod:`mirror_mirage.daemon`'s module
    docstring. Returns cleanly after draining in-flight buffers when the
    caller cancels the surrounding task.
    """
    queue = await PersistentQueue.open(cfg.daemon.queue_db)
    try:
        recovered = await queue.reset_in_flight()
        if recovered:
            _log.info("event=in_flight_recovered count=%d", recovered)

        providers = build_providers(cfg, dry_run=dry_run)
        batcher = Batcher(queue, cfg.daemon)
        mapper = Mapper(cfg.watches)
        filter_ = EventFilter(cfg.watches)

        async with InotifyWatcher(cfg.watches) as watcher:
            _log.info(
                "event=ready dry_run=%s watches=%d cdns=%d",
                dry_run,
                len(cfg.watches),
                len(providers),
            )
            async with asyncio.TaskGroup() as tg:
                for cdn_name, provider in providers.items():
                    tg.create_task(
                        _queue_worker(queue, cdn_name, provider, cfg.daemon.retry),
                        name=f"mirror-mirage-worker-{cdn_name}",
                    )
                tg.create_task(batcher.run(), name="mirror-mirage-batcher")
                tg.create_task(
                    _pipeline(watcher, filter_, mapper, batcher),
                    name="mirror-mirage-pipeline",
                )
    finally:
        await queue.close()
