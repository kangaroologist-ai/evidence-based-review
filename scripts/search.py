from __future__ import annotations

import argparse
import pathlib
import re
import shlex
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib import apis, cli_runtime, testflight
from lib.extra_sources import search_extra
import refs

SEMANTIC_SCHOLAR_BASE = "https://api.semanticscholar.org/graph/v1"
SEMANTIC_FIELDS = "title,year,authors,externalIds,citationCount,publicationTypes"

# CrossRef title search ranks by string overlap, not topical relevance, so
# queries containing these high-noise generic words tend to surface GP /
# training / pedagogy / animal hits unrelated to the medical topic. The
# `kissing / mouse / bond / partner` family appears in the search.py
# docstring; this list extends it with common medical-survey-prose words
# observed to flood results in real review runs (e.g. gap-1 round-3 of
# the 夜间复视成因与排查 testflight returned 10 GP-quality-improvement
# papers because the query contained "approach" + "general practice").
# Match is whole-word, lowercase. Keep this list short — false-positive
# warnings are nearly free, but listing every generic word would make the
# hint useless.
_AMBIGUOUS_KEYWORDS: frozenset[str] = frozenset(
    {
        "approach",
        "evaluation",
        "assessment",
        "review",
        "study",
        "analysis",
        "model",
        "intervention",
        "general",
        "primary",
        "practice",
        "kissing",
        "mouse",
        "mice",
        "bond",
        "partner",
        "outcome",
    }
)


def _ambiguous_terms_in(query: str) -> list[str]:
    tokens = [t.lower() for t in re.findall(r"[A-Za-z][A-Za-z\-]+", query)]
    seen: list[str] = []
    for token in tokens:
        if token in _AMBIGUOUS_KEYWORDS and token not in seen:
            seen.append(token)
    return seen


def _merge_dedup(
    crossref_hits: list["SearchHit"],
    semantic_hits: list["SearchHit"],
) -> list["SearchHit"]:
    """Merge CrossRef + Semantic Scholar hits, dedup by DOI (case-insensitive).

    Order preservation: CrossRef hits come first (its metadata tends to be
    cleaner for DOI-known papers), then Semantic Scholar hits not already
    present (surfaces cross-domain matches CrossRef missed). For overlapping
    DOIs we keep whichever copy has the higher ``cited_by_count`` — Semantic
    Scholar tracks citations from a larger, more recent corpus."""
    by_doi: dict[str, "SearchHit"] = {}
    for hit in crossref_hits + semantic_hits:
        key = hit.doi.lower() if hit.doi else ""
        if not key:
            continue
        existing = by_doi.get(key)
        if existing is None or hit.cited_by_count > existing.cited_by_count:
            by_doi[key] = hit
    ordered: list["SearchHit"] = []
    seen_keys: set[str] = set()
    for hit in crossref_hits:
        key = hit.doi.lower() if hit.doi else ""
        if key and key not in seen_keys:
            seen_keys.add(key)
            ordered.append(by_doi[key])
    for hit in semantic_hits:
        key = hit.doi.lower() if hit.doi else ""
        if key and key not in seen_keys:
            seen_keys.add(key)
            ordered.append(by_doi[key])
    return ordered


@dataclass(frozen=True)
class SearchHit:
    doi: str
    title: str
    year: int
    authors: list[str]
    first_author_family: str
    cited_by_count: int
    publication_type: str


_RRF_K = 60


