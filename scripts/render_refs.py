from __future__ import annotations

import argparse
import collections
import pathlib
import sys
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib import layout, testflight
import refs
import tmp_status

# Citation + marker regexes are shared with lint_review / gaps_status via
# lib/citation_scan (single source — they must agree on cited-key scanning).
# MARK_RE keeps its render_refs-local name (= REFS_MARK_RE) since this module
# both matches and writes the marker blocks.
from lib.citation_scan import (
    CITE_RE,
    PRISMA_MARK_RE,
    REFS_MARK_RE as MARK_RE,
    strip_tool_managed_blocks as _strip_tool_managed_blocks,
)


def _author_family(author: str) -> str:
    if "," in author:
        return author.split(",", 1)[0].strip().lower()
    parts = author.split()
    return parts[-1].lower() if parts else ""


def _author_text(authors: list[str]) -> str:
    if not authors:
        return "Anon."
    if len(authors) == 1:
        return authors[0]
    if len(authors) > 20:
        return ", ".join(authors[:19]) + ", ... " + authors[-1]
    return ", ".join(authors[:-1]) + ", & " + authors[-1]


def apa(entry: refs.Entry) -> str:
    author_text = _author_text(entry.get("authors", []))
    year_text = str(entry["year"]) if "year" in entry else "n.d."
    title = entry.get("title", "")
    journal = entry.get("journal", "")
    doi = entry["doi"]
    if journal:
        return f"{author_text} ({year_text}). {title}. *{journal}*. https://doi.org/{doi}"
    return f"{author_text} ({year_text}). {title}. https://doi.org/{doi}"


def _references_block(entries: list[refs.Entry]) -> str:
    rendered = "\n\n".join(apa(entry) for entry in entries)
    return f"<!-- refs:start -->\n## References\n\n{rendered}\n<!-- refs:end -->"


def _prisma_flow_block(store: refs.Store, cited_count: int) -> str:
    """Render the PRISMA-style 4-stage funnel from store state.

    Stages (playbook §7):
      - Identified: every entry the workflow has touched (includes failed verify + excluded)
      - Screened:   entries with successful CrossRef metadata fetch (verified or pending)
      - Eligible:   verified, non-excluded, non-retracted entries
      - Included:   entries cited in review.md body

    Excluded / failed / retracted are bookkept separately so the funnel
    is reproducible from references_store + audit log without leaking
    decisions into the review text.
    """
    all_entries = list(store["entries"].values())
    total = len(all_entries)
    failed = sum(1 for e in all_entries if e.get("verification_status") == "failed")
    excluded = sum(1 for e in all_entries if e.get("excluded_reason"))
    retracted = sum(1 for e in all_entries if e.get("retracted"))
    eligible = sum(
        1
        for e in all_entries
        if e.get("verification_status") == "verified"
        and not e.get("excluded_reason")
        and not e.get("retracted")
    )
    identified = total
    screened = total - failed  # crossref metadata succeeded for these

    def _pct(numerator: int, denominator: int) -> str:
        return f"{(numerator / denominator * 100):.1f}%" if denominator else "n/a"

    lines = [
        "<!-- prisma-flow:start -->",
        "## PRISMA flow（数字漏斗）",
        "",
        "| 阶段 | 数量 | 说明 |",
        "| --- | --- | --- |",
        f"| **Identified** | {identified} | 经 search / genealogy 拉入的候选（含 verify 失败与 excluded） |",
        f"| **Screened** | {screened} | CrossRef 元数据获取成功（去除 verify failed = {failed}） |",
        f"| **Eligible** | {eligible} | verified + 未 excluded + 未 retracted |",
        f"| **Included** | {cited_count} | review.md 正文引用数 |",
        "",
        f"漏斗比率：identified→eligible = {_pct(eligible, identified)}；"
        f"eligible→included = {_pct(cited_count, eligible)}。",
        "",
        f"_审计旁注：excluded = {excluded}；retracted = {retracted}（不进 References）。"
        "完整 exclusion 记录见 research_log.md 末尾 audit log。_",
        "<!-- prisma-flow:end -->",
    ]
    return "\n".join(lines)


