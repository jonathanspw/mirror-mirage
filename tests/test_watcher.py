"""Tests for :mod:`mirror_mirage.watcher`.

These tests use real inotify against a temp directory. They simulate
rsync's ``--delay-updates`` flow (write into ``.~tmp~/``, rename into
place) and assert that:

* Staging events are not surfaced.
* The post-rename ``IN_MOVED_TO`` event reaches the consumer.
* New subdirectories are auto-watched and their contents picked up.

Skipped on non-Linux hosts (inotify is Linux-only).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from mirror_mirage.config import Binding, WatchConfig
from mirror_mirage.watcher import (
    InotifyLimitError,
    InotifyWatcher,
    _count_watchable_dirs,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="inotify is Linux-only")


async def _drain_for(watcher: InotifyWatcher, seconds: float) -> list[tuple[str, Path, Path]]:
    """Collect events for ``seconds`` then stop."""
    events: list[tuple[str, Path, Path]] = []

    async def collect() -> None:
        async for ev in watcher:
            events.append(ev)

    task = asyncio.create_task(collect())
    try:
        await asyncio.sleep(seconds)
    finally:
        task.cancel()
        with pytest.raises((asyncio.CancelledError, BaseException)):
            await task
    return events


@pytest.fixture
def watch(mirror_tree: Path) -> WatchConfig:
    return WatchConfig(
        path=mirror_tree,
        ignore=["**/.~tmp~/**"],
        bindings=[Binding(cdn="fastly_main", url_prefix="https://x.example/")],
    )


def _simulate_rsync(mirror_tree: Path, rel: str, content: bytes = b"data") -> Path:
    """Mimic rsync --delay-updates: write into .~tmp~/, then rename into place."""
    target = mirror_tree / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    staging_dir = target.parent / ".~tmp~"
    staging_dir.mkdir(exist_ok=True)
    staging_file = staging_dir / target.name
    staging_file.write_bytes(content)
    os.rename(staging_file, target)
    return target


class TestBasicWatching:
    async def test_yields_close_write(self, mirror_tree: Path, watch: WatchConfig) -> None:
        async with InotifyWatcher([watch]) as w:
            collector = asyncio.create_task(_drain_for(w, 0.5))
            await asyncio.sleep(0.1)
            (mirror_tree / "9/os/x86_64/Packages/in-place.rpm").write_bytes(b"x")
            events = await collector
        masks = {e[0] for e in events}
        # Either CLOSE_WRITE or CREATE+CLOSE_WRITE — at minimum CLOSE_WRITE must appear.
        assert "IN_CLOSE_WRITE" in masks


class TestRsyncStaging:
    async def test_staging_events_filtered_via_ignore(
        self, mirror_tree: Path, watch: WatchConfig
    ) -> None:
        """rsync .~tmp~ contents must not produce surfaced events.

        The watcher must not even add a watch on ``.~tmp~`` directories,
        so the only events that reach the consumer are the post-rename
        ones on the live tree.
        """
        async with InotifyWatcher([watch]) as w:
            collector = asyncio.create_task(_drain_for(w, 0.6))
            await asyncio.sleep(0.1)
            target = _simulate_rsync(mirror_tree, "9/os/x86_64/repodata/repomd.xml", b"<repomd/>")
            events = await collector
        # The .~tmp~ path must not appear in any event.
        for mask, path, _ in events:
            assert ".~tmp~" not in str(path), f"unexpected staging event {mask} on {path}"
        # At least one event for the live target must appear.
        assert any(p == target for _, p, _ in events)


class TestRecursion:
    async def test_new_subdir_is_auto_watched(self, mirror_tree: Path, watch: WatchConfig) -> None:
        async with InotifyWatcher([watch]) as w:
            collector = asyncio.create_task(_drain_for(w, 0.8))
            await asyncio.sleep(0.1)
            new_dir = mirror_tree / "9/os/x86_64/new-arch"
            new_dir.mkdir()
            # Without the watcher auto-watching new_dir, the file write below
            # would emit no event.
            await asyncio.sleep(0.1)
            f = new_dir / "fresh.rpm"
            f.write_bytes(b"y")
            events = await collector
        # An event for fresh.rpm should be in the list.
        assert any(p == f for _, p, _ in events), (
            f"new subdir contents not surfaced; events: {events!r}"
        )

    async def test_synthetic_events_for_files_created_during_watch_race(
        self, mirror_tree: Path, watch: WatchConfig
    ) -> None:
        """Files that exist by the time the watch is added should still be flushed.

        Specifically: if a subdirectory is created and immediately populated
        before the watcher registers a watch on it, the watcher must scan
        and emit synthetic events for those pre-existing files.
        """
        async with InotifyWatcher([watch]) as w:
            collector = asyncio.create_task(_drain_for(w, 0.8))
            await asyncio.sleep(0.1)
            new_dir = mirror_tree / "9/os/x86_64/race-dir"
            new_dir.mkdir()
            # Race window: populate immediately, before the daemon has had
            # a chance to register the watch.
            (new_dir / "raced.rpm").write_bytes(b"z")
            events = await collector
        paths = {p for _, p, _ in events}
        assert (new_dir / "raced.rpm") in paths


class TestDirCounting:
    """`_count_watchable_dirs` must match what the watcher would add."""

    def test_counts_root_and_descendants(self, mirror_tree: Path) -> None:
        # Fixture creates: root + 9 + 9/os + 9/os/x86_64
        # + 9/os/x86_64/repodata + 9/os/x86_64/Packages = 6.
        assert _count_watchable_dirs(mirror_tree, ignore=[]) == 6

    def test_ignored_subtree_not_counted(self, mirror_tree: Path) -> None:
        # Drop in a staging dir that the default ignore pattern excludes.
        (mirror_tree / "9/os/x86_64/repodata/.~tmp~").mkdir()
        (mirror_tree / "9/os/x86_64/repodata/.~tmp~/sub1").mkdir()
        (mirror_tree / "9/os/x86_64/repodata/.~tmp~/sub2").mkdir()
        # Without ignore: 6 + 3 = 9.
        assert _count_watchable_dirs(mirror_tree, ignore=[]) == 9
        # With ignore: still 6.
        assert _count_watchable_dirs(mirror_tree, ignore=["**/.~tmp~/**"]) == 6

    def test_nonexistent_root_returns_zero(self, tmp_path: Path) -> None:
        assert _count_watchable_dirs(tmp_path / "no-such-dir", ignore=[]) == 0


class TestPreflightLimit:
    """The pre-flight check refuses to start when max_user_watches is too low."""

    async def test_preflight_fails_when_tree_exceeds_limit(
        self, monkeypatch, watch: WatchConfig
    ) -> None:
        # Fixture tree has 6 watchable dirs; advertise a kernel limit of 3.
        monkeypatch.setattr("mirror_mirage.watcher._read_max_user_watches", lambda: 3)
        with pytest.raises(InotifyLimitError) as excinfo:
            async with InotifyWatcher([watch]):
                pass
        msg = str(excinfo.value)
        assert "max_user_watches" in msg
        assert "sysctl" in msg
        # Suggested value should be a sane upward bump (>= 2x needed).
        assert excinfo.value.needed == 6
        assert excinfo.value.available == 3

    async def test_preflight_passes_when_limit_unknown(
        self, monkeypatch, watch: WatchConfig
    ) -> None:
        # When /proc isn't readable (containers, non-Linux), skip the check.
        monkeypatch.setattr("mirror_mirage.watcher._read_max_user_watches", lambda: None)
        async with InotifyWatcher([watch]):
            pass  # must not raise

    async def test_preflight_passes_when_limit_ample(self, monkeypatch, watch: WatchConfig) -> None:
        monkeypatch.setattr("mirror_mirage.watcher._read_max_user_watches", lambda: 1_000_000)
        async with InotifyWatcher([watch]):
            pass
