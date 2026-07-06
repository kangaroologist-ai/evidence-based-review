"""Mark a verified entry as excluded with a reason. Excluded entries stay in
the references store for audit but are barred from the review body
(`lint_review.py` / `render_refs.py` both fail if an excluded key is cited).

Usage:
    python tools/exclude.py reviews/<topic> DOI "reason text"
    python tools/exclude.py reviews/<topic> DOI --include   # remove exclusion

The reason is logged at the end of `research_log.md` so the audit trail is
visible without grepping JSON.

Sticky denylist (§B): the exclusion written here is a persistent denylist.
`refs.is_excluded(store, doi)` is an O(1) check derived from the same
``excluded_reason`` field (no separate set to keep in sync), and the add-paths
— search.py --auto-add, genealogy.py candidate apply, verify.py --add — all
consult it, so an excluded DOI is no longer silently re-added by a later
re-search or genealogy expansion. Resurrecting one is deliberate and audited:
`verify.py --add ... --readd --readd-reason "..."`.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib import testflight
import refs


def _append_log(topic_dir: pathlib.Path, doi: str, action: str, reason: str | None) -> None:
    log_path = topic_dir / "research_log.md"
    timestamp = datetime.now().isoformat(timespec="seconds")
    line = f"- [{timestamp}] {action} `{doi}`"
    if reason:
        line += f" — {reason}"
    if log_path.exists():
        existing = log_path.read_text(encoding="utf-8").rstrip()
        log_path.write_text(existing + "\n" + line + "\n", encoding="utf-8")
        return
    log_path.write_text(line + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("topic_dir")
    parser.add_argument("doi")
    parser.add_argument("reason", nargs="?", default="")
    parser.add_argument(
        "--include",
        action="store_true",
        help="Reverse a prior exclusion (clears excluded_reason).",
    )
    args = parser.parse_args()

    topic_dir = pathlib.Path(args.topic_dir)
    op = "include" if args.include else "exclude"
    with testflight.timer("exclude", op, topic_dir=topic_dir, doi=args.doi):
        store = refs.load(topic_dir)
        if store is None:
            print(f"[ERROR] missing references store: {topic_dir}")
            raise SystemExit(1)

        doi = args.doi.lower()
        if doi not in store["entries"]:
            print(f"[ERROR] DOI not in store: {doi}")
            raise SystemExit(1)

        if args.include:
            refs.include_entry(store, doi)
            refs.save(topic_dir, store)
            _append_log(topic_dir, doi, "include", None)
            print(f"[OK] cleared exclusion: {doi}")
            return

        if not args.reason:
            print("[ERROR] reason is required (or pass --include to reverse)")
            raise SystemExit(2)

        refs.exclude_entry(store, doi, args.reason)
        refs.save(topic_dir, store)
        _append_log(topic_dir, doi, "exclude", args.reason)
        print(f"[OK] excluded: {doi} — {args.reason}")


if __name__ == "__main__":
    main()
