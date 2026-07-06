from __future__ import annotations

import argparse
import collections
import functools
import pathlib
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib import apis, testflight
import refs

OPENALEX_BASE = "https://api.openalex.org"

# Process-local LRU caches: when --all-gaps runs multiple gaps in one
# Python process, seed lookups and citing pages overlap heavily across
# gaps (e.g. shared bariatric-surgery seeds in gap-1/2/3). Disk cache in
# lib/apis.py already deduplicates network calls, but disk read + JSON
# parse + throttle wait still costs ms per call; in-memory caches drop
# that to nanoseconds for repeat hits in the same process.
_CACHE_LOCK = threading.Lock()


def _as_dict(value: object) -> dict[str, object] | None:
    return value if isinstance(value, dict) else None


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _clean_doi(value: str | None) -> str | None:
    if value is None:
        return None
    return value.replace("https://doi.org/", "").strip().lower() or None


def _format_author(display_name: str) -> str:
    parts = [part for part in display_name.split() if part]
    if not parts:
        return ""
    family = parts[-1]
    initials = [part[0].upper() for part in parts[:-1] if part]
    if initials:
        return f"{family}, {'. '.join(initials)}."
    return family


@functools.lru_cache(maxsize=4096)
def work_by_doi(doi: str) -> dict[str, object] | None:
    return apis.get_json(f"{OPENALEX_BASE}/works/doi:{doi}")


@functools.lru_cache(maxsize=4096)
def work_by_id(work_id: str) -> dict[str, object] | None:
    return apis.get_json(f"{OPENALEX_BASE}/works/{work_id.rsplit('/', 1)[-1]}")


@functools.lru_cache(maxsize=2048)
def _citing_cached(work_id: str, max_pages: int) -> tuple[dict[str, object], ...]:
    """Tuple-returning inner cache (lists are unhashable for lru_cache);
    callers wrap with citing() to convert back to list."""
    short_id = work_id.rsplit("/", 1)[-1]
    cursor = "*"
    results: list[dict[str, object]] = []
    for _ in range(max_pages):
        payload = apis.get_json(
            f"{OPENALEX_BASE}/works",
            params={
                "filter": f"cites:{short_id}",
                "per-page": 200,
                "cursor": cursor,
            },
        )
        if payload is None:
            break
        for item in _as_list(payload.get("results")):
            if isinstance(item, dict):
                results.append(item)
        meta = _as_dict(payload.get("meta"))
        next_cursor = _as_str((meta or {}).get("next_cursor"))
        if not next_cursor:
            break
        cursor = next_cursor
    return tuple(results)


def citing(work_id: str, max_pages: int = 3) -> list[dict[str, object]]:
    return list(_citing_cached(work_id, max_pages))


def to_entry(
    work: dict[str, object],
    source: str,
    round_number: int,
    overlap: int,
    gap: str | None = None,
) -> refs.Entry | None:
    doi = _clean_doi(_as_str(work.get("doi")))
    if doi is None:
        return None

    authors: list[str] = []
    for authorship_obj in _as_list(work.get("authorships")):
        authorship = _as_dict(authorship_obj)
        author = _as_dict((authorship or {}).get("author"))
        display_name = _as_str((author or {}).get("display_name"))
        if display_name:
            formatted = _format_author(display_name)
            if formatted:
                authors.append(formatted)

    primary_location = _as_dict(work.get("primary_location"))
    source_obj = _as_dict((primary_location or {}).get("source"))
    return {
        "doi": doi,
        "title": _as_str(work.get("title")) or "",
        "year": _as_int(work.get("publication_year")) or 0,
        "authors": authors,
        "journal": _as_str((source_obj or {}).get("display_name")) or "",
        "source": source,
        "added_round": round_number,
        "overlap": overlap,
        "gap": gap,
    }


