"""Dump non-excluded *verified* entries as a flat table for at-a-glance triage.

Surfaces the one view the workflow keeps hand-rolling between rounds: "which
verified entries do I actually have, per gap, and which are already cited in
review.md vs sitting uncited in the store?" — the raw material for deciding
what to prune (see `tools/prune_keep.py`) or pull into the body.

Columns (tab-separated): ``gap`` | ``study_type`` | ``cited?`` | ``key`` |
``doi`` | ``title``. ``cited?`` is ``yes``/``no`` from whether the entry's
``citation_key`` appears in ``review.md`` body prose — computed via the SAME
``lib.citation_scan.scan_used_keys`` that lint_review / gaps_status use, so the
PRISMA-flow + References marker blocks are stripped first and the answer agrees
byte-for-byte with the rest of the toolchain.

Excluded entries (``excluded_reason`` set) and non-verified entries (pending /
failed) are omitted — this is a "what's live and usable" view, not a full store
listing (use `tools/gaps_status.py` for per-gap pending/failed/excluded counts).

Usage:
    python tools/store_dump.py reviews/<topic>
    python tools/store_dump.py reviews/<topic> --gap gap-2
    python tools/store_dump.py reviews/<topic> --uncited
    python tools/store_dump.py reviews/<topic> --gap gap-2 --uncited
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib import testflight
from lib.citation_scan import scan_used_keys
import refs


def _used_keys(topic_dir: pathlib.Path) -> set[str]:
    review_path = topic_dir / "review.md"
    if not review_path.exists():
        return set()
    return scan_used_keys(review_path.read_text(encoding="utf-8"))


def _collect_rows(
    store: refs.Store,
    used_keys: set[str],
    *,
    gap_filter: str | None,
    uncited_only: bool,
) -> list[tuple[str, str, bool, str, str, str]]:
    """Return (gap, study_type, cited, key, doi, title) tuples for every
    non-excluded verified entry, honoring --gap / --uncited filters.

    Sorted by (gap, cited-first-uncited-last, key) so that within a gap the
    not-yet-cited candidates — the rows that drive prune/keep decisions —
    cluster together predictably."""
    rows: list[tuple[str, str, bool, str, str, str, str, str, str]] = []
    for doi, entry in store["entries"].items():
        if entry.get("excluded_reason"):
            continue
        if entry.get("verification_status") != "verified":
            continue
        gap_id = entry.get("gap") if isinstance(entry.get("gap"), str) else "<no gap>"
        if gap_filter is not None and gap_id != gap_filter:
            continue
        key = entry.get("citation_key") if isinstance(entry.get("citation_key"), str) else ""
        cited = bool(key) and key in used_keys
        if uncited_only and cited:
            continue
        study_type = entry.get("study_type") if isinstance(entry.get("study_type"), str) else "other"
        title = entry.get("title") if isinstance(entry.get("title"), str) else ""
        # C17 (m5): surface grounding tier / source / round so a prune/keep decision can
        # see WHY an entry is weak (title_only) or where it came from, without re-greping.
        grounding = refs.grounding(entry)
        source = entry.get("source") if isinstance(entry.get("source"), str) else ""
        rnd = str(entry.get("added_round") if entry.get("added_round") is not None else "")
        rows.append((gap_id, study_type, cited, key, doi, title, grounding, source, rnd))
    rows.sort(key=lambda r: (r[0], not r[2], r[3]))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Dump non-excluded verified entries as a "
            "'gap|study_type|cited?|key|doi|title' table. cited? reflects "
            "whether the citation_key appears in review.md body prose."
        )
    )
    parser.add_argument("topic_dir", help="Path to a topic directory under reviews/")
    parser.add_argument("--gap", help="Only dump entries tagged with this gap id (e.g. gap-2).")
    parser.add_argument(
        "--uncited",
        action="store_true",
        help="Only show entries NOT cited in review.md (prune/keep candidates).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit rows as JSON (C17): {gap, study_type, cited, key, doi, title, grounding, source, round}.",
    )
    args = parser.parse_args()

    topic_dir = pathlib.Path(args.topic_dir)
    with testflight.timer("store_dump", "main", topic_dir=topic_dir) as detail:
        store = refs.load(topic_dir)
        if store is None:
            print(f"[ERROR] missing references store: {topic_dir}", file=sys.stderr)
            raise SystemExit(1)

        used_keys = _used_keys(topic_dir)
        rows = _collect_rows(
            store,
            used_keys,
            gap_filter=args.gap,
            uncited_only=args.uncited,
        )
        detail.update({"rows": len(rows)})

        if args.json:
            import json
            payload = [
                {"gap": r[0], "study_type": r[1], "cited": r[2], "key": r[3],
                 "doi": r[4], "title": r[5], "grounding": r[6], "source": r[7], "round": r[8]}
                for r in rows
            ]
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("gap\tstudy_type\tcited?\tgrounding\tsource\tround\tkey\tdoi\ttitle")
            for gap_id, study_type, cited, key, doi, title, grounding, source, rnd in rows:
                cited_text = "yes" if cited else "no"
                print(f"{gap_id}\t{study_type}\t{cited_text}\t{grounding}\t{source}\t{rnd}\t{key}\t{doi}\t{title}")
        print(f"{len(rows)} rows", file=sys.stderr)


if __name__ == "__main__":
    main()
