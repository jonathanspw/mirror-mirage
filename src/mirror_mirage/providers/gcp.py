"""GCP Cloud CDN invalidation provider.

See ``docs/cdn-providers.md`` for API details and error classification.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from mirror_mirage.providers.base import PurgeResult

GCP_DEFAULT_CONCURRENCY = 10
GCP_DEFAULT_QUOTA_PER_MINUTE = 500
GCP_COMPUTE_BASE = "https://compute.googleapis.com/compute/v1"

_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
_log = logging.getLogger("mirror_mirage.gcp")


class _SlidingWindowLimiter:
    """Admit up to ``capacity`` calls per rolling ``window_seconds``.

    When the window is full, ``acquire()`` waits until the oldest call
    in the window has aged out, then re-checks. Multiple waiters wake
    independently; the lock serializes admission so the count is
    consistent.
    """

    def __init__(self, capacity: int, window_seconds: float) -> None:
        self.capacity = capacity
        self.window_seconds = window_seconds
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = asyncio.get_event_loop().time()
                cutoff = now - self.window_seconds
                while self._calls and self._calls[0] <= cutoff:
                    self._calls.popleft()
                if len(self._calls) < self.capacity:
                    self._calls.append(now)
                    return
                oldest = self._calls[0]
                wait = max(0.0, (oldest + self.window_seconds) - now)
            await asyncio.sleep(wait)


class GcpCdnProvider:
    """Purges URLs via ``compute.urlMaps.invalidateCache``.

    The provider enforces a client-side rate limit (paths per minute) so
    that bursts do not exceed the project's per-minute invalidation
    quota. The default cap matches GCP's default quota of 500
    paths/minute; tune ``quota_per_minute`` upward if your project has
    an elevated quota.

    The limiter is a sliding-window counter (any call that would push
    the count above the cap awaits until the oldest call in the window
    expires). Because invalidations from this provider all consume from
    the same project-wide quota, the limiter is per-instance: a single
    :class:`GcpCdnProvider` instance corresponds to one URL map, and
    multiple instances pointing at the same project would each need
    their own quota share.
    """

    def __init__(
        self,
        *,
        project: str,
        url_map: str,
        service_account_file: Path,
        client: httpx.AsyncClient | None = None,
        concurrency: int = GCP_DEFAULT_CONCURRENCY,
        quota_per_minute: int = GCP_DEFAULT_QUOTA_PER_MINUTE,
    ) -> None:
        """Create a provider for a single URL map in a single project.

        GCP's API accepts only one path per invalidation call, so a batch
        of N URLs dispatches N concurrent API calls, bounded by
        ``concurrency`` *and* by ``quota_per_minute`` (whichever is
        stricter at any moment).
        """
        self._project = project
        self._url_map = url_map
        self._sa_file = service_account_file
        self._client = client
        self._own_client = client is None
        self._quota_per_minute = quota_per_minute
        # Exposed for test injection; production uses 60 seconds.
        self._quota_window_seconds: float = 60.0
        self._limiter: _SlidingWindowLimiter | None = None
        self._sem = asyncio.Semaphore(concurrency)
        self._creds: service_account.Credentials | None = None
        self._endpoint = (
            f"{GCP_COMPUTE_BASE}/projects/{project}/global/urlMaps/{url_map}/invalidateCache"
        )

    def _get_limiter(self) -> _SlidingWindowLimiter:
        if self._limiter is None:
            self._limiter = _SlidingWindowLimiter(
                self._quota_per_minute, self._quota_window_seconds
            )
        return self._limiter

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    async def _get_token(self) -> str:
        if self._creds is None:
            # google-auth ships partial stubs but this classmethod is
            # untyped within them — narrow the ignore to that one rule.
            self._creds = service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
                str(self._sa_file), scopes=_SCOPES
            )
        creds = self._creds
        if not creds.token or creds.expired:
            await asyncio.to_thread(creds.refresh, Request())
        token = creds.token
        # isinstance narrows Any → str for mypy under ignore_missing_imports.
        if not isinstance(token, str):
            raise RuntimeError("GCP credential refresh produced no string token")
        return token

    async def _purge_one(self, url: str) -> PurgeResult:
        await self._get_limiter().acquire()
        async with self._sem:
            parsed = urlparse(url)
            body = {
                "path": parsed.path or "/",
                "host": parsed.hostname or "",
            }
            try:
                token = await self._get_token()
            except Exception as e:
                return PurgeResult(ok=False, retryable=False, message=f"auth: {e}")
            try:
                client = await self._get_client()
                resp = await client.post(
                    self._endpoint,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
            except httpx.HTTPError as e:
                return PurgeResult(ok=False, retryable=True, message=f"transport: {e}")

            if 200 <= resp.status_code < 300:
                return PurgeResult(ok=True, retryable=False, message="ok")
            if resp.status_code in _RETRYABLE_STATUS:
                return PurgeResult(
                    ok=False,
                    retryable=True,
                    message=f"HTTP {resp.status_code}",
                )
            return PurgeResult(
                ok=False,
                retryable=False,
                message=f"HTTP {resp.status_code}",
            )

    async def purge(self, urls: list[str]) -> PurgeResult:
        """Issue one ``invalidateCache`` call per URL with bounded concurrency.

        Each URL contributes a ``{"path": "...", "host": "..."}`` body.
        Calls are paced by the per-minute quota: when the quota is full,
        :meth:`purge` awaits the oldest in-window call's expiry rather
        than letting GCP reject the request. This is cleaner than
        relying on ``429`` responses because the daemon never has to
        retry-after on the hot path.

        If any call returns a terminal error, the overall result is
        terminal; if any call returns a retryable error and none are
        terminal, the overall result is retryable.
        """
        if not urls:
            return PurgeResult(ok=True, retryable=False, message="empty")

        results = await asyncio.gather(*(self._purge_one(u) for u in urls), return_exceptions=False)
        terminal = [r for r in results if not r.ok and not r.retryable]
        retryable = [r for r in results if not r.ok and r.retryable]
        if terminal:
            return PurgeResult(
                ok=False,
                retryable=False,
                message="; ".join(r.message for r in terminal),
            )
        if retryable:
            return PurgeResult(
                ok=False,
                retryable=True,
                message="; ".join(r.message for r in retryable),
            )
        return PurgeResult(ok=True, retryable=False, message="ok")
