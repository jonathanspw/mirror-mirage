"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from mirror_mirage.config import (
    Binding,
    CloudFrontCdn,
    Config,
    DaemonConfig,
    FastlyCdn,
    GcpCdnConfig,
    RetryConfig,
    WatchConfig,
)


@pytest.fixture
def mirror_tree(tmp_path: Path) -> Path:
    """A minimal mirror tree on disk used by filter/watcher/mapper tests."""
    root = tmp_path / "srv" / "mirror" / "almalinux"
    (root / "9" / "os" / "x86_64" / "repodata").mkdir(parents=True)
    (root / "9" / "os" / "x86_64" / "Packages").mkdir(parents=True)
    return root


@pytest.fixture
def fastly_cdn() -> FastlyCdn:
    return FastlyCdn(type="fastly", service_id="SUtestSvcId")


@pytest.fixture
def cloudfront_cdn() -> CloudFrontCdn:
    return CloudFrontCdn(type="cloudfront", distribution_id="E2QWRUHEXAMPLE")


@pytest.fixture
def gcp_cdn() -> GcpCdnConfig:
    return GcpCdnConfig(type="gcp_cdn", project="alma-prod", url_map="alma-prod-lb")


@pytest.fixture
def watch_factory(mirror_tree: Path):
    """Factory producing a WatchConfig pointing at the temp mirror tree."""

    def _make(
        *,
        bindings: list[Binding] | None = None,
        ignore: list[str] | None = None,
        path: Path | None = None,
    ) -> WatchConfig:
        return WatchConfig(
            path=path or mirror_tree,
            ignore=ignore if ignore is not None else ["**/.~tmp~/**"],
            bindings=bindings
            or [
                Binding(cdn="fastly_main", url_prefix="https://repo.almalinux.org/almalinux/"),
            ],
        )

    return _make


@pytest.fixture
def daemon_cfg(tmp_path: Path) -> DaemonConfig:
    """Fast-tuned daemon config so tests don't wait on real timers."""
    return DaemonConfig(
        batch_quiet_window_seconds=0.05,
        batch_max_size=10,
        batch_max_age_seconds=0.5,
        queue_db=tmp_path / "queue.db",
        retry=RetryConfig(max_attempts=3, initial_backoff_seconds=0.01, max_backoff_seconds=1.0),
    )


@pytest.fixture
def basic_config(
    mirror_tree: Path,
    fastly_cdn: FastlyCdn,
    cloudfront_cdn: CloudFrontCdn,
    daemon_cfg: DaemonConfig,
) -> Config:
    """Two-CDN config with a single watch, ready to drive the daemon."""
    return Config(
        cdns={"fastly_main": fastly_cdn, "cloudfront_us": cloudfront_cdn},
        watches=[
            WatchConfig(
                path=mirror_tree,
                ignore=["**/.~tmp~/**"],
                bindings=[
                    Binding(cdn="fastly_main", url_prefix="https://repo.almalinux.org/almalinux/"),
                    Binding(
                        cdn="cloudfront_us",
                        url_prefix="https://cdn-us.almalinux.org/almalinux/",
                    ),
                ],
            ),
        ],
        daemon=daemon_cfg,
        credentials={
            "fastly_main": {"api_token": "fxxx-test"},
            "cloudfront_us": {
                "access_key_id": "AKIATEST",
                "secret_access_key": "secret-test",
            },
        },
    )
