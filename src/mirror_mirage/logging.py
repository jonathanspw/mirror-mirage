"""Structured logging helpers.

Mirage logs in a ``key=value`` format on stdout/stderr. systemd captures
these via journald, where each ``key=value`` segment surfaces as a
queryable field via ``journalctl -g``.

Use :func:`get_logger` for a module-scoped logger and :func:`configure`
once at daemon startup.
"""

from __future__ import annotations

import logging
import sys

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_configured = False


def configure(level: int = logging.INFO) -> None:
    """Install the structured stdout handler. Idempotent."""
    global _configured
    root = logging.getLogger("mirror_mirage")
    root.setLevel(level)
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(handler)
    # Don't propagate to the root logger — systemd already captures stdout.
    root.propagate = False
    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a logger configured for structured ``key=value`` emission."""
    return logging.getLogger(f"mirror_mirage.{name}")