def _chain(topic_dir: pathlib.Path, parallel: int = 4) -> None:
    """Pipelined verify → fetch: verify.py --chain-fetch dispatches a fetch
    on each DOI as soon as its verify completes. verify and fetch hit
    different API surfaces (CrossRef / Retraction Watch vs EuPMC /
    Unpaywall) so there is no rate-limit contention; the only shared
    endpoint (EuPMC /search) is cached by lib/apis.py, so the second
    caller takes the cached result rather than a new request."""
    here = pathlib.Path(__file__).parent
    verify_cmd = [
        sys.executable,
        str(here / "verify.py"),
        str(topic_dir),
        "--parallel",
        str(parallel),
        "--chain-fetch",
        "--fetch-include",
        "abstract",
    ]
    print(f"\n>> chaining verify.py --parallel {parallel} --chain-fetch ...")
    result = subprocess.run(verify_cmd, check=False)
    if result.returncode not in {0, 1}:
        print(f"[WARN] verify.py returned {result.returncode}")
    print("\n>> chaining extract_pdf.py ...")
    subprocess.run(
        [sys.executable, str(here / "extract_pdf.py"), str(topic_dir)],
        check=False,
    )


# Seed selection priority: high-tier evidence first, then recent.
# Lower number = higher priority. Within same tier, more recent year wins,
# then DOI for stable tie-break.
_SEED_TIER_PRIORITY: dict[str, int] = {
    "systematic-review": 0,
    "meta-analysis": 0,
    "rct": 1,
    "clinical-trial": 1,
    "cohort": 2,
    "observational": 3,
    "review": 4,
    "case-series": 5,
    "case-report": 6,
    "book-chapter": 7,
    "other": 8,
}


def _seed_priority_key(entry: refs.Entry) -> tuple[int, int, str]:
    study_type_str = (entry.get("study_type") or "other").lower()
    tier = _SEED_TIER_PRIORITY.get(study_type_str, 9)
    year_int = entry.get("year") or 0
    if not isinstance(year_int, int):
        year_int = 0
    # Negate year so newer wins inside tier; DOI is final tie-break.
    return (tier, -year_int, entry.get("doi") or "")


def _collect_candidates_for_gap(
    store: refs.Store,
    declared_gap: str | None,
    min_overlap: int,
    max_seeds: int = 0,
) -> tuple[list[tuple[str, int, str]], dict[str, dict[str, object]], int, int]:
    """Per-gap OpenAlex lookup phase: pure read, no store mutation. Safe to
    call from worker threads — work_by_doi/work_by_id/citing share a
    process-local LRU cache, so cross-gap seed overlap (e.g. shared
    bariatric papers in gap-1/2/3) skips network and disk-cache reads.

    Returns: (candidates, descendant_cache, seeds_used, seeds_eligible) — main
    thread applies them serially via refs.upsert. seeds_used is the count
    actually expanded (post-cap); seeds_eligible is the pre-cap pool size, so
    callers can render "seeds 25/60" when --max-seeds-per-gap is binding.

    When max_seeds > 0 and the gap has more verified entries than that, we
    keep only the top-`max_seeds` by evidence tier + recency. This caps the
    candidates pool: with 60 seeds × ~50 ancestors/descendants each, the
    pool can balloon to 1500+ even though `--min-overlap 2` filters most
    out. Sampling to 25 strong seeds typically halves the pool while
    keeping the high-quality genealogy edges intact."""
    eligible: list[refs.Entry] = [
        entry
        for entry in store["entries"].values()
        if entry.get("verification_status") == "verified"
        and not entry.get("retracted", False)
        and (declared_gap is None or entry.get("gap") == declared_gap)
    ]
    total_eligible = len(eligible)
    if max_seeds > 0 and total_eligible > max_seeds:
        eligible.sort(key=_seed_priority_key)
        eligible = eligible[:max_seeds]

    seed_dois = [
        doi
        for doi in (entry.get("doi") for entry in eligible)
        if isinstance(doi, str)
    ]
    if not seed_dois:
        return [], {}, 0, total_eligible

    seed_works: dict[str, dict[str, object]] = {}
    for doi in seed_dois:
        work = work_by_doi(doi)
        work_id = _as_str((work or {}).get("id"))
        if work is not None and work_id:
            seed_works[doi] = work

    seed_ids = {
        work_id
        for work_id in (_as_str(work.get("id")) for work in seed_works.values())
        if work_id
    }

    ancestors: collections.Counter[str] = collections.Counter()
    for work in seed_works.values():
        for referenced in _as_list(work.get("referenced_works")):
            referenced_id = _as_str(referenced)
            if referenced_id and referenced_id not in seed_ids:
                ancestors[referenced_id] += 1

    descendants: collections.Counter[str] = collections.Counter()
    descendant_cache: dict[str, dict[str, object]] = {}
    for work in seed_works.values():
        work_id = _as_str(work.get("id"))
        if not work_id:
            continue
        for citing_work in citing(work_id):
            citing_id = _as_str(citing_work.get("id"))
            if not citing_id or citing_id in seed_ids:
                continue
            descendants[citing_id] += 1
            descendant_cache[citing_id] = citing_work

    candidates: list[tuple[str, int, str]] = []
    for work_id, overlap in ancestors.items():
        if overlap >= min_overlap:
            candidates.append((work_id, overlap, "genealogy_ancestor"))
    for work_id, overlap in descendants.items():
        if overlap >= min_overlap:
            candidates.append((work_id, overlap, "genealogy_descendant"))
    candidates.sort(key=lambda item: (-item[1], item[0]))
    return candidates, descendant_cache, len(seed_dois), total_eligible


