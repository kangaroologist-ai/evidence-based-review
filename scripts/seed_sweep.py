"""Resolve a batch of paper *titles* to DOIs (Semantic Scholar fuzzy match) and
seed them into a topic's store via `verify.py --add --source seed`.

The recurring chore this fixes: you have a handful of known-by-title papers for
one gap (from a reading list, a reviewer's "you missed X", a textbook's
bibliography) and want them in the store without hand-looking-up each DOI. This
reads one title per line, runs each through Semantic Scholar's
``/paper/search/match`` endpoint (the same path as `search.py --match`), and for
every hit shells out to `verify.py --add` so the full add pipeline runs
unchanged: CrossRef title cross-check, retraction-watch, study-type
classification, audit trail. Titles that don't resolve to a DOI are listed at
the end for manual follow-up.

`verify.py` is invoked with the title/year/authors *as Semantic Scholar
resolved them* (not your input title), so its title-mismatch guard sees
consistent metadata. Your input title is only the search query.

Usage:
    python tools/seed_sweep.py reviews/<topic> --gap gap-2 --titles titles.txt

titles.txt: one paper title per line; blank lines and `#` comments ignored.
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib import testflight
import refs
import search


def _read_titles(titles_file: pathlib.Path) -> list[str]:
    """One title per line; ignore blanks and `#` comments. Order preserved."""
    titles: list[str] = []
    for raw in titles_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        titles.append(line)
    return titles


def _resolve_title(title: str) -> search.SearchHit | None:
    """Best Semantic Scholar match for a free-text title, or None.

    /paper/search/match returns its single best match first; we take hit[0]
    when present (it already requires a DOI + title via
    search._semantic_hit_from_item)."""
    hits = search._match_semantic_scholar(title)
    return hits[0] if hits else None


def _seed_one(
    topic_dir: pathlib.Path,
    hit: search.SearchHit,
    *,
    gap_id: str,
    round_number: int,
) -> int:
    """Shell out to verify.py --add for one resolved hit. Returns its exit code.

    Subprocess (not an in-process call) so the entire verify add pipeline —
    CrossRef cross-check, retraction watch, study-type inference, force-mismatch
    audit — runs exactly as a hand `verify.py --add` would, with no duplicated
    logic here."""
    authors = "; ".join(hit.authors)
    cmd = [
        sys.executable,
        str(pathlib.Path(__file__).parent / "verify.py"),
        str(topic_dir),
        "--add",
        hit.doi,
        hit.title,
        str(hit.year),
        authors,
        "--source",
        "seed",
        "--gap",
        gap_id,
        "--round",
        str(round_number),
    ]
    completed = subprocess.run(cmd)
    return completed.returncode


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Resolve a list of paper titles to DOIs via Semantic Scholar match "
            "and seed them with verify.py --add --source seed. Reports titles "
            "that could not be resolved."
        )
    )
    parser.add_argument("topic_dir", help="Path to a topic directory under reviews/")
    parser.add_argument("--gap", required=True, help="Gap id to tag seeded entries with (e.g. gap-2).")
    parser.add_argument(
        "--titles",
        required=True,
        help="File with one paper title per line (# comments / blanks ignored).",
    )
    parser.add_argument("--round", dest="round_number", type=int, default=1)
    args = parser.parse_args()

    topic_dir = pathlib.Path(args.topic_dir)
    with testflight.timer(
        "seed_sweep", "main", topic_dir=topic_dir, gap=args.gap
    ) as detail:
        titles_file = pathlib.Path(args.titles)
        if not titles_file.exists():
            print(f"[ERROR] titles file not found: {titles_file}", file=sys.stderr)
            raise SystemExit(1)

        store = refs.load(topic_dir)
        if store is None:
            print(f"[ERROR] missing references store: {topic_dir}", file=sys.stderr)
            raise SystemExit(1)

        # Fail fast on an undeclared gap before any API calls (mirrors
        # search.py's --auto-add guard) — otherwise verify.py --add would
        # attach entries to a phantom gap that lint_review later flags.
        if args.gap not in store.get("gaps", {}):
            print(
                f"[ERROR] gap '{args.gap}' not declared in {topic_dir} — run "
                f"verify.py --declare-gap {args.gap} '<description>' first.",
                file=sys.stderr,
            )
            raise SystemExit(1)

        titles = _read_titles(titles_file)
        if not titles:
            print(f"[ERROR] titles file is empty: {titles_file}", file=sys.stderr)
            raise SystemExit(2)

        unresolved: list[str] = []
        add_failures: list[tuple[str, str, int]] = []
        seeded = 0
        for title in titles:
            hit = _resolve_title(title)
            if hit is None:
                print(f"[UNRESOLVED] {title}")
                unresolved.append(title)
                continue
            print(f"[RESOLVED] {title}\n    -> {hit.doi} ({hit.year}) {hit.title[:70]}")
            code = _seed_one(
                topic_dir,
                hit,
                gap_id=args.gap,
                round_number=args.round_number,
            )
            if code == 0:
                seeded += 1
            else:
                add_failures.append((title, hit.doi, code))

        detail.update(
            {
                "titles": len(titles),
                "seeded": seeded,
                "unresolved": len(unresolved),
                "add_failed": len(add_failures),
            }
        )

        print(
            f"\nsummary: {len(titles)} titles, {seeded} seeded, "
            f"{len(unresolved)} unresolved, {len(add_failures)} add-failed"
        )
        if unresolved:
            print("unresolved titles (no Semantic Scholar DOI match):")
            for title in unresolved:
                print(f"  {title}")
        if add_failures:
            print("verify.py --add nonzero exit (likely title_mismatch / failed):")
            for title, doi, code in add_failures:
                print(f"  exit={code} {doi}  <- {title}")

        # Nonzero overall only if every title failed to seed and we had work to
        # do — partial success stays exit 0 so a sweep over a mixed list isn't
        # treated as a hard failure by callers / hooks.
        if seeded == 0 and titles:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
