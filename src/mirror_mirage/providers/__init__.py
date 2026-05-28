"""CDN provider implementations.

Each provider implements :class:`~mirror_mirage.providers.base.CdnProvider`:
an async ``purge(urls)`` method returning a
:class:`~mirror_mirage.providers.base.PurgeResult`.

:func:`build_providers` constructs the right provider instance for each
CDN named in a :class:`~mirror_mirage.config.Config`. When called with
``dry_run=True``, every CDN is wrapped with
:class:`~mirror_mirage.providers.dry_run.DryRunProvider` instead — see that
module for details.
"""

from __future__ import annotations

from pathlib import Path

from mirror_mirage.config import CloudFrontCdn, Config, FastlyCdn, GcpCdnConfig
from mirror_mirage.providers.base import CdnProvider, PurgeResult
from mirror_mirage.providers.cloudfront import CloudFrontProvider
from mirror_mirage.providers.dry_run import DryRunProvider
from mirror_mirage.providers.fastly import FastlyProvider
from mirror_mirage.providers.gcp import GcpCdnProvider

__all__ = ["CdnProvider", "DryRunProvider", "PurgeResult", "build_providers"]


def build_providers(cfg: Config, *, dry_run: bool = False) -> dict[str, CdnProvider]:
    """Instantiate one provider per CDN defined in ``cfg``.

    When ``dry_run`` is True, every CDN gets a :class:`DryRunProvider`
    that logs intended purges and always returns success — no real CDN
    API is contacted. This mode is intended for verifying event coverage
    on a staging server before enabling real flushes.
    """
    providers: dict[str, CdnProvider] = {}
    for name, cdn in cfg.cdns.items():
        if dry_run:
            providers[name] = DryRunProvider(cdn_name=name, wrapped_type=cdn.type)
            continue
        creds = cfg.credentials.get(name, {})
        if isinstance(cdn, FastlyCdn):
            providers[name] = FastlyProvider(
                service_id=cdn.service_id,
                api_token=creds["api_token"],
                purge_all_service_domains=cdn.purge_all_service_domains,
            )
        elif isinstance(cdn, CloudFrontCdn):
            providers[name] = CloudFrontProvider(
                distribution_id=cdn.distribution_id,
                region=cdn.region,
                access_key_id=creds.get("access_key_id"),
                secret_access_key=creds.get("secret_access_key"),
            )
        elif isinstance(cdn, GcpCdnConfig):
            providers[name] = GcpCdnProvider(
                project=cdn.project,
                url_map=cdn.url_map,
                service_account_file=Path(creds["service_account_file"]),
                quota_per_minute=cdn.quota_per_minute,
            )
        else:  # pragma: no cover
            raise ValueError(f"unknown CDN type: {type(cdn).__name__}")
    return providers