def _write_citation_stats(stats_path: pathlib.Path, entries: list[refs.Entry]) -> None:
    """Overwrite reviews/<topic>/meta/citation_stats.md with the current
    study_type distribution. Kept out of research_log.md so the log
    remains a human-edited writing space."""
    counts = collections.Counter(entry.get("study_type", "other") for entry in entries)
    timestamp = datetime.now().isoformat(timespec="seconds")
    lines = [f"# 引用类型分布", "", f"_最后渲染: {timestamp}_  _共 {len(entries)} 条引用_", ""]
    lines.extend(f"- {study_type}: {count}" for study_type, count in counts.most_common())
    stats_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("review_md")
    parser.add_argument("topic_dir")
    parser.add_argument("--no-status", action="store_true")
    parser.add_argument(
        "--no-stats",
        action="store_true",
        help="Skip writing meta/citation_stats.md.",
    )
    parser.add_argument(
        "--gap-audit",
        action="store_true",
        help="Print per-gap coverage report.",
    )
    parser.add_argument(
        "--min-cited-per-gap",
        type=int,
        default=2,
        help="Fail gap audit if a pending gap has fewer cited keys.",
    )
    parser.add_argument(
        "--audit-all-gaps",
        action="store_true",
        help=(
            "By default audit only fails on 'pending' gaps. "
            "Pass this to also fail on 'resolved' / 'insufficient'."
        ),
    )
    parser.add_argument(
        "--min-refs",
        type=int,
        default=0,
        help=(
            "Warn when total cited references fall below this count. 0 = "
            "off (default). Use --gap-audit instead for the per-gap check; "
            "a global lower bound mis-flags decision-density reviews where "
            "low ref count is the deliberate output."
        ),
    )
    parser.add_argument(
        "--max-refs",
        type=int,
        default=0,
        help=(
            "Warn when total cited references exceed this count. 0 = off "
            "(default). Useful when a review is bloating beyond what one "
            "reader can absorb."
        ),
    )
    args = parser.parse_args()

    review_path = pathlib.Path(args.review_md)
    topic_dir = pathlib.Path(args.topic_dir)
    with testflight.timer("render_refs", "main", topic_dir=topic_dir) as detail:
        _render(review_path, topic_dir, args, detail)


