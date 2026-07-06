"""Re-tag an entry's gap label. Use when an entry was first seeded under one
gap (gap-3) but on rereading better fits another (gap-4). `refs.upsert`
preserves the first-seen gap on purpose; this CLI is the deliberate override.

Usage:
    python tools/regap.py reviews/<topic> DOI gap-N
    python tools/regap.py reviews/<topic> DOI --clear   # detach from any gap

Refuses to set a gap that has not been declared in the store. Appends to
`research_log.md` for an audit trail.
"""
from __future__ import annotations

import argparse
import difflib
import pathlib
import sys
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib import testflight
import refs


def closest_gaps(target: str, store: refs.Store, *, n: int = 3, cutoff: float = 0.4) -> list[str]:
    """plan v3 B2: fuzzy-match a (possibly hallucinated) gap label against the
    declared gap ids AND their descriptions; return up to ``n`` candidate gap
    ids best matching ``target``. Empty when nothing is close — the caller must
    then refuse rather than silently mis-attach."""
    gaps = store.get("gaps", {})
    if not gaps:
        return []
    ids = list(gaps)
    ordered: list[str] = []
    for gid in difflib.get_close_matches(target, ids, n=n, cutoff=cutoff):
        if gid not in ordered:
            ordered.append(gid)
    # also match against descriptions, mapping back to the gap id
    desc_to_id = {
        (g.get("description") or ""): gid
        for gid, g in gaps.items()
        if isinstance(g, dict) and g.get("description")
    }
    for desc in difflib.get_close_matches(target, list(desc_to_id), n=n, cutoff=cutoff):
        gid = desc_to_id[desc]
        if gid not in ordered:
            ordered.append(gid)
    return ordered[:n]


def best_gap(target: str, store: refs.Store, *, cutoff: float = 0.8) -> str | None:
    """Single confident gap-id match for ``target`` (high cutoff), or None.
    Used by --accept-closest so a near-perfect typo auto-resolves while
    prefix-sharing siblings (which the loose closest_gaps() surfaces) do not."""
    ids = list(store.get("gaps", {}))
    matches = difflib.get_close_matches(target, ids, n=1, cutoff=cutoff)
    return matches[0] if matches else None


def _append_log(topic_dir: pathlib.Path, doi: str, before: str | None, after: str | None) -> None:
    log_path = topic_dir / "research_log.md"
    timestamp = datetime.now().isoformat(timespec="seconds")
    line = f"- [{timestamp}] regap `{doi}`: {before or '<none>'} → {after or '<none>'}"
    if log_path.exists():
        existing = log_path.read_text(encoding="utf-8").rstrip()
        log_path.write_text(existing + "\n" + line + "\n", encoding="utf-8")
        return
    log_path.write_text(line + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("topic_dir")
    parser.add_argument("doi")
    parser.add_argument("new_gap", nargs="?", default=None)
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Detach the entry from any gap (sets gap=None).",
    )
    parser.add_argument(
        "--accept-closest",
        action="store_true",
        help="If new_gap is undeclared, apply the single best fuzzy-matched "
        "declared gap instead of erroring (B2). Refuses if 0 or >1 candidates.",
    )
    args = parser.parse_args()

    if args.clear and args.new_gap:
        print("[ERROR] --clear conflicts with positional new_gap")
        raise SystemExit(2)
    if not args.clear and not args.new_gap:
        print("[ERROR] specify a gap id or --clear")
        raise SystemExit(2)

    topic_dir = pathlib.Path(args.topic_dir)
    with testflight.timer("regap", "main", topic_dir=topic_dir, doi=args.doi):
        store = refs.load(topic_dir)
        if store is None:
            print(f"[ERROR] missing references store: {topic_dir}")
            raise SystemExit(1)

        doi = args.doi.lower()
        entry = store["entries"].get(doi)
        if entry is None:
            print(f"[ERROR] DOI not in store: {doi}")
            raise SystemExit(1)

        new_gap = None if args.clear else args.new_gap
        if new_gap is not None and new_gap not in store.get("gaps", {}):
            confident = best_gap(new_gap, store) if args.accept_closest else None
            if confident is not None:
                print(f"[OK] --accept-closest: '{new_gap}' → {confident}")
                new_gap = confident
            else:
                candidates = closest_gaps(new_gap, store)
                print(f"[ERROR] gap not declared: {new_gap}")
                if candidates:
                    declared = store.get("gaps", {})
                    hints = ", ".join(
                        f"{gid} ({(declared.get(gid, {}).get('description') or '')[:40]})"
                        for gid in candidates
                    )
                    print(f"  did you mean: {hints}")
                    print("  (pass --accept-closest to apply when there is a single match,")
                    print("   or declare the gap / pick the right id — never silently mis-attach)")
                raise SystemExit(1)

        before = entry.get("gap")
        refs.set_gap(store, doi, new_gap)
        refs.save(topic_dir, store)
        _append_log(topic_dir, doi, before, new_gap)
        print(f"[OK] regap {doi}: {before or '<none>'} → {new_gap or '<none>'}")


if __name__ == "__main__":
    main()
