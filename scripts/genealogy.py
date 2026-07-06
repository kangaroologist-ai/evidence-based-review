from __future__ import annotations

import argparse
import collections
import functools
import os
import pathlib
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib import apis, cli_runtime, config, quarantine, testflight
import fetch
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
    # ⚠ Force the chained children onto the LOCAL path. genealogy itself may be
    # running inside the daemon (dispatched as a "genealogy" command), which
    # holds _DISPATCH_LOCK for the whole call — including this subprocess.run.
    # If the child verify.py inherited HEALTH_REVIEW_DAEMON=1 it would connect
    # BACK to the same daemon, whose handler thread blocks on the held
    # _DISPATCH_LOCK → the nested request never returns → this subprocess.run
    # (no timeout) waits forever → permanent hang. Pinning the children to
    # DAEMON=0 breaks the re-entry; they still get E1 throttling locally.
    child_env = {**os.environ, "HEALTH_REVIEW_DAEMON": "0"}
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
    result = subprocess.run(verify_cmd, check=False, env=child_env)
    if result.returncode not in {0, 1}:
        print(f"[WARN] verify.py returned {result.returncode}")
    print("\n>> chaining extract_pdf.py ...")
    subprocess.run(
        [sys.executable, str(here / "extract_pdf.py"), str(topic_dir)],
        check=False,
        env=child_env,
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

    # Thin-seed guard: with ≤2 resolved seeds, overlap≥2 is essentially
    # unreachable (a candidate would have to be linked to BOTH seeds), so a
    # gap with only 1–2 seeds silently adds nothing under the default
    # --min-overlap 2. Auto-relax to overlap≥1 and tell the caller — this is
    # exactly the hand-lowering a human does for a thin gap (cowork testflight #6).
    effective_min_overlap = min_overlap
    if min_overlap > 1 and len(seed_works) <= 2:
        effective_min_overlap = 1
        print(
            f"[hint] only {len(seed_works)} seed(s) resolved in OpenAlex for "
            f"{declared_gap or 'this gap'}; lowering min_overlap "
            f"{min_overlap}→{effective_min_overlap} "
            f"(overlap≥{min_overlap} is unreachable with ≤2 seeds)",
            file=sys.stderr,
        )

    candidates: list[tuple[str, int, str]] = []
    for work_id, overlap in ancestors.items():
        if overlap >= effective_min_overlap:
            candidates.append((work_id, overlap, "genealogy_ancestor"))
    for work_id, overlap in descendants.items():
        if overlap >= effective_min_overlap:
            candidates.append((work_id, overlap, "genealogy_descendant"))
    candidates.sort(key=lambda item: (-item[1], item[0]))
    return candidates, descendant_cache, len(seed_dois), total_eligible


def _work_relevance_text(work: dict[str, object]) -> str:
    """Lowercased title + abstract tokens (from the OpenAlex inverted index) —
    the cheap, already-fetched signal the prune-early gate matches against."""
    title = _as_str(work.get("title")) or ""
    inverted = work.get("abstract_inverted_index")
    tokens = " ".join(inverted.keys()) if isinstance(inverted, dict) else ""
    return f"{title} {tokens}".lower()


def _is_relevant_text(text: str, terms: list[str] | None) -> bool:
    """Core relevance predicate: relevant unless ZERO of ``terms`` appears in
    ``text`` (case-insensitive substring). Shared by the title-only gate
    (`_is_relevant`) and the abstract re-judgment path (spec §0.4②)."""
    if not terms:
        return True
    low = text.lower()
    return any(term.lower() in low for term in terms if term)


def _is_relevant(work: dict[str, object], terms: list[str] | None) -> bool:
    """plan v3 §4.2 C2 prune-early: a candidate is relevant unless it shares
    ZERO keywords with the gap/topic term set. Recall-safe by design — only
    obviously cross-domain candidates (no overlap at all) are dropped before
    the expensive verify/fetch/notes rungs; anything with a single term match
    passes. No terms → gate off (relevant)."""
    if not terms:
        return True
    return _is_relevant_text(_work_relevance_text(work), terms)


def _has_abstract(work: dict[str, object]) -> bool:
    """True iff the OpenAlex work carries an abstract. OpenAlex stores the
    abstract as an inverted index (token -> positions); a present, non-empty
    ``abstract_inverted_index`` means the abstract is recoverable."""
    inverted = work.get("abstract_inverted_index")
    return isinstance(inverted, dict) and bool(inverted)


def _fetch_abstract_for_rejudge(doi: str | None) -> str:
    """Spec §0.4②: a genealogy candidate with NO OpenAlex abstract was judged on
    its TITLE alone — that is not a confident reject. Resolve its abstract the
    way fetch.py does (EuPMC core search → CrossRef fallback) so the relevance
    gate can re-judge against real text instead of dropping it title-blind.

    Returns the abstract text, or "" when the DOI is missing / no abstract is
    retrievable from either source. Network-backed, but EuPMC /search is cached
    by lib/apis.py, so the abstract this warms is reused for free if the
    candidate then chains through verify.py --chain-fetch. Kept as a single
    module-level function so tests can monkeypatch it (no network in unit
    tests) — it is the only seam the re-judge path touches the network through."""
    if not doi:
        return ""
    hit = fetch.eupmc_search(doi)
    text = _as_str((hit or {}).get("abstractText")) or ""
    if not text:
        # EuPMC has nothing → CrossRef stores a JATS-stripped abstract for many
        # works EuPMC misses (older / non-PMC journals — exactly the no-abstract
        # genealogy candidates that reach this branch).
        text = fetch.crossref_abstract(doi)
    return text


def _offtopic_record(
    work: dict[str, object],
    work_id: str,
    gap: str | None,
    round_number: int,
    source: str,
    *,
    uncertain: bool,
    reason: str,
) -> dict[str, object]:
    """One meta/quarantine.jsonl record for an off-topic genealogy reject.
    Stable schema (``{openalex_id, title, doi, reason, uncertain, gap, round,
    source}``) — recall_audit reads ``doi``, round_gate reads existence."""
    return {
        "openalex_id": work_id,
        "title": _as_str(work.get("title")) or "",
        "doi": _as_str(work.get("doi")),
        "reason": reason,
        "uncertain": uncertain,
        "gap": gap,
        "round": round_number,
        "source": source,
    }


def _parse_relevance_terms(raw: str | None) -> list[str] | None:
    """Parse a comma-separated relevance_terms string into a term list.

    Mirrors the CLI --relevance-terms parsing incl. the no-comma footgun
    fallback (a lone term with internal whitespace was almost certainly meant
    as separate keywords). Lets callers pull a gap's stored spec-N2
    relevance_terms when the CLI flag is absent.
    """
    if not raw:
        return None
    terms = [t.strip() for t in raw.split(",") if t.strip()]
    if len(terms) == 1 and "," not in raw and len(terms[0].split()) > 1:
        terms = terms[0].split()
    return terms or None


def _stored_relevance_terms(store, gid: str) -> list[str] | None:
    """A gap's declared relevance_terms (spec N2), parsed for the C2 gate.

    --all-gaps can't carry per-gap terms on the CLI, so without this the C2
    relevance gate / quarantine pool never runs in --all-gaps mode even though
    each gap's terms are sitting in the store (round_gate then blocks on a
    missing meta/quarantine.jsonl).
    """
    return _parse_relevance_terms(
        (store.get("gaps", {}).get(gid) or {}).get("relevance_terms")
    )


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
    relevance_terms: list[str] | None = None,
    topic_dir: pathlib.Path | None = None,
    rejudge_uncertain: bool = True,
) -> tuple[int, int, int]:
    """Serial in-memory upsert. Caller holds _CACHE_LOCK if running
    inside --all-gaps to prevent concurrent dict mutations.

    Returns (added, skipped_excluded, eligible_not_added). The third value
    (C-F6b) is how many MORE candidates are relevance-eligible BEYOND the cap —
    a bounded look-ahead so the caller can tell a throttled round (real
    candidates remained) from one the cap merely coincided with exhausting. The
    old cap_hit used raw ``len(candidates) > added``, which a dense cross-domain
    neighbourhood (700 candidates, 690 quarantined, 3 added) made permanently
    true → false throttle → never saturates. Candidates whose DOI is on the sticky
    exclusion denylist (`refs.is_excluded`) are skipped and counted — they
    were deliberately rejected as noise in an earlier round, and re-adding
    them would clear the excluded flag (the bug §B fixes). When
    skip_no_abstract is set, OpenAlex works with no abstract are also dropped
    (an opt-in §E intake filter; off by default to preserve behavior).

    rejudge_uncertain (spec §0.4②, default on): when the relevance gate would
    drop a candidate that has NO OpenAlex abstract — i.e. it was judged on its
    TITLE alone, not a confident reject — actually fetch its abstract
    (EuPMC/CrossRef) and re-run the relevance test. It is added if the abstract
    now overlaps, recorded as a *confident* reject (uncertain=False) if it
    still has 0 overlap with the abstract in hand, and only kept uncertain=True
    if no abstract could be retrieved at all. Set False (CLI
    --no-rejudge-uncertain) to fall back to the old title-only behavior."""
    added = 0
    skipped_excluded = 0
    skipped_offtopic = 0
    resurrected = 0
    eligible_not_added = 0  # C-F6b: relevance-eligible candidates cut by the cap
    scanned_past_cap = 0
    # candidates are overlap-sorted (best first), so the eligible ones cluster right
    # after the cap; a bounded window keeps a dense off-topic tail from forcing many
    # network work_by_id lookups (ancestors aren't in descendant_cache).
    cap_lookahead = max(20, max_add * 3) if max_add else 0
    quarantined: list[dict[str, object]] = []
    for work_id, overlap, source in candidates:
        if max_add and added >= max_add:
            # C-F6b: past the cap, only COUNT further relevance-eligible candidates
            # (no upsert / quarantine / rejudge-fetch). Stop at the first eligible
            # (>0 already settles cap_hit) or after the off-topic look-ahead window.
            if eligible_not_added > 0 or scanned_past_cap >= cap_lookahead:
                break
            scanned_past_cap += 1
            work = descendant_cache.get(work_id) or work_by_id(work_id)
            if work is None or bool(work.get("is_retracted", False)):
                continue
            if skip_no_abstract and not _has_abstract(work):
                continue
            if relevance_terms and not _is_relevant(work, relevance_terms):
                continue  # off-topic by title/abstract (no rejudge past the cap)
            entry = to_entry(work, source, round_number, overlap, gap=declared_gap)
            if entry is None:
                continue
            doi = entry["doi"]
            if refs.is_blocked(blocklist, doi) or refs.is_excluded(store, doi):
                continue
            eligible_not_added += 1
            continue
        work = descendant_cache.get(work_id) or work_by_id(work_id)
        if work is None or bool(work.get("is_retracted", False)):
            continue
        if skip_no_abstract and not _has_abstract(work):
            continue
        # prune-early (C2): drop clearly cross-domain candidates BEFORE the
        # expensive verify/fetch/notes rungs they would otherwise pay for.
        # R2: dropped ≠ discarded — record each in the quarantine pool so the
        # recall loss is auditable / reversible (spec §0.4).
        if relevance_terms and not _is_relevant(work, relevance_terms):
            has_abs = _has_abstract(work)
            if has_abs:
                # title + abstract both 0-overlap → confident cross-domain reject.
                quarantined.append(_offtopic_record(
                    work, work_id, declared_gap, round_number, source,
                    uncertain=False,
                    reason="relevance-gate: 0 keyword overlap (title+abstract)",
                ))
                skipped_offtopic += 1
                continue
            # R3-F12 (spec §0.4②: 标题不确定者不在 title 层判死): NO OpenAlex abstract →
            # the 0-overlap verdict rests on the TITLE alone, which is NOT a confident
            # reject. Actually re-judge: fetch the abstract and re-test relevance, so
            # the §0.4② requirement is *mechanically* met rather than label-only.
            if rejudge_uncertain:
                abstract_text = _fetch_abstract_for_rejudge(
                    _clean_doi(_as_str(work.get("doi")))
                )
                if abstract_text:
                    combined = f"{_work_relevance_text(work)} {abstract_text}"
                    if not _is_relevant_text(combined, relevance_terms):
                        # abstract now in hand, STILL 0 overlap → confident reject.
                        quarantined.append(_offtopic_record(
                            work, work_id, declared_gap, round_number, source,
                            uncertain=False,
                            reason="relevance-gate: 0 keyword overlap (title + "
                                   "re-fetched abstract, re-judged · spec §0.4②)",
                        ))
                        skipped_offtopic += 1
                        continue
                    # now overlaps → wrongly title-dropped; resurrect it (fall
                    # through to the normal verify/fetch/notes path below).
                    resurrected += 1
                else:
                    # no abstract retrievable from OpenAlex / EuPMC / CrossRef →
                    # genuinely cannot re-judge; keep uncertain=True so the recall
                    # audit / a later pass can still revisit it.
                    quarantined.append(_offtopic_record(
                        work, work_id, declared_gap, round_number, source,
                        uncertain=True,
                        reason="relevance-gate UNCERTAIN: no abstract (OpenAlex / "
                               "EuPMC / CrossRef), 0 title overlap — could not "
                               "re-judge (spec §0.4②)",
                    ))
                    skipped_offtopic += 1
                    continue
            else:
                # re-judge disabled (--no-rejudge-uncertain): old title-only behavior.
                quarantined.append(_offtopic_record(
                    work, work_id, declared_gap, round_number, source,
                    uncertain=True,
                    reason="relevance-gate UNCERTAIN: no abstract, 0 title overlap — "
                           "re-judge disabled (--no-rejudge-uncertain · spec §0.4②)",
                ))
                skipped_offtopic += 1
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
    if topic_dir is not None and relevance_terms:
        # C2 ran → ensure the pool file exists even if nothing was rejected, so
        # round_gate can require it (absence = C2 not run).
        quarantine.ensure(topic_dir)
        if quarantined:
            quarantine.append(topic_dir, quarantined)
    if skipped_offtopic and relevance_terms:
        print(
            f"[relevance-gate] dropped {skipped_offtopic} off-topic candidate(s) "
            f"(0 keyword overlap with {len(relevance_terms)} term(s)) before fetch"
            + (f" → quarantined to {quarantine.path(topic_dir).name}" if topic_dir else ""),
            file=sys.stderr,
        )
    if resurrected and relevance_terms:
        print(
            f"[relevance-gate] re-judged {resurrected} title-uncertain candidate(s) "
            f"with a fetched abstract → now relevant, kept (spec §0.4②)",
            file=sys.stderr,
        )
    return added, skipped_excluded, eligible_not_added


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
    parser.add_argument(
        "--cap-auto",
        action="store_true",
        help=(
            "Compute --max-add from round + current eligible-pool size "
            "(config.auto_genealogy_cap, W2/C4): early rounds stay wide, "
            "confirmation rounds tighten to ~eligible/10 with a floor. "
            "Overrides --max-add. Lets the round workflow stop hard-coding cap."
        ),
    )
    parser.add_argument(
        "--relevance-terms",
        default=None,
        help=(
            "Comma-separated keyword set for the prune-early relevance gate (C2). "
            "When given, a genealogy candidate is dropped BEFORE verify/fetch/notes "
            "iff its title+abstract shares ZERO of these terms (clearly cross-domain). "
            "Recall-safe (any single match passes); off by default. Supply curated "
            "domain terms (English, matching the OpenAlex corpus) — the round-loop "
            "operator derives these from the gap; do NOT pass raw CJK gap prose."
        ),
    )
    parser.add_argument(
        "--no-rejudge-uncertain",
        dest="rejudge_uncertain",
        action="store_false",
        help=(
            "Disable spec §0.4② abstract re-judgment. By default, when the C2 "
            "relevance gate (--relevance-terms) would drop a candidate that has "
            "NO OpenAlex abstract — judged on title alone — its abstract is "
            "fetched (EuPMC/CrossRef) and relevance re-tested, so a title-blind "
            "drop is never a confident reject. Pass this to fall back to the old "
            "title-only behavior (avoids the extra per-uncertain-candidate fetch)."
        ),
    )
    parser.set_defaults(rejudge_uncertain=True)
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

    relevance_terms = None
    if args.relevance_terms:
        relevance_terms = [t.strip() for t in args.relevance_terms.split(",") if t.strip()]
        # F1b footgun guard: a space-separated string with NO commas parses as one
        # giant term → no candidate's title+abstract ever contains the whole phrase
        # → silent 100% drop (added=0). If there are no commas but the lone term
        # has internal whitespace, fall back to whitespace tokenisation and warn —
        # the caller almost certainly meant separate keywords (e.g. passing a
        # space-separated search `query` straight through). Comma input is left
        # intact so legitimate multi-word phrases still work.
        if (
            len(relevance_terms) == 1
            and "," not in args.relevance_terms
            and len(relevance_terms[0].split()) > 1
        ):
            relevance_terms = relevance_terms[0].split()
            print(
                f"[WARN] --relevance-terms had no commas; split into "
                f"{len(relevance_terms)} whitespace tokens. Use commas to group "
                f"multi-word phrases.",
                file=sys.stderr,
            )
        relevance_terms = relevance_terms or None

    topic_dir = pathlib.Path(args.topic_dir)
    store = refs.load(topic_dir)
    if store is None:
        print(f"[ERROR] missing references store: {topic_dir}")
        raise SystemExit(1)

    # W2/C4: when --cap-auto, derive the per-gap cap from round + eligible pool
    # so the round workflow no longer hard-codes `g.cap || 15`. Computed once
    # against the store's current eligible count (the saturation-ratio basis).
    # MUST come after refs.load (store) — an earlier placement read `store`
    # before it existed (UnboundLocalError, caught only by an end-to-end run).
    effective_max_add = args.max_add
    if args.cap_auto:
        eligible_count = sum(
            1
            for e in store["entries"].values()
            if e.get("verification_status") == "verified"
            and not e.get("retracted")
            and not e.get("excluded_reason")
        )
        cap_round = args.round_number or (int(store.get("rounds", 0) or 0) + 1)
        # R5: divide the confirmation cap by the gaps expanding this round so the
        # round TOTAL (Σ gaps) stays ≈ eligible/10 → C3 saturation reachable.
        num_gaps = len(store.get("gaps", {})) if args.all_gaps else 1
        effective_max_add = config.auto_genealogy_cap(cap_round, eligible_count, num_gaps)
        print(
            f"[cap-auto] round={cap_round} eligible={eligible_count} "
            f"gaps={num_gaps} → per-gap max-add={effective_max_add}",
            file=sys.stderr,
        )

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
        # Use latest_round() (max of rounds-hint / added_round / created_round)
        # rather than the lagging store['rounds'] hint: a round that added
        # evidence via verify/search but not genealogy leaves store['rounds']
        # behind, which would mis-stamp added_round and mis-key the per-round
        # gates (see refs.latest_round docstring).
        round_number = args.round_number or (refs.latest_round(store) + 1)
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
            round_cap_hit = False  # F6: did any gap stop because of the cap?
            for gid in gap_ids:
                candidates, descendant_cache, seeds_used, seeds_eligible = results.get(
                    gid, ([], {}, 0, 0)
                )
                if seeds_used == 0:
                    print(f"[skip] gap {gid}: no verified seeds")
                    continue
                added, skipped_excluded, eligible_not_added = _apply_candidates(
                    store,
                    candidates,
                    descendant_cache,
                    gid,
                    round_number,
                    effective_max_add,
                    blocklist,
                    skip_no_abstract=args.skip_no_abstract,
                    # C2: fall back to the gap's stored relevance_terms — a single
                    # CLI --relevance-terms can't vary per gap across --all-gaps.
                    relevance_terms=relevance_terms or _stored_relevance_terms(store, gid),
                    topic_dir=topic_dir,
                    rejudge_uncertain=args.rejudge_uncertain,
                )
                # F6 (testflight #2) + C-F6b: a gap throttled by the cap (added the cap AND
                # relevance-ELIGIBLE candidates remained) was not exhausted — term_check
                # must not let it confirm saturation. Gate on eligible_not_added, NOT raw
                # len(candidates): a dense cross-domain pool's off-topic tail no longer
                # fakes a throttle (which would make the topic never saturate).
                if effective_max_add and added >= effective_max_add and eligible_not_added > 0:
                    round_cap_hit = True
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
            # F6: persist this round's cap-hit so term_check can distinguish a
            # throttled low-growth round from a genuinely exhausted one.
            round_meta = store.setdefault("round_meta", {})
            round_meta[str(round_number)] = {
                "genealogy_cap_hit": round_cap_hit,
                "effective_max_add": effective_max_add,
            }
            refs.save(topic_dir, store)
            if round_cap_hit:
                print(
                    f"[cap-hit] round {round_number} stopped at the per-gap cap "
                    f"(={effective_max_add}) with candidates remaining → this round "
                    f"CANNOT confirm saturation (term_check will reject it, F6)."
                )
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

        added, skipped_excluded, eligible_not_added = _apply_candidates(
            store,
            candidates,
            descendant_cache,
            declared_gap,
            round_number,
            effective_max_add,
            blocklist,
            skip_no_abstract=args.skip_no_abstract,
            relevance_terms=relevance_terms or _stored_relevance_terms(store, declared_gap),
            topic_dir=topic_dir,
            rejudge_uncertain=args.rejudge_uncertain,
        )

        store["rounds"] = max(store.get("rounds", 0), round_number)
        # C-F6a: the single-gap path (codex_round_loop's canonical per-gap call) ALSO
        # persists round_meta, else the cap-throttle signal never reaches term_check on
        # the canonical path (the old code wrote it only under --all-gaps). codex_round_loop
        # calls this once PER gap in a round, so OR-merge into the same round entry on the
        # freshly-loaded store (read-modify-write) — a later gap must not erase an earlier
        # gap's cap-hit (R3 minor: same-load read).
        gap_cap_hit = bool(effective_max_add and added >= effective_max_add and eligible_not_added > 0)
        round_meta = store.setdefault("round_meta", {})
        prev = round_meta.get(str(round_number), {})
        round_meta[str(round_number)] = {
            "genealogy_cap_hit": bool(prev.get("genealogy_cap_hit")) or gap_cap_hit,
            "effective_max_add": effective_max_add,
        }
        refs.save(topic_dir, store)
        seed_label = (
            f"{seeds_used}/{seeds_eligible}"
            if seeds_used != seeds_eligible
            else str(seeds_used)
        )
        if gap_cap_hit:
            print(
                f"[cap-hit] gap {declared_gap} round {round_number} stopped at the cap "
                f"(={effective_max_add}) with eligible candidates remaining → this round "
                f"CANNOT confirm saturation (term_check will reject it, C-F6a/b)."
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


def run(argv: list[str], cwd: str | None = None, env: dict[str, str] | None = None) -> int:
    """In-process entry for the daemon (E3) / tests; wraps main() via
    cli_runtime so the exit-code contract matches the standalone CLI."""
    return cli_runtime.invoke(main, argv, prog="genealogy.py", cwd=cwd, env=env)


if __name__ == "__main__":
    from lib import daemon

    raise SystemExit(daemon.cli_entry("genealogy", main))
