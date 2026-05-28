"""SQLite-backed persistent job queue with retry.

A single ``jobs`` table holds one row per coalesced batch:

==================  =========================================================
Column              Meaning
==================  =========================================================
``id``              Auto-incrementing primary key.
``cdn_name``        Name of the bound CDN (matches ``Config.cdns`` key).
``urls_json``       JSON-encoded list of public URLs in the batch.
``attempts``        Number of times the provider has been called for this job.
``next_attempt_at`` Unix timestamp of the earliest next attempt.
``created_at``      Unix timestamp the job was enqueued.
``status``          One of ``pending``, ``in_flight``, ``done``, ``failed``.
==================  =========================================================

WAL mode is enabled for crash safety. On startup, :meth:`reset_in_flight`
moves any leftover ``in_flight`` rows back to ``pending`` — purges are
idempotent on every supported CDN, so re-running an interrupted job is
safe.

The queue is purely a persistence layer; retry/backoff *policy* lives in
:func:`exponential_backoff` and the worker that drives the queue (in
:mod:`mirror_mirage.daemon`).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cdn_name        TEXT    NOT NULL,
    urls_json       TEXT    NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL    NOT NULL,
    created_at      REAL    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'pending',
    message         TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_ready
    ON jobs (cdn_name, status, next_attempt_at);
"""


@dataclass(frozen=True)
class Job:
    """One row from the ``jobs`` table."""

    id: int
    cdn_name: str
    urls: list[str]
    attempts: int
    next_attempt_at: float
    created_at: float
    status: str


class PersistentQueue:
    """Async SQLite-backed durable queue."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    @classmethod
    async def open(cls, db_path: Path) -> PersistentQueue:
        """Open (or create) the SQLite database at ``db_path``.

        Enables WAL mode and creates the ``jobs`` table if it does not
        exist. Does *not* automatically reset ``in_flight`` rows; the
        daemon does that explicitly via :meth:`reset_in_flight` after
        opening.
        """
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(db_path))
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA synchronous = NORMAL")
        await conn.executescript(_SCHEMA)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        """Close the underlying SQLite connection."""
        await self._conn.close()

    async def enqueue(self, cdn_name: str, urls: list[str]) -> int:
        """Insert a new ``pending`` job. Returns the new row's ``id``."""
        now = time.time()
        cursor = await self._conn.execute(
            "INSERT INTO jobs (cdn_name, urls_json, next_attempt_at, created_at, status) "
            "VALUES (?, ?, ?, ?, 'pending')",
            (cdn_name, json.dumps(list(urls)), now, now),
        )
        await self._conn.commit()
        job_id = cursor.lastrowid
        assert job_id is not None
        return job_id

    async def claim_ready(self, cdn_name: str) -> Job | None:
        """Atomically move one ready ``pending`` job for ``cdn_name`` to ``in_flight``.

        "Ready" means ``next_attempt_at <= now()``. Returns the claimed
        :class:`Job`, or ``None`` if no job is ready.

        Implemented as ``SELECT`` followed by a conditional ``UPDATE
        ... WHERE status = 'pending'`` so the claim works on SQLite
        builds older than 3.35 (which is when ``RETURNING`` landed). The
        ``status = 'pending'`` predicate in the UPDATE provides the
        atomicity guarantee: if two callers race on the SELECT, only one
        UPDATE will mark the row, and the loser sees ``rowcount == 0``
        and returns ``None`` as if nothing was ready.
        """
        now = time.time()
        cursor = await self._conn.execute(
            """
            SELECT id, cdn_name, urls_json, attempts,
                   next_attempt_at, created_at
              FROM jobs
             WHERE cdn_name = ?
               AND status = 'pending'
               AND next_attempt_at <= ?
             ORDER BY id ASC
             LIMIT 1
            """,
            (cdn_name, now),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        update_cursor = await self._conn.execute(
            "UPDATE jobs SET status = 'in_flight' WHERE id = ? AND status = 'pending'",
            (row[0],),
        )
        await self._conn.commit()
        if update_cursor.rowcount == 0:
            # Lost a race with another claimer.
            return None
        return Job(
            id=row[0],
            cdn_name=row[1],
            urls=json.loads(row[2]),
            attempts=row[3],
            next_attempt_at=row[4],
            created_at=row[5],
            status="in_flight",
        )

    async def mark_done(self, job_id: int) -> None:
        """Set ``status = 'done'`` for ``job_id``."""
        await self._conn.execute("UPDATE jobs SET status = 'done' WHERE id = ?", (job_id,))
        await self._conn.commit()

    async def reschedule(
        self,
        job_id: int,
        *,
        attempts: int,
        next_attempt_at: float,
    ) -> None:
        """Return a job to ``pending`` with updated ``attempts`` and ``next_attempt_at``."""
        await self._conn.execute(
            "UPDATE jobs "
            "   SET status = 'pending', attempts = ?, next_attempt_at = ? "
            " WHERE id = ?",
            (attempts, next_attempt_at, job_id),
        )
        await self._conn.commit()

    async def mark_failed(self, job_id: int, message: str) -> None:
        """Set ``status = 'failed'`` and record the failure message."""
        await self._conn.execute(
            "UPDATE jobs SET status = 'failed', message = ? WHERE id = ?",
            (message, job_id),
        )
        await self._conn.commit()

    async def reset_in_flight(self) -> int:
        """Move any ``in_flight`` rows back to ``pending``. Returns count moved."""
        cursor = await self._conn.execute(
            "UPDATE jobs SET status = 'pending' WHERE status = 'in_flight'"
        )
        await self._conn.commit()
        return cursor.rowcount or 0

    async def stats(self) -> dict[str, int]:
        """Return ``{status: count}`` across all jobs."""
        cursor = await self._conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status")
        rows = await cursor.fetchall()
        return {row[0]: row[1] for row in rows}


def exponential_backoff(attempts: int, *, initial: float, ceiling: float) -> float:
    """Compute the next backoff in seconds for ``attempts`` (1-indexed).

    Formula: ``min(initial * 2 ** (attempts - 1), ceiling)``.

    ``attempts`` must be ``>= 1``; the result for ``attempts == 1`` is
    ``initial``, doubling each call.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    return float(min(initial * (2 ** (attempts - 1)), ceiling))
