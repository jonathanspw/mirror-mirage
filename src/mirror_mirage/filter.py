"""Event filtering and normalization.

inotify emits a wide range of event types; downstream code in Mirage
cares about only two normalized outcomes:

* ``INVALIDATE`` — a file at the path is now live or has been replaced.
  Sources: ``IN_MOVED_TO``, ``IN_CLOSE_WRITE``.
* ``DELETE`` — a file at the path has been removed.
  Sources: ``IN_DELETE``, ``IN_MOVED_FROM``.

:class:`EventFilter` also applies each watch's ``ignore`` globs to drop
events that should never reach the mapper (e.g. files inside an rsync
``.~tmp~/`` staging directory).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase
from pathlib import Path

from mirror_mirage.config import WatchConfig

_INVALIDATE_MASKS = frozenset({"IN_MOVED_TO", "IN_CLOSE_WRITE"})
_DELETE_MASKS = frozenset({"IN_DELETE", "IN_MOVED_FROM"})


def _gm(pp: list[str], pi: int, sp: tuple[str, ...], si: int) -> bool:
    """Recursive glob matcher with ``**`` support."""
    while pi < len(pp):
        if pp[pi] == "**":
            # Collapse runs of consecutive `**`.
            while pi + 1 < len(pp) and pp[pi + 1] == "**":
                pi += 1
            if pi == len(pp) - 1:
                return True
            return any(_gm(pp, pi + 1, sp, j) for j in range(si, len(sp) + 1))
        if si >= len(sp):
            return False
        if not fnmatchcase(sp[si], pp[pi]):
            return False
        pi += 1
        si += 1
    return si == len(sp)


def matches_any_glob(rel_parts: tuple[str, ...], patterns: list[str]) -> bool:
    """Return True if any pattern matches ``rel_parts``.

    Patterns use forward slashes between components; ``**`` matches any
    number of path components (including zero), ``*`` matches one
    component with fnmatch semantics.
    """
    return any(_gm(pattern.split("/"), 0, rel_parts, 0) for pattern in patterns)


class EventKind(StrEnum):
    """Downstream-relevant event kinds."""

    INVALIDATE = "invalidate"
    DELETE = "delete"


@dataclass(frozen=True)
class FsEvent:
    """A normalized filesystem event ready for the mapper."""

    kind: EventKind
    abs_path: Path
    watch_root: Path


class EventFilter:
    """Stateless event filter parametrized by the watch list.

    Construct once at daemon startup; call :meth:`normalize` for each
    inotify event.
    """

    def __init__(self, watches: list[WatchConfig]) -> None:
        # Try the most specific (deepest) watch first so a nested watch
        # is preferred over its parent when both could match.
        self._watches = sorted(watches, key=lambda w: len(w.path.parts), reverse=True)

    def normalize(self, mask_name: str, abs_path: Path) -> FsEvent | None:
        """Normalize one inotify event.

        Returns ``None`` if the event:
        * Does not belong to any configured watch.
        * Matches an ``ignore`` glob on its owning watch.
        * Has a mask not in the downstream-relevant set.

        Otherwise returns a populated :class:`FsEvent`. ``mask_name`` is the
        symbolic inotify mask (e.g. ``"IN_MOVED_TO"``); when multiple bits
        are set, the caller should pass the most specific one.
        """
        watch = None
        rel: Path | None = None
        for w in self._watches:
            try:
                rel = abs_path.relative_to(w.path)
            except ValueError:
                continue
            watch = w
            break
        if watch is None or rel is None:
            return None

        if matches_any_glob(rel.parts, watch.ignore):
            return None

        if mask_name in _INVALIDATE_MASKS:
            kind = EventKind.INVALIDATE
        elif mask_name in _DELETE_MASKS:
            if not watch.flush_on_delete:
                return None
            kind = EventKind.DELETE
        else:
            return None

        return FsEvent(kind=kind, abs_path=abs_path, watch_root=watch.path)
