"""Client-liveness probe for daemon-routed store commits (v3.2 C12).

A long daemon-routed tool (genealogy / a round) can run for minutes. If the client
(the CLI / orchestrator) vanishes mid-run, finishing the run and committing the store
to disk is wasteful at best and — when the client died because the user aborted /
re-issued the round — produces a half-applied store the operator never saw.

This module is a NEUTRAL indirection so neither ``refs`` nor ``daemon`` imports the
other: the daemon registers a per-thread ``probe`` (a conn-liveness check); ``refs.save``
calls ``ensure_alive()`` right before it starts writing and raises ``ClientGone`` if the
peer is gone, so the on-disk store is left untouched. Outside the daemon no probe is
registered → ``alive()`` is always True → CLI / tests are byte-for-byte unaffected.
"""
from __future__ import annotations

import threading
from typing import Callable

_local = threading.local()


class ClientGone(Exception):
    """The daemon client disconnected before the store was committed (C12). Raised by
    ``ensure_alive()`` so the tool aborts without writing a half-applied store."""


def set_probe(probe: "Callable[[], bool] | None") -> None:
    """Register a per-thread liveness probe returning True while the client is connected."""
    _local.probe = probe


def clear_probe() -> None:
    _local.probe = None


def alive() -> bool:
    """True if no probe is registered (CLI / tests) or the registered probe says the
    client is still connected. A probe that itself raises is treated as 'alive' (never
    let a flaky probe abort a real commit)."""
    probe = getattr(_local, "probe", None)
    if probe is None:
        return True
    try:
        return bool(probe())
    except Exception:
        return True


def ensure_alive() -> None:
    """Raise ClientGone if the daemon client has disconnected (no-op outside the daemon)."""
    if not alive():
        raise ClientGone("client disconnected before store commit (C12)")
