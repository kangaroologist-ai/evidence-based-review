"""Per-gap status summary for a review topic.

Reads ``reviews/<topic>/references_store.json`` + the per-entry files and
prints, for each declared gap, the count of entries split by
verification_status, plus an excluded count and (if review.md exists) the
number of citations to that gap from the review body.

Read-only; no side effects.

CLI:
    python tools/gaps_status.py reviews/<topic>
    python tools/gaps_status.py reviews/<topic> --gap gap-1
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib import config, testflight
from lib.citation_scan import scan_used_keys
import refs

# Evidence-tier ordering for the per-gap breakdown line. CLAUDE.md
# termination check requires "≥1 RCT/meta where the field has them",
# so we surface these counts inline instead of forcing the caller to
# grep study_type fields by hand.
_EVIDENCE_TIER_ORDER: tuple[str, ...] = (
    "systematic-review",
    "meta",
    "rct",
    "clinical-trial",
    "cohort",
    "observational",
    "review",
    "case-series",
    "case-report",
    "book-chapter",
    "other",
)


def _colored_count(value: int, *, warn: bool, use_color: bool) -> str:
    text = str(value)
    if not use_color or not warn:
        return text
    return f"\x1b[31m{text}\x1b[0m"


def _evidence_tier_summary(store: refs.Store, dois: list[str]) -> str:
    """Return a one-line `rct=N1 review=N2 cohort=N3 ...` summary over the
    given DOIs (typically the verified bucket of one gap). Buckets with
    count=0 are omitted; unknown study_types fall under "other"."""
    counts: collections.Counter[str] = collections.Counter()
    for doi in dois:
        entry = store["entries"].get(doi, {})
        st = (entry.get("study_type") or "other").lower()
        if st not in _EVIDENCE_TIER_ORDER:
            st = "other"
        counts[st] += 1
    if not counts:
        return "(none)"
    parts = [f"{tier}={counts[tier]}" for tier in _EVIDENCE_TIER_ORDER if counts[tier]]
    return " ".join(parts) if parts else "(none)"


def _used_keys(review_path: pathlib.Path) -> set[str]:
    if not review_path.exists():
        return set()
    return scan_used_keys(review_path.read_text(encoding="utf-8"))


def _bucket_entries(store: refs.Store) -> dict[str, dict[str, list[str]]]:
    """Return {gap_id: {"verified"|"pending"|"failed"|"excluded": [doi, ...]}}.

    Entries with ``entry.gap is None`` are bucketed under ``"<no gap>"`` so
    callers can see orphan counts too.
    """
    buckets: dict[str, dict[str, list[str]]] = collections.defaultdict(
        lambda: {"verified": [], "pending": [], "failed": [], "excluded": []}
    )
    for doi, entry in store["entries"].items():
        gap_id = entry.get("gap") if isinstance(entry.get("gap"), str) else "<no gap>"
        if entry.get("excluded_reason"):
            buckets[gap_id]["excluded"].append(doi)
            continue
        status = entry.get("verification_status", "pending")
        if status not in {"verified", "pending", "failed"}:
            status = "pending"
        buckets[gap_id][status].append(doi)
    return buckets


def _count_citations(
    store: refs.Store,
    used_keys: set[str],
) -> dict[str, int]:
    """For each gap id, count how many of its *verified, non-retracted,
    non-excluded* entries are cited in the review body."""
    counts: collections.Counter[str] = collections.Counter()
    for entry in store["entries"].values():
        citation_key = entry.get("citation_key")
        gap_id = entry.get("gap")
        if not (isinstance(citation_key, str) and isinstance(gap_id, str)):
            continue
        if citation_key not in used_keys:
            continue
        if entry.get("verification_status") != "verified":
            continue
        if entry.get("retracted") or entry.get("excluded_reason"):
            continue
        counts[gap_id] += 1
    return dict(counts)


def _print_gap(
    gap_id: str,
    store: refs.Store,
    buckets: dict[str, dict[str, list[str]]],
    citations: dict[str, int],
    *,
    use_color: bool = False,
) -> None:
    gap_meta = store.get("gaps", {}).get(gap_id, {})
    status = gap_meta.get("status", "pending")
    created = gap_meta.get("created_round", "?")
    resolved = gap_meta.get("resolved_round")
    description = gap_meta.get("description", "")
    gap_type = gap_meta.get("gap_type")
    secondary_type = gap_meta.get("secondary_type")
    subgap_of = gap_meta.get("subgap_of")
    depends_on = gap_meta.get("depends_on") or []
    fields = gap_meta.get("fields") if isinstance(gap_meta.get("fields"), dict) else {}

    resolved_text = f" resolved round {resolved}" if resolved is not None else ""
    header_extras: list[str] = [status]
    if isinstance(gap_type, str):
        header_extras.append(gap_type)
    header_tag = "[" + " ".join(header_extras) + "]"
    print(f"{gap_id} {header_tag} declared round {created}{resolved_text}")
    if description:
        print(f"  {description}")
    # Classification + relations metadata (only printed when set, to keep
    # the legacy-store output unchanged).
    if isinstance(secondary_type, str):
        print(f"  secondary_type: {secondary_type}")
    if isinstance(subgap_of, str) and subgap_of:
        print(f"  subgap_of: {subgap_of}")
    if depends_on:
        print(f"  depends_on: {', '.join(depends_on)}")
    # C16 (m2): show the persisted search query + relevance-gate terms so the operator
    # can see (and tune) what drives this gap's round expansion without grepping the store.
    query = gap_meta.get("query")
    relevance_terms = gap_meta.get("relevance_terms")
    if isinstance(query, str) and query.strip():
        print(f"  query: {query.strip()}")
    if relevance_terms:
        rt = relevance_terms if isinstance(relevance_terms, str) else ", ".join(relevance_terms)
        if rt.strip():
            print(f"  relevance_terms: {rt.strip()}")
    if fields:
        # Print structured sub-fields one per line, sorted for stable output.
        # Booleans (prevalence_subtag) come out as 'true'/'false'.
        for field_key in sorted(fields):
            field_value = fields[field_key]
            if isinstance(field_value, bool):
                field_value = "true" if field_value else "false"
            print(f"  {field_key}: {field_value}")

    bucket = buckets.get(gap_id, {"verified": [], "pending": [], "failed": [], "excluded": []})
    verified_count = len(bucket["verified"])
    cited_count = citations.get(gap_id, 0)
    broad_volume_warn = (
        gap_id != "<no gap>"
        and verified_count >= config.BROAD_GAP_VERIFIED_THRESHOLD
        and cited_count < config.BROAD_GAP_MIN_CITED
    )
    print(
        "  verified  : "
        + _colored_count(verified_count, warn=broad_volume_warn, use_color=use_color)
    )
    print(f"  pending   : {len(bucket['pending'])}")
    print(f"  failed    : {len(bucket['failed'])}")
    print(f"  excluded  : {len(bucket['excluded'])}")
    if bucket["verified"]:
        print(f"  evidence  : {_evidence_tier_summary(store, bucket['verified'])}")
    if gap_id in citations or gap_id != "<no gap>":
        print(f"  citations : {cited_count}")


def _warn_phantom_gaps(
    buckets: dict[str, dict[str, list[str]]],
    declared: set[str],
) -> None:
    """Print a [WARN] line for any gap id referenced by entries but absent
    from the declared gap list.  Called in both full and --gap modes so the
    integrity check is never silently skipped."""
    phantom_ids = sorted(
        gid for gid in buckets if gid != "<no gap>" and gid not in declared
    )
    if phantom_ids:
        phantom_total = sum(
            sum(len(v) for v in buckets[gid].values()) for gid in phantom_ids
        )
        print(
            f"[WARN] {len(phantom_ids)} undeclared gap(s) referenced by "
            f"{phantom_total} entries: {phantom_ids}"
        )
        print(
            "  These entries have a gap tag that is not in the store's "
            "declared gap list. Run lint_review.py to catch this upstream."
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Per-gap entry counts for a review topic (read-only)."
    )
    parser.add_argument("topic_dir")
    parser.add_argument(
        "--gap",
        help="Only report on this gap id (e.g. gap-1).",
    )
    parser.add_argument(
        "--include-orphans",
        action="store_true",
        help="Also show entries not tagged to any declared gap.",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable TTY color output.",
    )
    args = parser.parse_args()

    topic_dir = pathlib.Path(args.topic_dir)
    with testflight.timer("gaps_status", "main", topic_dir=topic_dir, gap=args.gap):
        store = refs.load(topic_dir)
        if store is None:
            print(f"[ERROR] no references store under {topic_dir}")
            raise SystemExit(1)

        used_keys = _used_keys(topic_dir / "review.md")
        buckets = _bucket_entries(store)
        citations = _count_citations(store, used_keys)
        declared = set(store.get("gaps", {}).keys())
        use_color = sys.stdout.isatty() and not args.no_color

        if args.gap:
            if args.gap not in declared:
                print(f"[ERROR] gap '{args.gap}' not declared in {topic_dir}")
                raise SystemExit(1)
            _print_gap(args.gap, store, buckets, citations, use_color=use_color)
            _warn_phantom_gaps(buckets, declared)
            return

        if not declared:
            print(f"(no gaps declared in {topic_dir})")
        else:
            for gap_id in sorted(declared):
                _print_gap(gap_id, store, buckets, citations, use_color=use_color)
                print()

        _warn_phantom_gaps(buckets, declared)

        orphan_total = sum(len(v) for v in buckets.get("<no gap>", {}).values())
        if orphan_total and (args.include_orphans or not declared):
            print("<no gap> (entries not tagged to any declared gap)")
            _print_gap("<no gap>", store, buckets, citations, use_color=use_color)
        elif orphan_total:
            print(f"[INFO] {orphan_total} entries have no gap tag; pass --include-orphans to see details")


if __name__ == "__main__":
    main()