def _rank_candidates(
    hits: list[SearchHit],
    *,
    mode: str,
    cr_order: list[str] | None = None,
    sm_order: list[str] | None = None,
) -> list[SearchHit]:
    """Re-order merged candidates (see docs/research_tooling.md §3).

    ``rrf`` (default, Reciprocal Rank Fusion) fuses the two source rankings by
    *position* only: a paper ranked high by BOTH CrossRef and Semantic Scholar
    is boosted above one-source noise, and because it ignores absolute citation
    counts, brand-new papers are not penalised. Single-source results have only
    one ranking, so ``rrf`` ≡ ``relevance`` there (no-op short-circuit).
    ``cited`` / ``year`` sort by the corresponding ``SearchHit`` field;
    ``relevance`` keeps the existing merge order."""
    if mode == "relevance" or not hits:
        return hits
    if mode == "cited":
        return sorted(hits, key=lambda h: h.cited_by_count, reverse=True)
    if mode == "year":
        return sorted(hits, key=lambda h: h.year, reverse=True)
    # rrf — needs both source orderings; with one source it degenerates to the
    # existing relevance order, so short-circuit (keeps single-source default safe).
    if not cr_order or not sm_order:
        return hits
    cr_rank = {doi.lower(): i for i, doi in enumerate(cr_order)}
    sm_rank = {doi.lower(): i for i, doi in enumerate(sm_order)}

    def _rrf_score(hit: SearchHit) -> float:
        key = hit.doi.lower()
        score = 0.0
        if key in cr_rank:
            score += 1.0 / (_RRF_K + cr_rank[key])
        if key in sm_rank:
            score += 1.0 / (_RRF_K + sm_rank[key])
        return score

    # Stable sort preserves merge order within equal scores.
    return sorted(hits, key=_rrf_score, reverse=True)


def _as_dict(value: object) -> dict[str, object] | None:
    return value if isinstance(value, dict) else None


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _format_author(author_obj: object) -> str:
    author = _as_dict(author_obj)
    if author is None:
        return ""
    family = (_as_str(author.get("family")) or "").strip()
    given = (_as_str(author.get("given")) or "").strip()
    initials = "".join(part[0].upper() for part in given.split() if part)
    if family and initials:
        return f"{family}, {'. '.join(initials)}."
    if family:
        return family
    return given


def _first_author_family(authors: list[str]) -> str:
    if not authors:
        return ""
    first = authors[0]
    if "," in first:
        return first.split(",", 1)[0].strip()
    parts = [part for part in first.split() if part]
    return parts[-1] if parts else ""


def _year_from_message(message: dict[str, object]) -> int:
    for field_name in ("issued", "published-print", "published-online", "created"):
        dated = _as_dict(message.get(field_name))
        date_parts = _as_list((dated or {}).get("date-parts"))
        first_part = date_parts[0] if date_parts else None
        if isinstance(first_part, list) and first_part:
            year = _as_int(first_part[0])
            if year is not None:
                return year
    return 0


def _build_params(
    query: str,
    rows: int,
    year_from: int | None,
    year_to: int | None,
    type_filter: str | None,
) -> apis.QueryParams:
    params: apis.QueryParams = {
        "query": query,
        "rows": rows,
        "sort": "relevance",
        "order": "desc",
    }
    filters: list[str] = []
    if year_from is not None:
        filters.append(f"from-pub-date:{year_from}-01-01")
    if year_to is not None:
        filters.append(f"until-pub-date:{year_to}-12-31")
    if type_filter:
        filters.append(f"type:{type_filter}")
    if filters:
        params["filter"] = ",".join(filters)
    return apis.with_mailto(params)


def _search_crossref(
    query: str,
    rows: int,
    year_from: int | None,
    year_to: int | None,
    type_filter: str | None,
) -> list[SearchHit]:
    payload = apis.get_json(
        "https://api.crossref.org/works",
        params=_build_params(query, rows, year_from, year_to, type_filter),
    )
    message = _as_dict((payload or {}).get("message"))
    items = _as_list((message or {}).get("items"))
    hits: list[SearchHit] = []
    for item in items:
        raw = _as_dict(item)
        if raw is None:
            continue
        doi = (_as_str(raw.get("DOI")) or "").lower()
        titles = _strings(raw.get("title"))
        if not doi or not titles:
            continue
        authors = [formatted for formatted in (_format_author(author) for author in _as_list(raw.get("author"))) if formatted]
        hits.append(
            SearchHit(
                doi=doi,
                title=titles[0],
                year=_year_from_message(raw),
                authors=authors,
                first_author_family=_first_author_family(authors),
                cited_by_count=_as_int(raw.get("is-referenced-by-count")) or 0,
                publication_type=_as_str(raw.get("type")) or "",
            )
        )
    return hits


