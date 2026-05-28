"""Recursive inotify watcher.

Wraps :mod:`asyncinotify` to expose an async iterator of normalized
events. Responsibilities:

* On startup, walk every configured watch root and add a watch on every
  directory inside it (skipping ``ignore``-matched dirs).
* React to ``IN_CREATE | IN_ISDIR`` by adding a watch on the new
  directory **and** rescanning its contents to emit synthetic events for
  anything created between ``mkdir`` and watch registration.
* React to ``IN_DELETE_SELF`` / ``IN_MOVE_SELF`` by dropping watches.

The watcher emits ``(mask_name, abs_path, watch_root)`` tuples; the
filter and mapper consume these.
"""

from __future__ import annotations

import errno
import logging
import os
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from types import TracebackType

from asyncinotify import Inotify, Mask

from mirror_mirage.config import WatchConfig
from mirror_mirage.filter import matches_any_glob

_log = logging.getLogger("mirror_mirage.watcher")

_WATCH_MASK = (
    Mask.MOVED_TO
    | Mask.CLOSE_WRITE
    | Mask.DELETE
    | Mask.MOVED_FROM
    | Mask.CREATE
    | Mask.MOVE_SELF
    | Mask.DELETE_SELF
)

_MAX_USER_WATCHES_PROC = Path("/proc/sys/fs/inotify/max_user_watches")


class InotifyLimitError(Exception):
    """The kernel inotify watch budget is too small for the configured tree."""

    def __init__(self, *, needed: int, available: int) -> None:
        self.needed = needed
        self.available = available
        # Suggest a comfortable round-number bump.
        suggested = max(needed * 2, 524288)
        super().__init__(
            f"inotify watch budget too small: need at least {needed} watches "
            f"but fs.inotify.max_user_watches is {available}. "
            f"Raise it (root) with: "
            f"sysctl -w fs.inotify.max_user_watches={suggested}  "
            f"and persist it in /etc/sysctl.d/99-mirror-mirage.conf"
        )


def _read_max_user_watches() -> int | None:
    """Return the current ``fs.inotify.max_user_watches`` value, or ``None``.

    Returns ``None`` on any read failure — non-Linux, container namespaces
    that hide /proc, or a custom kernel build without inotify. In that case
    the pre-flight check is skipped (the runtime ENOSPC handler still
    catches over-budget conditions, just less gracefully).
    """
    try:
        return int(_MAX_USER_WATCHES_PROC.read_text().strip())
    except (OSError, ValueError):
        return None


def _count_watchable_dirs(root: Path, ignore: list[str]) -> int:
    """Count directories under ``root`` that the watcher would add a watch on.

    Honors the same ``ignore`` globs that the watcher applies, so e.g.
    ``**/.~tmp~/**`` subtrees are not counted. Symlinks are not
    followed, matching the watcher's behavior.
    """
    if not root.is_dir():
        return 0
    count = 0
    for current_str, dirs, _files in os.walk(root, followlinks=False):
        count += 1
        current = Path(current_str)
        kept: list[str] = []
        for d in dirs:
            child_rel = (current / d).relative_to(root).parts
            if matches_any_glob(child_rel, ignore):
                continue
            kept.append(d)
        # Prune ignored children in-place so os.walk skips them.
        dirs[:] = kept
    return count


def _mask_to_name(mask: Mask) -> str | None:
    """Pick the most specific symbolic name for ``mask``.

    Returns ``None`` if no downstream-relevant bit is set.
    """
    if mask & Mask.MOVED_TO:
        return "IN_MOVED_TO"
    if mask & Mask.CLOSE_WRITE:
        return "IN_CLOSE_WRITE"
    if mask & Mask.DELETE:
        return "IN_DELETE"
    if mask & Mask.MOVED_FROM:
        return "IN_MOVED_FROM"
    return None


