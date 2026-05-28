"""End-to-end integration test.

Composes the real watcher → filter → mapper → batcher → queue pipeline
with mock CDN providers, runs an rsync-shaped push, and asserts that
each bound CDN sees exactly one batched purge containing the post-rename
URLs.

Skipped on non-Linux hosts (inotify is Linux-only).
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from mirror_mirage.config import Config
from mirror_mirage.daemon import run as daemon_run
from mirror_mirage.providers.base import PurgeResult

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="inotify is Linux-only")


@dataclass
class RecordingProvider:
    """A provider that records every batch and always succeeds."""

    name: str
    batches: list[list[str]] = field(default_factory=list)

    async def purge(self, urls: list[str]) -> PurgeResult:
        self.batches.append(sorted(urls))
        return PurgeResult(ok=True, retryable=False, message="recorded")


def _simulate_rsync_push(mirror_tree: Path, files: list[str]) -> list[Path]:
    """Stage every file in .~tmp~/, then rename them into place."""
    rendered: list[Path] = []
    for rel in files:
        target = mirror_tree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        staging_dir = target.parent / ".~tmp~"
        staging_dir.mkdir(exist_ok=True)
        staging_file = staging_dir / target.name
        staging_file.write_bytes(f"data:{rel}".encode())
        os.rename(staging_file, target)
        rendered.append(target)
    return rendered


class TestEndToEnd:
    async def test_rsync_push_produces_one_batch_per_cdn(
        self, basic_config: Config, mirror_tree: Path, monkeypatch
    ) -> None:
        # Install recording providers in place of the real ones.
        recorders = {
            "fastly_main": RecordingProvider("fastly_main"),
            "cloudfront_us": RecordingProvider("cloudfront_us"),
        }

        def fake_build(cfg, *, dry_run=False):
            return dict(recorders)

        monkeypatch.setattr("mirror_mirage.daemon.build_providers", fake_build, raising=False)
        # Some implementations may import build_providers at module load.
        monkeypatch.setattr("mirror_mirage.providers.build_providers", fake_build, raising=False)

        task = asyncio.create_task(daemon_run(basic_config))
        try:
            # Give the watcher time to register watches.
            await asyncio.sleep(0.2)

            files = [
                "9/os/x86_64/repodata/repomd.xml",
                "9/os/x86_64/repodata/primary.xml.gz",
                "9/os/x86_64/Packages/foo-1.0-1.x86_64.rpm",
            ]
            _simulate_rsync_push(mirror_tree, files)

            # Wait for the quiet window to elapse and the queue worker
            # to drain at least one batch per CDN.
            for _ in range(40):  # up to 2 s
                if all(r.batches for r in recorders.values()):
                    break
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        # Each CDN should have received exactly one batch of three URLs.
        for name, rec in recorders.items():
            assert len(rec.batches) == 1, (
                f"{name}: expected 1 batch, got {len(rec.batches)} ({rec.batches!r})"
            )
            batch = rec.batches[0]
            assert len(batch) == 3
            # All three of the rsync'd files should be present.
            for rel in [
                "9/os/x86_64/repodata/repomd.xml",
                "9/os/x86_64/repodata/primary.xml.gz",
                "9/os/x86_64/Packages/foo-1.0-1.x86_64.rpm",
            ]:
                assert any(rel in url for url in batch), (
                    f"{name}: {rel} missing from batch {batch!r}"
                )

        # And the URLs in each CDN's batch must use that CDN's prefix.
        assert all(
            url.startswith("https://repo.almalinux.org/almalinux/")
            for url in recorders["fastly_main"].batches[0]
        )
        assert all(
            url.startswith("https://cdn-us.almalinux.org/almalinux/")
            for url in recorders["cloudfront_us"].batches[0]
        )

    async def test_staging_writes_do_not_purge(
        self, basic_config: Config, mirror_tree: Path, monkeypatch
    ) -> None:
        """Writing into .~tmp~ without renaming must not produce purges."""
        recorders = {
            "fastly_main": RecordingProvider("fastly_main"),
            "cloudfront_us": RecordingProvider("cloudfront_us"),
        }

        def fake_build(cfg, *, dry_run=False):
            return dict(recorders)

        monkeypatch.setattr("mirror_mirage.daemon.build_providers", fake_build, raising=False)
        monkeypatch.setattr("mirror_mirage.providers.build_providers", fake_build, raising=False)

        task = asyncio.create_task(daemon_run(basic_config))
        try:
            await asyncio.sleep(0.2)
            # Write into staging but never rename out.
            staging = mirror_tree / "9/os/x86_64/repodata/.~tmp~"
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "repomd.xml").write_bytes(b"<repomd/>")
            # Wait long enough for any spurious flush to occur.
            await asyncio.sleep(0.5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        for name, rec in recorders.items():
            assert rec.batches == [], (
                f"{name}: unexpected batches from staging-only writes: {rec.batches!r}"
            )
