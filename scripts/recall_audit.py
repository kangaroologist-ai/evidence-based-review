"""tools/recall_audit.py — known-item recall audit (plan v3.1 R4 / workflow_spec §0.4).

Quantifies the false-negative rate of search + the C2 relevance gate: did the
pipeline actually RETRIEVE the literature it should have? A curated set of
landmark DOIs — ``meta/known_items.txt`` (one DOI per line, ``#`` comments ok),
or, absent that, the seed-sourced entries the author named — MUST appear,
verified, in the store. Misses = papers we KNOW are on-topic but the pipeline
failed to find. Any miss that is sitting in ``meta/quarantine.jsonl`` is a **C2
false-drop** (the relevance gate wrongly quarantined a real landmark) — the
highest-priority signal, listed separately.

Writes ``meta/recall_audit.json`` (round_gate checks this exists; spec §4
process) and prints a research_log marker line to paste. Exit 0 always — an
audit reports; the gate reads the artifact (spec §0.4 召回审计 ↔ §0.6 忠实度审计
对称：省钱不得以无审计的召回损失为代价).

    python tools/recall_audit.py reviews/<topic> [--known-items FILE]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import refs
from lib import layout, quarantine

_DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "doi.org/", "doi:")


def normalize_doi(raw: str) -> str:
    doi = (raw or "").strip()
    low = doi.lower()
    for prefix in _DOI_PREFIXES:
        if low.startswith(prefix):
            return doi[len(prefix):].lower()
    return low


def _known_items_path(topic_dir: pathlib.Path, explicit: str | None) -> pathlib.Path:
    if explicit:
        return pathlib.Path(explicit)
    return topic_dir / layout.META_DIRNAME / "known_items.txt"


def _known_items(
    topic_dir: pathlib.Path, store: refs.Store, explicit: str | None
) -> tuple[list[str], str]:
    path = _known_items_path(topic_dir, explicit)
    if path.exists():
        items = [
            normalize_doi(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        return items, "known_items.txt"
    # fallback: seed-sourced entries (author-named landmarks). Always ~100% hits,
    # but still catches a seed that was accidentally excluded / quarantined.
    seeds = [
        doi
        for doi, entry in store.get("entries", {}).items()
        if isinstance(entry.get("source"), str) and entry["source"].startswith("seed")
    ]
    return seeds, "seed-entries"


def audit(topic_dir: pathlib.Path, explicit_known: str | None = None) -> dict[str, object]:
    store = refs.load(topic_dir)
    if store is None:
        return {"error": f"no references store under {topic_dir}"}
    known, source = _known_items(topic_dir, store, explicit_known)
    entries = store.get("entries", {})
    quarantined = {
        normalize_doi(str(record.get("doi") or ""))
        for record in quarantine.load(topic_dir)
        if record.get("doi")
    }

    hits: list[str] = []
    misses: list[str] = []
    misses_in_quarantine: list[str] = []
    for doi in known:
        entry = entries.get(doi)
        if (
            entry is not None
            and entry.get("verification_status") == "verified"
            and not entry.get("excluded_reason")
        ):
            hits.append(doi)
        else:
            misses.append(doi)
            if doi in quarantined:
                misses_in_quarantine.append(doi)

    total = len(known)
    return {
        "known_items": total,
        "hits": len(hits),
        "misses": misses,
        "hit_rate": round(len(hits) / total, 3) if total else 1.0,
        "misses_in_quarantine": misses_in_quarantine,
        "known_source": source,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="recall_audit — known-item 召回审计 (R4).")
    parser.add_argument("topic_dir")
    parser.add_argument("--known-items", default=None, help="File of landmark DOIs.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    topic_dir = pathlib.Path(args.topic_dir)
    if not topic_dir.is_dir():
        print(f"[ERROR] not a topic dir: {topic_dir}", file=sys.stderr)
        raise SystemExit(2)

    result = audit(topic_dir, args.known_items)
    meta = topic_dir / layout.META_DIRNAME
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "recall_audit.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # C-RA (m9): seed-fallback uses the store's own seeds as known-items → hit_rate≈1.0 by
    # construction = ZERO detection power. round_gate already returns `pending` (not pass) on
    # this source; here we also WARN loudly on stderr so the operator knows the audit is
    # vacuous until a real known-item list is supplied (spec §0.4 否则死契约).
    if result.get("known_source") == "seed-entries":
        print(
            "[WARN] recall_audit 用 seed-fallback 当 known-items（hit_rate≈1.0、零检测力）— "
            "请提供 meta/known_items.txt（地标综述 / 已知文献 DOI），才能真正量化召回假阴率（spec §0.4）",
            file=sys.stderr,
        )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if "error" in result:
            print(f"[ERROR] {result['error']}")
            raise SystemExit(2)
        print(
            f"recall_audit: {result['hits']}/{result['known_items']} known items found "
            f"(hit_rate={result['hit_rate']}, source={result['known_source']})"
        )
        if result["misses"]:
            print(f"  misses: {', '.join(result['misses'][:10])}")
        if result["misses_in_quarantine"]:
            print(
                f"  ⚠ C2 false-drop（在隔离池里的 known item，应复活）: "
                f"{', '.join(result['misses_in_quarantine'])}"
            )
        print(
            f"  → research_log 标记: 召回审计 hit_rate={result['hit_rate']} "
            f"({result['hits']}/{result['known_items']}), misses={len(result['misses'])}"
        )
    raise SystemExit(0)


if __name__ == "__main__":
    main()