def _format_semantic_author(author_obj: object) -> str:
    author = _as_dict(author_obj)
    if author is None:
        return ""
    name = (_as_str(author.get("name")) or "").strip()
    if not name:
        return ""
    parts = name.split()
    if len(parts) == 1:
        return parts[0]
    family = parts[-1]
    given = " ".join(parts[:-1])
    initials = "".join(part[0].upper() for part in given.split() if part)
    return f"{family}, {'. '.join(initials)}." if initials else family


def _semantic_hit_from_item(raw: dict[str, object]) -> SearchHit | None:
    external = _as_dict(raw.get("externalIds")) or {}
    doi = (_as_str(external.get("DOI")) or "").lower()
    title = _as_str(raw.get("title")) or ""
    if not doi or not title:
        return None
    authors = [a for a in (_format_semantic_author(x) for x in _as_list(raw.get("authors"))) if a]
    pub_types = _strings(raw.get("publicationTypes"))
    return SearchHit(
        doi=doi,
        title=title,
        year=_as_int(raw.get("year")) or 0,
        authors=authors,
        first_author_family=_first_author_family(authors),
        cited_by_count=_as_int(raw.get("citationCount")) or 0,
        publication_type=pub_types[0].lower() if pub_types else "",
    )


def _semantic_year_range(year_from: int | None, year_to: int | None) -> str | None:
    if year_from is None and year_to is None:
        return None
    lo = str(year_from) if year_from is not None else ""
    hi = str(year_to) if year_to is not None else ""
    return f"{lo}-{hi}"


def _search_semantic_scholar(
    query: str,
    rows: int,
    year_from: int | None,
    year_to: int | None,
) -> list[SearchHit]:
    params: apis.QueryParams = {
        "query": query,
        "limit": rows,
        "fields": SEMANTIC_FIELDS,
    }
    year_range = _semantic_year_range(year_from, year_to)
    if year_range is not None:
        params["year"] = year_range
    payload = apis.get_json(f"{SEMANTIC_SCHOLAR_BASE}/paper/search", params=params)
    items = _as_list((payload or {}).get("data"))
    hits: list[SearchHit] = []
    for item in items:
        raw = _as_dict(item)
        if raw is None:
            continue
        hit = _semantic_hit_from_item(raw)
        if hit is not None:
            hits.append(hit)
    return hits


def _both_sources_parallel(
    query: str,
    *,
    rows: int,
    year_from: int | None,
    year_to: int | None,
    type_filter: str | None,
) -> tuple[list[SearchHit], list[SearchHit], list[str]]:
    """F7 (plan §8.2): run CrossRef + Semantic Scholar concurrently instead of
    back-to-back. They hit distinct hosts (HostGate-isolated, thread-safe — same
    threading model genealogy/fetch --parallel already use), so the two network
    round-trips overlap. A source that raises is isolated to [] so the other's
    hits survive — strictly more robust than the old sequential path, where an
    exception in either backend aborted the whole --source both search.

    Returns (crossref_hits, semantic_hits, failed_sources). failed_sources lists
    the labels whose backend raised (each isolated to []), so the caller can
    surface a DEGRADED both-source run (e.g. record it in detail[]) instead of
    silently treating half-coverage as full — an --auto-add run must not proceed
    on one-source results believing it had both. merge/dedup/rank unchanged."""
    failed: list[str] = []

    def _safe(fn, label: str) -> list[SearchHit]:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — one backend must not sink the other
            print(f"[WARN] search source {label} failed: {exc}", file=sys.stderr)
            failed.append(label)  # list.append is atomic under the GIL; ≤1/thread
            return []

    with ThreadPoolExecutor(max_workers=2) as pool:
        cr_future = pool.submit(
            _safe,
            lambda: _search_crossref(query, rows, year_from, year_to, type_filter),
            "crossref",
        )
        sm_future = pool.submit(
            _safe,
            lambda: _search_semantic_scholar(query, rows, year_from, year_to),
            "semantic",
        )
        cr_hits, sm_hits = cr_future.result(), sm_future.result()
    return cr_hits, sm_hits, sorted(failed)


