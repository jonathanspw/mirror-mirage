"""Common provider protocol and result type."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PurgeResult:
    """Outcome of a single :meth:`CdnProvider.purge` call.

    Attributes
    ----------
    ok
        True if every URL in the batch was accepted by the CDN.
    retryable
        Only meaningful when ``ok`` is False. When True, the queue worker
        will reschedule the job with exponential backoff. When False, the
        job is moved to ``failed`` status and requires operator action.
    message
        Human-readable description of the outcome — surfaced in logs and
        in the ``failed`` job's recorded message.
    """

    ok: bool
    retryable: bool
    message: str = ""


class CdnProvider(Protocol):
    """Async CDN purge client."""

    async def purge(self, urls: list[str]) -> PurgeResult:
        """Purge the given list of URLs from the CDN.

        Implementations may chunk ``urls`` internally if the CDN imposes
        per-call size limits (Fastly's 256-URL cap, for example). They
        must classify errors as retryable vs terminal per the rules in
        ``docs/cdn-providers.md``.
        """
        ...
