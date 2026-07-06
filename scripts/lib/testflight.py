"""Testflight-only timing + structured event log.

Off by default — set ``HEALTH_REVIEW_TESTFLIGHT=1`` for the duration of a
testflight session and every tool that wraps ``timer(...)`` will:

1. Print one ``[testflight] <tool>.<op> elapsed=...s ...`` line to stderr.
2. Append a JSON line to ``<topic_dir>/testflight.jsonl`` (or
   ``state/testflight.jsonl`` when no topic is in context).

Outside testflight the wrapper is a near-zero-cost no-op (one env lookup
per call), so it is safe to leave the instrumentation in place.

Usage:

    from lib import testflight
    with testflight.timer("verify", "main", topic_dir=topic_dir, parallel=4):
        ...

The companion ``tools/profile.py`` reads the JSONL log and prints a
summary by tool / op.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pathlib
import sys
import time
from collections.abc import Iterator
from datetime import datetime, timezone

from . import project

_ENV_VAR = "HEALTH_REVIEW_TESTFLIGHT"


def is_active() -> bool:
    raw = os.environ.get(_ENV_VAR, "").strip().lower()
    return raw not in {"", "0", "false", "no", "off"}


def _log_path(topic_dir: pathlib.Path | None) -> pathlib.Path:
    if topic_dir is not None:
        return topic_dir / "testflight.jsonl"
    return project.project_root() / "state" / "testflight.jsonl"


def _append_jsonl(path: pathlib.Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(line)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def record(
    tool: str,
    op: str,
    elapsed_sec: float,
    *,
    topic_dir: pathlib.Path | None = None,
    **details: object,
) -> None:
    """Emit a single testflight event. No-op when testflight mode is off."""
    if not is_active():
        return
    payload: dict[str, object] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": tool,
        "op": op,
        "elapsed_sec": round(elapsed_sec, 4),
    }
    if topic_dir is not None:
        payload["topic"] = topic_dir.name
    payload.update(details)
    _append_jsonl(_log_path(topic_dir), payload)
    detail_text = (
        " "
        + " ".join(f"{key}={value}" for key, value in details.items())
        if details
        else ""
    )
    sys.stderr.write(
        f"[testflight] {tool}.{op} elapsed={elapsed_sec:.2f}s{detail_text}\n"
    )
    sys.stderr.flush()


@contextlib.contextmanager
def timer(
    tool: str,
    op: str,
    *,
    topic_dir: pathlib.Path | None = None,
    **details: object,
) -> Iterator[dict[str, object]]:
    """Context manager that times a block and records it on exit. Yields a
    mutable detail dict so callers can attach late-known fields (counts,
    sub-phase outcomes) before the recording fires:

        with testflight.timer("fetch", "main", topic_dir=t) as detail:
            ...
            detail["processed"] = n
    """
    if not is_active():
        yield {}
        return
    mutable: dict[str, object] = dict(details)
    start = time.perf_counter()
    try:
        yield mutable
    finally:
        record(tool, op, time.perf_counter() - start, topic_dir=topic_dir, **mutable)