def _match_semantic_scholar(query: str) -> list[SearchHit]:
    params: apis.QueryParams = {
        "query": query,
        "fields": SEMANTIC_FIELDS,
    }
    payload = apis.get_json(f"{SEMANTIC_SCHOLAR_BASE}/paper/search/match", params=params)
    items = _as_list((payload or {}).get("data"))
    hits: list[SearchHit] = []
    for item in items:
        raw = _as_dict(item)
        if raw is None:
            continue
        hit = _semantic_hit_from_item(raw)
        if hit is not None:
            hits.append(hit)
    return hits


def _add_command(topic_dir: str, hit: SearchHit, round_number: int) -> str:
    authors = "; ".join(hit.authors)
    return " ".join(
        [
            "python3",
            "tools/verify.py",
            shlex.quote(topic_dir),
            "--add",
            shlex.quote(hit.doi),
            shlex.quote(hit.title),
            str(hit.year),
            shlex.quote(authors),
            "--source",
            "search",
            "--round",
            str(round_number),
        ]
    )


def _print_hits(hits: list[SearchHit], *, topic_dir: str, round_number: int) -> None:
    for index, hit in enumerate(hits, start=1):
        print(
            f"[{index}] cited={hit.cited_by_count} year={hit.year} "
            f"type={hit.publication_type or 'unknown'} first_author={hit.first_author_family or '-'} "
            f"doi={hit.doi}"
        )
        print(f"    {hit.title[:80]}")
        print(f"    add: {_add_command(topic_dir, hit, round_number)}")


def _emit_batch(
    hits: list[SearchHit], out_path: pathlib.Path, *, gap_id: str | None
) -> int:
    """Write hits as a TSV candidate file for ``verify.py --add-batch``: one
    ``DOI<TAB>TITLE<TAB>YEAR<TAB>AUTHORS<TAB>GAP`` row per hit (authors joined
    by '; '). Closes the discovery-first loop: search → emit-batch → eyeball &
    delete rows → ``verify --add-batch``. The reader ignores blank and
    ``#``-prefixed lines; a header comment documents the columns."""

    def _clean(text: str) -> str:
        return text.replace("\t", " ").replace("\n", " ").strip()

    lines = [
        "# DOI\tTITLE\tYEAR\tAUTHORS\tGAP  — delete unwanted rows, then:",
        "# python tools/verify.py <topic> --add-batch <this-file> --source search --round R",
    ]
    for hit in hits:
        authors = "; ".join(hit.authors)
        lines.append(
            f"{hit.doi}\t{_clean(hit.title)}\t{hit.year}\t{_clean(authors)}\t{gap_id or ''}"
        )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(hits)


def _gap_declared(topic_dir: pathlib.Path, gap_id: str) -> bool:
    store = refs.load(topic_dir)
    return store is not None and gap_id in store.get("gaps", {})


def _gap_has_seed(store: refs.Store, gap_id: str) -> bool:
    """True iff some entry tagged to `gap_id` was added with source=seed.

    Drives the §A soft guardrail: search before seeding a gap's known
    landmarks tends to flood the store with low-relevance noise that the
    landmark genealogy would have anchored. We only warn (never block) —
    the correct first round for a brand-new gap is seed → search → genealogy,
    but legitimate later rounds may search a gap that was seeded earlier in
    a prior run, and we must not get in their way."""
    for entry in store["entries"].values():
        if entry.get("gap") == gap_id and entry.get("source") == "seed":
            return True
    return False


