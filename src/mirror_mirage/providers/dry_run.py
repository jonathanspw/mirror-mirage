"""Dry-run provider.

Logs each batch it would purge at INFO level and returns success without
calling any real CDN API. Activated by passing ``--dry-run`` to the CLI;
in that mode, every entry in the ``cdns:`` block of ``config.yaml`` is
wrapped with this provider regardless of its declared type.

Useful for verifying event coverage and config correctness on a staging
server before pointing the daemon at real CDN credentials.
"""

from __future__ import annotations

import logging

from mirror_mirage.providers.base import PurgeResult

_log = logging.getLogger("mirror_mirage.dry_run")


class DryRunProvider:
    """A provider that logs intended purges and always reports success."""

    def __init__(self, *, cdn_name: str, wrapped_type: str) -> None:
        """Create a dry-run provider tagged with the CDN it stands in for.

        Parameters
        ----------
        cdn_name
            The CDN binding name from config; appears in every log line.
        wrapped_type
            The real provider type that would otherwise have been used
            (``"fastly"``, ``"cloudfront"``, ``"gcp_cdn"``). Logged so an
            operator can verify the dry-run is exercising the intended
            backend path.
        """
        self._cdn_name = cdn_name
        self._wrapped_type = wrapped_type

    async def purge(self, urls: list[str]) -> PurgeResult:
        """Log ``(cdn_name, wrapped_type, len(urls), urls)`` and return success.

        Every URL is logged so the operator can grep the journal to
        confirm event coverage. The result is always
        ``PurgeResult(ok=True, retryable=False, message="dry-run")``.
        """
        _log.info(
            "event=dry_run_purge cdn=%s wrapped_type=%s batch_size=%d",
            self._cdn_name,
            self._wrapped_type,
            len(urls),
        )
        for url in urls:
            _log.info(
                "event=dry_run_url cdn=%s wrapped_type=%s url=%s",
                self._cdn_name,
                self._wrapped_type,
                url,
            )
        return PurgeResult(ok=True, retryable=False, message="dry-run")