def _render(
    review_path: pathlib.Path,
    topic_dir: pathlib.Path,
    args: argparse.Namespace,
    detail: dict[str, object],
) -> None:
    review_text = review_path.read_text(encoding="utf-8")
    store = refs.load(topic_dir)
    if store is None:
        print(f"[ERROR] missing references store: {topic_dir}")
        raise SystemExit(1)

    by_key: dict[str, refs.Entry] = {}
    for entry in store["entries"].values():
        citation_key = entry.get("citation_key")
        if isinstance(citation_key, str):
            by_key[citation_key] = entry

    # Scan citation keys only in author-written prose. Strip the PRISMA
    # flow block and the existing References block first — both are
    # tool-managed and re-rendered on every call, so any `@key` they
    # contain is noise (caused the rerender regression Codex P1 caught).
    scannable = _strip_tool_managed_blocks(review_text)
    used_keys = sorted(set(CITE_RE.findall(scannable)))
    missing = [key for key in used_keys if key not in by_key]
    not_verified = [
        key
        for key in used_keys
        if key in by_key and by_key[key].get("verification_status") != "verified"
    ]
    retracted = [
        key
        for key in used_keys
        if key in by_key and bool(by_key[key].get("retracted", False))
    ]
    excluded_cited = [
        key
        for key in used_keys
        if key in by_key and by_key[key].get("excluded_reason")
    ]
    if missing:
        print(f"[ERROR] citation keys not in references store: {missing}")
        raise SystemExit(1)
    if not_verified:
        print(f"[ERROR] citations are not verified: {not_verified}")
        raise SystemExit(1)
    if retracted:
        print(f"[ERROR] retracted citations in review: {retracted}")
        raise SystemExit(1)
    if excluded_cited:
        print(f"[ERROR] excluded entries cited in review: {excluded_cited}")
        print("  Either remove the citation or run `refs include_entry` to unmark.")
        raise SystemExit(1)

    superseded = [
        key
        for key in used_keys
        if key in by_key and isinstance(by_key[key].get("superseded_by"), str)
    ]
    if superseded:
        print(f"[WARN] superseded citations still in use: {superseded}")

    entries = [by_key[key] for key in used_keys]
    entries.sort(key=lambda entry: (_author_family((entry.get("authors") or [""])[0]), entry.get("year", 0)))

    # PRISMA flow: insert/update before References. Use marker pair for
    # idempotent re-renders; if no marker, append before the References
    # block (or at EOF if References missing too).
    prisma_block = _prisma_flow_block(store, len(entries))
    if PRISMA_MARK_RE.search(review_text):
        review_text = PRISMA_MARK_RE.sub(prisma_block, review_text)
    elif MARK_RE.search(review_text):
        # Insert before the existing References marker pair.
        review_text = MARK_RE.sub(prisma_block + "\n\n" + r"\g<0>", review_text)
    else:
        review_text = review_text.rstrip() + "\n\n" + prisma_block + "\n"

    block = _references_block(entries)
    if MARK_RE.search(review_text):
        rendered_text = MARK_RE.sub(block, review_text)
    else:
        rendered_text = review_text.rstrip() + "\n\n" + block + "\n"
    review_path.write_text(rendered_text, encoding="utf-8")
    print(f"rendered {len(entries)} references + PRISMA flow")
    detail["rendered"] = len(entries)

    if args.min_refs and len(entries) < args.min_refs:
        print(
            f"[WARN] {len(entries)} < --min-refs {args.min_refs}; "
            "consider another gap-fill round"
        )
    if args.max_refs and len(entries) > args.max_refs:
        print(
            f"[WARN] {len(entries)} > --max-refs {args.max_refs}; "
            "consider trimming"
        )

    if not args.no_stats:
        topic_dir = review_path.parent
        layout.ensure_subdirs(topic_dir)
        _write_citation_stats(layout.citation_stats_path(topic_dir), entries)

    if args.gap_audit:
        gaps = store.get("gaps", {})
        if not gaps:
            print("[WARN] no gaps declared in store")

        phantom_gap_entries = [
            f"{entry.get('citation_key', entry.get('doi', '?'))}->{gap_id}"
            for entry in store["entries"].values()
            for gap_id in [entry.get("gap")]
            if isinstance(gap_id, str) and gap_id not in gaps
        ]
        if phantom_gap_entries:
            print(f"[ERROR] entries reference undeclared gaps: {phantom_gap_entries}")
            raise SystemExit(1)

        key_to_gap = {
            entry["citation_key"]: entry.get("gap")
            for entry in store["entries"].values()
            if isinstance(entry.get("citation_key"), str)
        }
        per_gap_cited: dict[str, list[str]] = collections.defaultdict(list)
        per_gap_uncited: dict[str, list[str]] = collections.defaultdict(list)
        orphan_cited: list[str] = []

        for key in used_keys:
            gap_id = key_to_gap.get(key)
            if isinstance(gap_id, str) and gap_id:
                per_gap_cited[gap_id].append(key)
            else:
                orphan_cited.append(key)

        for entry in store["entries"].values():
            citation_key = entry.get("citation_key")
            gap_id = entry.get("gap")
            if (
                isinstance(citation_key, str)
                and isinstance(gap_id, str)
                and citation_key not in used_keys
                and not entry.get("excluded_reason")
            ):
                per_gap_uncited[gap_id].append(citation_key)

        print("\n=== gap audit ===")
        weak_pending: list[str] = []
        weak_other: list[str] = []
        for gap_id, meta in sorted(gaps.items()):
            cited = per_gap_cited.get(gap_id, [])
            uncited = per_gap_uncited.get(gap_id, [])
            status = meta.get("status", "pending")
            print(
                f"{gap_id}  [{status}]  cited={len(cited)}  "
                f"uncited_verified={len(uncited)}"
            )
            print(f"    {meta.get('description', '')}")
            if cited:
                print(f"    cited: {', '.join(cited)}")
            if uncited:
                print(f"    uncited: {', '.join(uncited)}")
            if len(cited) < args.min_cited_per_gap:
                if status == "pending":
                    weak_pending.append(gap_id)
                else:
                    weak_other.append(gap_id)

        if orphan_cited:
            print(f"\norphan (cited, no gap): {', '.join(orphan_cited)}")

        fail_set = weak_pending + (weak_other if args.audit_all_gaps else [])
        if fail_set:
            print(
                f"\n[FAIL] gaps below --min-cited-per-gap="
                f"{args.min_cited_per_gap}: {fail_set}"
            )
            raise SystemExit(1)
        if weak_other and not args.audit_all_gaps:
            print(
                "\n[INFO] gaps below threshold but not 'pending' "
                f"(use --audit-all-gaps to fail on these): {weak_other}"
            )

    if not args.no_status:
        print()
        tmp_status.report()


if __name__ == "__main__":
    main()