def _auto_add(
    topic_dir: pathlib.Path,
    hits: list[SearchHit],
    *,
    gap_id: str | None,
    round_number: int,
    max_add: int = 0,
) -> tuple[int, int]:
    """Write search hits as pending entries, skipping any DOI already on the
    sticky exclusion denylist. Returns (added, skipped_excluded).

    Excluded DOIs are deliberate prior-round noise rejections; re-adding them
    would clear the excluded flag via upsert and resurrect the noise, forcing
    a re-prune loop (the bug §B fixes). We load the store once up front so the
    is_excluded check is a pure in-memory dict lookup per hit."""
    store = refs.load(topic_dir)
    added = 0
    skipped_excluded = 0
    skipped_nodoi = 0
    for hit in hits:
        if max_add and added >= max_add:
            break
        # R6 (C8 noise gate): clinical-trial hits carry doi="nct:<NCTId>" and
        # other extra sources can lack a DOI — neither is verify-able, so don't
        # auto-add them as pending (they'd just fail verify and clutter the store).
        doi = (hit.doi or "").strip()
        if not doi or doi.lower().startswith("nct:"):
            skipped_nodoi += 1
            print(f"[SKIP] no verifiable DOI (nct/empty): {hit.doi!r}", file=sys.stderr)
            continue
        if store is not None and refs.is_excluded(store, hit.doi):
            skipped_excluded += 1
            print(f"[SKIP] previously-excluded: {hit.doi}", file=sys.stderr)
            continue
        refs.put_entry(
            topic_dir,
            {
                "doi": hit.doi,
                "title": hit.title,
                "year": hit.year,
                "authors": hit.authors,
                "source": "search",
                "added_round": round_number,
                "gap": gap_id,
                "verification_status": "pending",
            },
        )
        added += 1
        print(f"[ADDED] {hit.doi}")
    if skipped_nodoi:
        print(
            f"[auto-add] skipped {skipped_nodoi} hit(s) with no verifiable DOI "
            "(nct/empty) — R6 noise gate",
            file=sys.stderr,
        )
    return added, skipped_excluded


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Find candidate DOIs by keyword or fuzzy title. "
            "Tip: for queries with cross-domain ambiguous terms "
            "(kissing, mouse, bond, partner, …) CrossRef title search "
            "tends to surface unrelated dental / animal hits. Use "
            "`--source semantic` — Semantic Scholar's relevance ranking "
            "handles topic disambiguation much better."
        )
    )
    parser.add_argument("query", nargs="+")
    parser.add_argument("--rows", type=int, default=10)
    parser.add_argument("--year-from", type=int)
    parser.add_argument("--year-to", type=int)
    parser.add_argument("--type", dest="type_filter")
    parser.add_argument(
        "--source",
        choices=["crossref", "semantic", "both", "pubmed", "biorxiv", "clinicaltrials", "all"],
        default="both",
        help=(
            "Search backend. both = crossref + semantic merge dedup "
            "(default; recommended for first-pass — Bramer 2017 multi-DB "
            "rationale). crossref = keyword search over DOI registry. "
            "semantic = Semantic Scholar relevance-ranked search."
        ),
    )
    parser.add_argument(
        "--match",
        action="store_true",
        help="Use Semantic Scholar /paper/search/match for fuzzy title→DOI "
        "resolution. Implies --source semantic; ignores --rows / --type.",
    )
    parser.add_argument("--auto-add")
    parser.add_argument(
        "--max-add",
        type=int,
        default=0,
        help="Cap hits auto-added per query (0 = no cap). Bounds greedy "
        "auto-add noise — rows x both-source can yield 20+/query, mostly "
        "off-topic. Prefer discovery-first: search WITHOUT --auto-add, eyeball "
        "titles, then `verify.py --add` the hits.",
    )
    parser.add_argument("--gap")
    parser.add_argument("--round", dest="round_number", type=int, default=1)
    parser.add_argument(
        "--rank",
        choices=["relevance", "rrf", "cited", "year"],
        default="rrf",
        help="Ordering of --source both results. rrf (default) = Reciprocal "
        "Rank Fusion of the CrossRef + Semantic rankings (two-source consensus "
        "on top, new papers not penalised by low citation counts); relevance = "
        "legacy merge order; cited / year = sort by citations / recency. "
        "Single-source: rrf == relevance. See docs/research_tooling.md §3.",
    )
    parser.add_argument(
        "--emit-batch",
        help="Write candidates as a TSV (DOI/TITLE/YEAR/AUTHORS/GAP) to this "
        "path instead of auto-adding; edit it (delete rows), then feed to "
        "`verify.py --add-batch`. Mutually exclusive with --auto-add.",
    )
    args = parser.parse_args()

    # --type is a CrossRef-only filter. Under --source both (the default),
    # CrossRef hits get filtered while Semantic Scholar hits silently pass
    # through unfiltered — caller would see e.g. non-review Semantic hits
    # despite `--type review-article`. Fail fast: --type requires
    # --source crossref. (Codex P3 2026-05-24)
    if args.type_filter and (args.match or args.source != "crossref"):
        print(
            "[ERROR] --type is CrossRef-only; with --source "
            f"{'match' if args.match else args.source}, the type filter "
            "only applies to CrossRef hits while other-source results "
            "pass through unfiltered. Switch --source crossref or drop --type."
        )
        raise SystemExit(1)

    if args.emit_batch and args.auto_add:
        print(
            "[ERROR] --emit-batch and --auto-add are mutually exclusive: "
            "emit-batch writes candidates to a file for review then "
            "`verify --add-batch`; auto-add writes straight to the store.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    query = " ".join(args.query).strip()

    # CrossRef's keyword-overlap ranking generates noise on generic terms;
    # Semantic Scholar's relevance ranking handles those better. With
    # --source both (default) we already run both backends, so this hint
    # is only useful when the caller explicitly picked crossref-only.
    if args.source == "crossref" and not args.match:
        ambiguous = _ambiguous_terms_in(query)
        if ambiguous:
            print(
                f"[HINT] query contains generic term(s) {ambiguous} — CrossRef "
                "ranks by title-string overlap and tends to return unrelated "
                "GP / pedagogy / animal hits for these. Consider re-running "
                "with --source both (default) or semantic for relevance-ranked "
                "results."
            )
    topic_dir = pathlib.Path(args.auto_add) if args.auto_add else None

    # Fail-fast: gap must be declared BEFORE we hit the API and print the
    # candidate list, otherwise the lone [ERROR] line gets buried under 10+
    # `add: python3 tools/verify.py ...` lines and is easy to miss
    # (see reviews/成年绝育室内猫晨间爆发活动成因与缓解/testflight_retro.md).
    if args.auto_add and args.gap and topic_dir is not None:
        store_for_checks = refs.load(topic_dir)
        if store_for_checks is None or args.gap not in store_for_checks.get(
            "gaps", {}
        ):
            print(
                f"[ERROR] gap '{args.gap}' not declared in {args.auto_add} — "
                f"run `python tools/verify.py {shlex.quote(str(args.auto_add))} "
                f"--declare-gap {args.gap} '<description>' "
                f"--round {args.round_number}` first.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        # §A soft guardrail: searching a gap before seeding its known
        # landmarks floods the store with low-relevance noise. WARN only —
        # never block — so legitimate later rounds (gap seeded in a prior
        # run) are not impeded.
        if not _gap_has_seed(store_for_checks, args.gap):
            print(
                f"[HINT] gap '{args.gap}' has no source=seed entry yet — "
                "consider seeding known landmark DOIs first "
                f"(`python tools/verify.py {shlex.quote(str(args.auto_add))} "
                f"--add DOI TITLE YEAR AUTHORS --gap {args.gap} "
                "--source seed`), then search to fill the gaps. seed → search "
                "→ genealogy keeps the candidate pool clean.",
                file=sys.stderr,
            )

    op = "match" if args.match else f"search_{args.source}"
    with testflight.timer(
        "search",
        op,
        topic_dir=topic_dir,
        rows=args.rows,
        gap=args.gap,
    ) as detail:
        cr_order: list[str] | None = None
        sm_order: list[str] | None = None
        if args.match:
            hits = _match_semantic_scholar(query)
        elif args.source == "semantic":
            hits = _search_semantic_scholar(
                query,
                rows=args.rows,
                year_from=args.year_from,
                year_to=args.year_to,
            )
        elif args.source == "crossref":
            hits = _search_crossref(
                query,
                rows=args.rows,
                year_from=args.year_from,
                year_to=args.year_to,
                type_filter=args.type_filter,
            )
        elif args.source in ("pubmed", "biorxiv", "clinicaltrials"):
            # plan v3 C8 / A4:免认证 REST 多源（每源失败优雅降级回 []）。
            hits = search_extra(query, args.rows, [args.source])
        elif args.source == "all":
            # crossref + semantic (并发) ∪ pubmed/biorxiv/clinicaltrials（新 DOI 追加）。
            cr_hits, sm_hits, source_failures = _both_sources_parallel(
                query,
                rows=args.rows,
                year_from=args.year_from,
                year_to=args.year_to,
                type_filter=args.type_filter,
            )
            hits = _merge_dedup(cr_hits, sm_hits)
            cr_order = [h.doi for h in cr_hits]
            sm_order = [h.doi for h in sm_hits]
            detail["crossref_hits"] = len(cr_hits)
            detail["semantic_hits"] = len(sm_hits)
            if source_failures:
                detail["source_failures"] = source_failures
            extra_hits = search_extra(
                query, args.rows, ["pubmed", "biorxiv", "clinicaltrials"]
            )
            seen_dois = {h.doi.lower() for h in hits}
            for h in extra_hits:
                if h.doi.lower() not in seen_dois:
                    hits.append(h)
                    seen_dois.add(h.doi.lower())
            detail["extra_hits"] = len(extra_hits)
        else:  # both — default; merge dedup by DOI
            cr_hits, sm_hits, source_failures = _both_sources_parallel(
                query,
                rows=args.rows,
                year_from=args.year_from,
                year_to=args.year_to,
                type_filter=args.type_filter,
            )
            hits = _merge_dedup(cr_hits, sm_hits)
            cr_order = [h.doi for h in cr_hits]
            sm_order = [h.doi for h in sm_hits]
            detail["crossref_hits"] = len(cr_hits)
            detail["semantic_hits"] = len(sm_hits)
            # F7: surface a degraded both-source run so downstream (e.g. auto-add)
            # doesn't silently treat one-source coverage as full both-source.
            if source_failures:
                detail["source_failures"] = source_failures
        if not args.match:
            hits = _rank_candidates(
                hits, mode=args.rank, cr_order=cr_order, sm_order=sm_order
            )
        detail["rank"] = "match" if args.match else args.rank
        topic_dir_label = args.auto_add or "TOPIC_DIR"
        _print_hits(hits, topic_dir=topic_dir_label, round_number=args.round_number)
        detail["hits"] = len(hits)

        if args.emit_batch:
            emitted = _emit_batch(
                hits, pathlib.Path(args.emit_batch), gap_id=args.gap
            )
            detail["emitted"] = emitted
            print(f"emitted {emitted} candidates → {args.emit_batch}")
            return

        if args.auto_add and topic_dir is not None:
            added, skipped_excluded = _auto_add(
                topic_dir, hits, gap_id=args.gap, round_number=args.round_number,
                max_add=args.max_add,
            )
            detail["added"] = added
            detail["skipped_excluded"] = skipped_excluded
            print(f"auto-added {added} entries to {topic_dir}")
            if skipped_excluded:
                print(
                    f"skipped {skipped_excluded} previously-excluded",
                    file=sys.stderr,
                )
            return

        print(f"{len(hits)} results")


def run(argv: list[str], cwd: str | None = None, env: dict[str, str] | None = None) -> int:
    """In-process entry for the daemon (E3) / tests; wraps main() via
    cli_runtime so the exit-code contract matches the standalone CLI."""
    return cli_runtime.invoke(main, argv, prog="search.py", cwd=cwd, env=env)


if __name__ == "__main__":
    from lib import daemon

    raise SystemExit(daemon.cli_entry("search", main))
