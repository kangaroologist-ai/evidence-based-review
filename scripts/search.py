from __future__ import annotations

import argparse
import pathlib
import re
import shlex
import sys
from dataclasses import dataclass

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib import apis, testflight
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
    for hit in hits:
        if max_add and added >= max_add:
            break
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
        choices=["crossref", "semantic", "both"],
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
                f"run `python scripts/verify.py {shlex.quote(str(args.auto_add))} "
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
                f"(`python scripts/verify.py {shlex.quote(str(args.auto_add))} "
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
        else:  # both — default; merge dedup by DOI
            cr_hits = _search_crossref(
                query,
                rows=args.rows,
                year_from=args.year_from,
                year_to=args.year_to,
                type_filter=args.type_filter,
            )
            sm_hits = _search_semantic_scholar(
                query,
                rows=args.rows,
                year_from=args.year_from,
                year_to=args.year_to,
            )
            hits = _merge_dedup(cr_hits, sm_hits)
            detail["crossref_hits"] = len(cr_hits)
            detail["semantic_hits"] = len(sm_hits)
        topic_dir_label = args.auto_add or "TOPIC_DIR"
        _print_hits(hits, topic_dir=topic_dir_label, round_number=args.round_number)
        detail["hits"] = len(hits)

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


if __name__ == "__main__":
    main()
