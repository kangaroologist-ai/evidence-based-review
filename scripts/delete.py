"""Permanently remove an entry from a topic's references store.

Prefer `tools/exclude.py` for routine取舍 — exclusion preserves the audit
trail. Use this only when an entry was added by mistake (wrong DOI typed,
duplicate after dedupe, etc.). Refuses to delete entries that have already
been cited in `review.md` so accidental deletions don't break lint.

Usage:
    python tools/delete.py reviews/<topic> DOI
    python tools/delete.py reviews/<topic> DOI --force   # bypass cited check
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib import testflight
from lib.citation_scan import CITE_RE as _CITE_RE
import refs


def _is_cited(topic_dir: pathlib.Path, citation_key: str | None) -> bool:
    if not citation_key:
        return False
    review_path = topic_dir / "review.md"
    if not review_path.exists():
        return False
    used = set(_CITE_RE.findall(review_path.read_text(encoding="utf-8")))
    return citation_key in used


def _append_log(topic_dir: pathlib.Path, doi: str, key: str | None) -> None:
    log_path = topic_dir / "research_log.md"
    timestamp = datetime.now().isoformat(timespec="seconds")
    suffix = f" (key={key})" if key else ""
    line = f"- [{timestamp}] delete `{doi}`{suffix}"
    if log_path.exists():
        existing = log_path.read_text(encoding="utf-8").rstrip()
        log_path.write_text(existing + "\n" + line + "\n", encoding="utf-8")
        return
    log_path.write_text(line + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("topic_dir")
    parser.add_argument("doi")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete even if the citation key is referenced in review.md.",
    )
    args = parser.parse_args()

    topic_dir = pathlib.Path(args.topic_dir)
    with testflight.timer("delete", "main", topic_dir=topic_dir, doi=args.doi):
        store = refs.load(topic_dir)
        if store is None:
            print(f"[ERROR] missing references store: {topic_dir}")
            raise SystemExit(1)

        doi = args.doi.lower()
        entry = store["entries"].get(doi)
        if entry is None:
            print(f"[ERROR] DOI not in store: {doi}")
            raise SystemExit(1)

        citation_key = entry.get("citation_key") if isinstance(entry.get("citation_key"), str) else None
        if not args.force and _is_cited(topic_dir, citation_key):
            print(
                f"[ERROR] {doi} (key={citation_key}) is cited in review.md. "
                "Remove the citation first, or pass --force."
            )
            raise SystemExit(1)

        refs.delete(store, doi)
        refs.delete_entry(topic_dir, doi)
        refs.save(topic_dir, store)
        _append_log(topic_dir, doi, citation_key)
        print(f"[OK] deleted: {doi}")


if __name__ == "__main__":
    main()