class InotifyWatcher:
    """Async-iterable inotify wrapper with recursive watch management."""

    def __init__(self, watches: list[WatchConfig]) -> None:
        self._watches = watches
        self._inotify: Inotify | None = None
        # watched directory path → owning watch root.
        self._dir_to_root: dict[Path, Path] = {}
        # watched directory path → owning watch config (for ignore globs).
        self._dir_to_config: dict[Path, WatchConfig] = {}
        # synthetic events queued for emission (e.g. from the inotify race window).
        self._synthetic: deque[tuple[str, Path, Path]] = deque()

    async def __aenter__(self) -> InotifyWatcher:
        """Open the inotify fd and add watches on all existing directories.

        Performs a pre-flight: if ``fs.inotify.max_user_watches`` is
        readable and lower than the number of directories we would watch,
        raises :class:`InotifyLimitError` *before* opening the inotify
        fd. A second backstop (catching ``ENOSPC`` from ``add_watch``)
        handles the case where the limit is hit during recursion — for
        example, when another process under the same UID consumes
        watches between the pre-flight and the actual add.
        """
        needed = sum(_count_watchable_dirs(wc.path, wc.ignore) for wc in self._watches)
        available = _read_max_user_watches()
        if available is not None and needed > available:
            raise InotifyLimitError(needed=needed, available=available)
        _log.info(
            "event=watch_budget needed=%d max_user_watches=%s",
            needed,
            available if available is not None else "unknown",
        )

        self._inotify = Inotify()
        self._inotify.__enter__()
        try:
            for wc in self._watches:
                self._add_recursive(wc.path, wc)
        except InotifyLimitError:
            # Close the inotify fd before propagating — __aexit__ won't run.
            self._inotify.__exit__(None, None, None)
            self._inotify = None
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the inotify fd."""
        if self._inotify is not None:
            self._inotify.__exit__(exc_type, exc, tb)
        self._inotify = None
        self._dir_to_root.clear()
        self._dir_to_config.clear()
        self._synthetic.clear()

    def _is_ignored(self, path: Path, watch_root: Path, ignore: list[str]) -> bool:
        try:
            rel = path.relative_to(watch_root)
        except ValueError:
            return False
        return matches_any_glob(rel.parts, ignore)

    def _add_recursive(self, path: Path, wc: WatchConfig) -> None:
        """Add a watch on ``path`` and recurse into its existing subdirectories."""
        assert self._inotify is not None
        if not path.is_dir():
            return
        if path != wc.path and self._is_ignored(path, wc.path, wc.ignore):
            return
        if path in self._dir_to_root:
            return  # already watched
        try:
            self._inotify.add_watch(path, _WATCH_MASK)
        except OSError as e:
            if e.errno == errno.ENOSPC:
                # Surface as a startup error — partial coverage is worse
                # than no coverage; the operator needs to raise the limit.
                raise InotifyLimitError(
                    needed=len(self._dir_to_root) + 1,
                    available=len(self._dir_to_root),
                ) from e
            _log.warning("event=add_watch_failed path=%s error=%s", path, e)
            return
        self._dir_to_root[path] = wc.path
        self._dir_to_config[path] = wc
        try:
            entries = list(os.scandir(path))
        except OSError:
            return
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    self._add_recursive(Path(entry.path), wc)
            except OSError:
                continue

    def _add_dir_and_scan(self, new_dir: Path, wc: WatchConfig) -> None:
        """Add a watch on a newly-created subdirectory and emit synthetic events.

        Files created in ``new_dir`` between ``mkdir`` and our ``add_watch``
        won't have generated inotify events that we can observe, so we
        scan after registering the watch and synthesize ``IN_MOVED_TO``
        events for anything already there.
        """
        assert self._inotify is not None
        if self._is_ignored(new_dir, wc.path, wc.ignore):
            return
        if new_dir in self._dir_to_root:
            return
        try:
            self._inotify.add_watch(new_dir, _WATCH_MASK)
        except OSError as e:
            if e.errno == errno.ENOSPC:
                _log.error(
                    "event=add_watch_enospc path=%s "
                    "fs.inotify.max_user_watches exhausted at runtime; "
                    "events under this directory will be missed. "
                    "Raise the limit and restart mirror_mirage.",
                    new_dir,
                )
            else:
                _log.warning("event=add_watch_failed path=%s error=%s", new_dir, e)
            return
        self._dir_to_root[new_dir] = wc.path
        self._dir_to_config[new_dir] = wc
        try:
            entries = list(os.scandir(new_dir))
        except OSError:
            return
        for entry in entries:
            child = Path(entry.path)
            try:
                if entry.is_dir(follow_symlinks=False):
                    self._add_dir_and_scan(child, wc)
                else:
                    if self._is_ignored(child, wc.path, wc.ignore):
                        continue
                    self._synthetic.append(("IN_MOVED_TO", child, wc.path))
            except OSError:
                continue

    def __aiter__(self) -> AsyncIterator[tuple[str, Path, Path]]:
        """Yield ``(mask_name, abs_path, watch_root)`` until canceled."""
        return self._iter()

    async def _iter(self) -> AsyncIterator[tuple[str, Path, Path]]:
        assert self._inotify is not None
        while True:
            if self._synthetic:
                yield self._synthetic.popleft()
                continue

            event = await self._inotify.get()
            mask = event.mask

            # Kernel inotify queue overflowed — events were dropped.
            # Cache coverage is now inconsistent until the next change
            # touches each affected file. Log loudly so operators know.
            if mask & Mask.Q_OVERFLOW:
                _log.error(
                    "event=inotify_overflow "
                    "kernel inotify queue overflowed; some filesystem events were lost. "
                    "Consider raising fs.inotify.max_queued_events.",
                )
                continue

            # Defensive: events without an associated watch (other than the
            # overflow notification handled above) shouldn't reach us, but
            # asyncinotify's type allows None — skip rather than crash.
            if event.watch is None:
                continue
            watch_dir = event.watch.path
            full_path = event.path  # already includes event.name when present

            # Watch removal — clean up our bookkeeping.
            if mask & (Mask.IGNORED | Mask.DELETE_SELF | Mask.MOVE_SELF):
                self._dir_to_root.pop(watch_dir, None)
                self._dir_to_config.pop(watch_dir, None)
                continue

            if mask & Mask.ISDIR:
                # Directory event; only IN_CREATE matters (start watching it).
                if mask & Mask.CREATE and full_path is not None:
                    wc = self._dir_to_config.get(watch_dir)
                    if wc is not None:
                        self._add_dir_and_scan(full_path, wc)
                continue

            if full_path is None:
                continue
            watch_root = self._dir_to_root.get(watch_dir)
            if watch_root is None:
                continue

            mask_name = _mask_to_name(mask)
            if mask_name is None:
                continue

            yield (mask_name, full_path, watch_root)