def _has_abstract(work: dict[str, object]) -> bool:
    """True iff the OpenAlex work carries an abstract. OpenAlex stores the
    abstract as an inverted index (token -> positions); a present, non-empty
    ``abstract_inverted_index`` means the abstract is recoverable."""
    inverted = work.get("abstract_inverted_index")
    return isinstance(inverted, dict) and bool(inverted)


def _apply_candidates(
    store: refs.Store,
    candidates: list[tuple[str, int, str]],
    descendant_cache: dict[str, dict[str, object]],
    declared_gap: str | None,
    round_number: int,
    max_add: int,
    blocklist: object,
    *,
    skip_no_abstract: bool = False,
) -> tuple[int, int]:
    """Serial in-memory upsert. Caller holds _CACHE_LOCK if running
    inside --all-gaps to prevent concurrent dict mutations.

    Returns (added, skipped_excluded). Candidates whose DOI is on the sticky
    exclusion denylist (`refs.is_excluded`) are skipped and counted — they
    were deliberately rejected as noise in an earlier round, and re-adding
    them would clear the excluded flag (the bug §B fixes). When
    skip_no_abstract is set, OpenAlex works with no abstract are also dropped
    (an opt-in §E intake filter; off by default to preserve behavior)."""
    added = 0
    skipped_excluded = 0
    for work_id, overlap, source in candidates:
        if added >= max_add:
            break
        work = descendant_cache.get(work_id) or work_by_id(work_id)
        if work is None or bool(work.get("is_retracted", False)):
            continue
        if skip_no_abstract and not _has_abstract(work):
            continue
        entry = to_entry(work, source, round_number, overlap, gap=declared_gap)
        if entry is None:
            continue
        doi = entry["doi"]
        if refs.is_blocked(blocklist, doi):
            continue
        if refs.is_excluded(store, doi):
            skipped_excluded += 1
            continue
        if refs.upsert(store, entry):
            added += 1
    return added, skipped_excluded


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("topic_dir", help="Path to a topic directory under reviews/")
    parser.add_argument("--min-overlap", type=int, default=2)
    parser.add_argument(
        "--max-add",
        type=int,
        default=15,
        help=(
            "Cap genealogy additions per gap. Default tightened to 15 (was 20) "
            "to keep the funnel lean — most rounds only cite the top few "
            "genealogy edges and the long tail just pays verify/fetch cost. "
            "Raise it explicitly when a gap genuinely needs deeper coverage."
        ),
    )
    parser.add_argument(
        "--max-seeds-per-gap",
        type=int,
        default=20,
        help=(
            "Cap the number of verified entries used as genealogy seeds per "
            "gap. When the gap has more eligible seeds than this, keep the "
            "highest evidence-tier (RCT/meta > cohort > review > other) and "
            "most recent. 0 = no cap. Default tightened to 20 (was 25); with "
            "60+ seeds the candidate pool can balloon to 1000+ even after "
            "--min-overlap filtering, with most extra candidates being weak. "
            "Sampling to ~20 strong seeds keeps the high-quality genealogy "
            "edges while shrinking the pool. Raise explicitly when needed."
        ),
    )
    parser.add_argument(
        "--skip-no-abstract",
        action="store_true",
        help=(
            "Opt-in §E intake filter: drop OpenAlex candidates that carry no "
            "abstract (no abstract_inverted_index) before they enter the "
            "store. Off by default — preserves existing behavior. Useful to "
            "shrink the funnel when a gap is over-broad; abstract-less works "
            "are usually editorials / errata / indexing stubs with low "
            "synthesis value."
        ),
    )
    # TODO(predatory-journal denylist, §E.1): an ISSN-level denylist would let
    # us drop known predatory-publisher candidates at intake instead of
    # re-pruning them every round. NOT implemented here because it needs a
    # vetted data source (e.g. a curated ISSN list) — do not hard-code journal
    # names from memory (false positives are libellous and unmaintainable).
    # When a source is chosen, wire the check in _apply_candidates alongside
    # is_blocked / is_excluded.
    parser.add_argument("--round", dest="round_number", type=int)
    parser.add_argument(
        "--gap",
        metavar="GAP_ID",
        help="Restrict seeds to those matching this gap; candidates inherit the gap.",
    )
    parser.add_argument(
        "--all-gaps",
        action="store_true",
        help=(
            "Run genealogy for every pending gap in the store, in parallel. "
            "OpenAlex lookups share a process-local LRU cache across gaps "
            "(seed overlap is common). Single _chain at the end runs verify "
            "+fetch over the merged candidate set, replacing N subprocess "
            "spawns with one. Mutually exclusive with --gap."
        ),
    )
    parser.add_argument(
        "--gap-workers",
        type=int,
        default=4,
        help="Parallel workers for --all-gaps mode (one gap per worker).",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=4,
        help="Parallel workers for verify/fetch chain.",
    )
    parser.add_argument("--no-verify", action="store_true")
    args = parser.parse_args()

    if args.all_gaps and args.gap is not None:
        print("[ERROR] --all-gaps and --gap are mutually exclusive")
        raise SystemExit(2)

    topic_dir = pathlib.Path(args.topic_dir)
    store = refs.load(topic_dir)
    if store is None:
        print(f"[ERROR] missing references store: {topic_dir}")
        raise SystemExit(1)

    with testflight.timer(
        "genealogy",
        "main",
        topic_dir=topic_dir,
        gap=args.gap,
        all_gaps=args.all_gaps,
        min_overlap=args.min_overlap,
        max_add=args.max_add,
        parallel=args.parallel,
    ) as detail:
        blocklist = refs.load_blocklist()
        round_number = args.round_number or (store.get("rounds", 0) + 1)
        declared_gap = args.gap
        if declared_gap is not None and declared_gap not in store.get("gaps", {}):
            print(f"[ERROR] gap {declared_gap!r} not declared in store")
            raise SystemExit(2)

        if args.all_gaps:
            gap_ids = list(store.get("gaps", {}).keys())
            if not gap_ids:
                print("[ERROR] no gaps declared in store")
                raise SystemExit(2)

            # Phase 1: per-gap OpenAlex lookups, parallel across gaps.
            # work_by_doi / work_by_id / citing share a thread-safe LRU
            # cache, so seeds shared between gaps cost ~0 on the second
            # gap to look them up.
            results: dict[str, tuple[list[tuple[str, int, str]], dict[str, dict[str, object]], int, int]] = {}
            workers = max(1, min(args.gap_workers, len(gap_ids)))
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="gap"
            ) as pool:
                futures = {
                    pool.submit(
                        _collect_candidates_for_gap,
                        store,
                        gid,
                        args.min_overlap,
                        args.max_seeds_per_gap,
                    ): gid
                    for gid in gap_ids
                }
                for fut in as_completed(futures):
                    gid = futures[fut]
                    results[gid] = fut.result()

            # Phase 2: serial in-memory upsert (avoids dict races).
            total_added = 0
            total_seeds = 0
            total_candidates = 0
            total_skipped_excluded = 0
            for gid in gap_ids:
                candidates, descendant_cache, seeds_used, seeds_eligible = results.get(
                    gid, ([], {}, 0, 0)
                )
                if seeds_used == 0:
                    print(f"[skip] gap {gid}: no verified seeds")
                    continue
                added, skipped_excluded = _apply_candidates(
                    store,
                    candidates,
                    descendant_cache,
                    gid,
                    round_number,
                    args.max_add,
                    blocklist,
                    skip_no_abstract=args.skip_no_abstract,
                )
                seed_label = (
                    f"{seeds_used}/{seeds_eligible}"
                    if seeds_used != seeds_eligible
                    else str(seeds_used)
                )
                print(
                    f"gap {gid}: +{added} (seeds {seed_label}, "
                    f"candidates pool {len(candidates)})"
                )
                total_added += added
                total_seeds += seeds_used
                total_candidates += len(candidates)
                total_skipped_excluded += skipped_excluded

            store["rounds"] = round_number
            refs.save(topic_dir, store)
            print(
                f"round {round_number}: +{total_added} "
                f"across {len(gap_ids)} gaps (candidates pool {total_candidates})"
            )
            if total_skipped_excluded:
                print(
                    f"skipped {total_skipped_excluded} previously-excluded",
                    file=sys.stderr,
                )
            detail.update(
                {
                    "round": round_number,
                    "gaps": len(gap_ids),
                    "seeds": total_seeds,
                    "candidates": total_candidates,
                    "added": total_added,
                    "skipped_excluded": total_skipped_excluded,
                }
            )

            if total_added > 0 and not args.no_verify:
                _chain(topic_dir, parallel=args.parallel)
            return

        # Single-gap mode: original behavior.
        candidates, descendant_cache, seeds_used, seeds_eligible = _collect_candidates_for_gap(
            store, declared_gap, args.min_overlap, args.max_seeds_per_gap
        )
        if seeds_used == 0:
            print("[ERROR] no verified seeds; run verify.py first")
            raise SystemExit(2)

        added, skipped_excluded = _apply_candidates(
            store,
            candidates,
            descendant_cache,
            declared_gap,
            round_number,
            args.max_add,
            blocklist,
            skip_no_abstract=args.skip_no_abstract,
        )

        store["rounds"] = round_number
        refs.save(topic_dir, store)
        seed_label = (
            f"{seeds_used}/{seeds_eligible}"
            if seeds_used != seeds_eligible
            else str(seeds_used)
        )
        print(
            f"round {round_number}: +{added} "
            f"(seeds {seed_label}, candidates pool {len(candidates)})"
        )
        if skipped_excluded:
            print(
                f"skipped {skipped_excluded} previously-excluded",
                file=sys.stderr,
            )
        detail.update(
            {
                "round": round_number,
                "seeds": seeds_used,
                "seeds_eligible": seeds_eligible,
                "candidates": len(candidates),
                "added": added,
                "skipped_excluded": skipped_excluded,
            }
        )

        if added > 0 and not args.no_verify:
            _chain(topic_dir, parallel=args.parallel)


if __name__ == "__main__":
    main()
