"""meta/quarantine.jsonl — the C2 隔离池 (workflow_spec §0.4 / plan v3.1 R2).

Candidates the relevance gate (C2, genealogy prune-early) drops *before* fetch
are NOT discarded — each is appended here with its reason. This makes the recall
loss **auditable and reversible**: round_gate checks the pool exists, the recall
audit (R4) re-scans it, and the pre-write pass can resurrect a wrongly-dropped
candidate (spec §0.4: 省钱不得以无审计的召回损失为代价).

One JSON object per line: ``{openalex_id, title, doi, reason, gap, round, source}``.
Title-dropped candidates with no retrievable abstract also carry ``uncertain: true``
(R2-F12): judged on title alone, they are a re-judgeable候选 not a confident reject —
genealogy's ``rejudge_uncertain`` consumes the flag (fetch abstract → re-test).
"""
from __future__ import annotations

import json
import pathlib

from lib import layout


def path(topic_dir: pathlib.Path) -> pathlib.Path:
    return topic_dir / layout.META_DIRNAME / "quarantine.jsonl"


def ensure(topic_dir: pathlib.Path) -> None:
    """Create an empty pool file if absent — marks 'C2 relevance gate ran' even when
    nothing was rejected, so round_gate can require it on a round-based topic."""
    target = path(topic_dir)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")


def append(topic_dir: pathlib.Path, records: list[dict[str, object]]) -> int:
    """Append records to the pool (creates meta/quarantine.jsonl). Returns the
    number written. No-op for an empty list."""
    if not records:
        return 0
    target = path(topic_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return len(records)


def load(topic_dir: pathlib.Path) -> list[dict[str, object]]:
    """Read every quarantined record (skips malformed lines)."""
    target = path(topic_dir)
    if not target.exists():
        return []
    out: list[dict[str, object]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
