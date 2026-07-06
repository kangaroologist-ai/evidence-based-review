"""Batch-exclude every non-excluded verified entry whose citation_key is NOT in
a keep-set. The inverse of hand-running `exclude.py` once per noise DOI after a
round's triage: you write down the keys you DECIDED to keep, and this prunes
everything else.

Reuses `exclude.py`'s write path verbatim — `refs.exclude_entry` + `refs.save`
plus the same `research_log.md` audit line — so a bulk prune is indistinguishable
from N hand `exclude.py` calls (excluded entries stay in the store for audit and
are barred from the body by lint_review / render_refs). Nothing is hard-deleted.

The keep-file lists one citation_key per line; blank lines and `#` comments are
ignored. Only *verified, non-excluded* entries are candidates — pending / failed
/ already-excluded entries are left untouched. A keep-key that matches no live
entry is reported as a warning (likely a typo) but does not abort the prune.

Usage:
    python tools/prune_keep.py reviews/<topic> --keep-file keep.txt
    python tools/prune_keep.py reviews/<topic> --keep-file keep.txt --dry-run
    python tools/prune_keep.py reviews/<topic> --keep-file keep.txt --reason "round-4 prune"
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib import testflight
import refs
# Reuse the exact audit-log writer exclude.py uses, so bulk prune leaves the
# same research_log trail as hand exclusions (no second copy to drift).
from exclude import _append_log

_DEFAULT_REASON = "prune_keep: citation_key not in keep-set"


def _read_keep_keys(keep_file: pathlib.Path) -> set[str]:
    """One citation_key per line; ignore blanks and `#` comments."""
    keys: set[str] = set()
    for raw in keep_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        keys.add(line)
    return keys


def _prune_targets(
    store: refs.Store,
    keep_keys: set[str],
) -> tuple[list[tuple[str, str]], set[str]]:
    """Return (targets, matched_keep_keys).

    targets = [(doi, citation_key), ...] for every non-excluded verified entry
    whose key is NOT in keep_keys. matched_keep_keys = the subset of keep_keys
    that actually resolved to a live verified entry (so the caller can warn
    about unmatched — probably mistyped — keep keys)."""
    targets: list[tuple[str, str]] = []
    matched: set[str] = set()
    for doi, entry in store["entries"].items():
        if entry.get("excluded_reason"):
            continue
        if entry.get("verification_status") != "verified":
            continue
        key = entry.get("citation_key") if isinstance(entry.get("citation_key"), str) else ""
        if key and key in keep_keys:
            matched.add(key)
            continue
        targets.append((doi, key or "(no key)"))
    targets.sort(key=lambda t: t[1])
    return targets, matched


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Batch-exclude non-excluded verified entries whose citation_key "
            "is not listed in --keep-file. Reuses exclude.py's write path; "
            "nothing is hard-deleted. Use --dry-run to preview."
        )
    )
    parser.add_argument("topic_dir", help="Path to a topic directory under reviews/")
    parser.add_argument(
        "--keep-file",
        required=True,
        help="File with one citation_key to KEEP per line (# comments / blanks ignored).",
    )
    parser.add_argument(
        "--reason",
        default=_DEFAULT_REASON,
        help="Exclusion reason recorded on each pruned entry + research_log.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print what would be excluded; do not modify the store.",
    )
    args = parser.parse_args()

    topic_dir = pathlib.Path(args.topic_dir)
    op = "dry_run" if args.dry_run else "main"
    with testflight.timer("prune_keep", op, topic_dir=topic_dir) as detail:
        keep_file = pathlib.Path(args.keep_file)
        if not keep_file.exists():
            print(f"[ERROR] keep-file not found: {keep_file}", file=sys.stderr)
            raise SystemExit(1)

        store = refs.load(topic_dir)
        if store is None:
            print(f"[ERROR] missing references store: {topic_dir}", file=sys.stderr)
            raise SystemExit(1)

        keep_keys = _read_keep_keys(keep_file)
        if not keep_keys:
            print(
                f"[ERROR] keep-file is empty: {keep_file}. Refusing to prune "
                "every verified entry — pass a non-empty keep-set.",
                file=sys.stderr,
            )
            raise SystemExit(2)

        targets, matched = _prune_targets(store, keep_keys)
        unmatched = sorted(keep_keys - matched)
        detail.update({"targets": len(targets), "keep": len(keep_keys)})

        for key in unmatched:
            print(
                f"[WARN] keep key not found among live verified entries: {key}",
                file=sys.stderr,
            )

        if args.dry_run:
            print(f"[DRY-RUN] would exclude {len(targets)} entries "
                  f"(keep-set={len(keep_keys)}, reason={args.reason!r}):")
            for doi, key in targets:
                print(f"  {key}\t{doi}")
            return

        if not targets:
            print("[OK] nothing to prune — all verified entries are in the keep-set.")
            return

        for doi, _key in targets:
            refs.exclude_entry(store, doi, args.reason)
        refs.save(topic_dir, store)
        for doi, _key in targets:
            _append_log(topic_dir, doi, "exclude", args.reason)
        print(f"[OK] excluded {len(targets)} entries (kept {len(matched)}).")


if __name__ == "__main__":
    main()
