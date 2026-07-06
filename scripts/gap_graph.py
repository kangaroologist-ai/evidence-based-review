"""Citation-corpus clustering to surface gap-coverage holes (plan v3 §3.3 C12).

A data-driven companion to the LLM completeness critic (B1/W9): cluster the
verified corpus by shared title keywords and show, per dense cluster, which
declared gaps its members are assigned to. A cluster whose members carry NO
declared gap (all orphan/unassigned) is a candidate "uncovered topic" — a knot
of literature the review pulled in but never declared a gap for.

Design honesty: the faithful version would cluster on OpenAlex *concept ids*
(language-neutral), but entries do not store concepts yet, so this uses title
keywords as a proxy and keys "coverage" off the gap ASSIGNMENT (not off matching
CJK gap-description text against English titles — that cross-language match
would be all false positives). It is ADVISORY: it produces material for the
critic / human, it does not auto-prune or auto-declare.

Usage:
    python tools/gap_graph.py reviews/<topic> [--min-count N]
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib import testflight
import refs

# Generic / structural words that carry no topical signal.
_STOPWORDS = {
    "analysis", "approach", "association", "based", "between", "clinical",
    "comparison", "effect", "effects", "evidence", "from", "impact", "into",
    "meta", "methods", "model", "outcomes", "over", "patients", "randomized",
    "randomised", "review", "studies", "study", "systematic", "trial", "trials",
    "with", "without", "using", "their", "that", "this", "among", "across",
    "during", "after", "before", "versus", "role", "data", "results",
}


def _tokens(text: str) -> set[str]:
    """Significant lowercase title tokens (≥4 alpha chars, not stopwords)."""
    return {
        tok
        for tok in re.findall(r"[a-z][a-z0-9-]{3,}", (text or "").lower())
        if tok not in _STOPWORDS
    }


def _eligible(store: refs.Store) -> list[refs.Entry]:
    return [
        e
        for e in store["entries"].values()
        if e.get("verification_status") == "verified"
        and not e.get("retracted")
        and not e.get("excluded_reason")
    ]


def term_clusters(
    store: refs.Store, *, min_count: int = 3
) -> list[dict[str, object]]:
    """Cluster eligible entries by shared title term. For each term appearing in
    ≥ min_count entries, report its entry count, the set of declared gaps those
    entries belong to, and whether the cluster is uncovered (no declared gap)."""
    declared = set(store.get("gaps", {}))
    term_keys: dict[str, set[str]] = collections.defaultdict(set)
    term_gaps: dict[str, set[str]] = collections.defaultdict(set)
    for entry in _eligible(store):
        key = entry.get("citation_key") or entry.get("doi") or "?"
        gap = entry.get("gap")
        gap_label = gap if (isinstance(gap, str) and gap in declared) else None
        for tok in _tokens(entry.get("title", "")):
            term_keys[tok].add(key)
            if gap_label is not None:
                term_gaps[tok].add(gap_label)
    clusters: list[dict[str, object]] = []
    for term, keys in term_keys.items():
        if len(keys) < min_count:
            continue
        gaps_covering = sorted(term_gaps.get(term, set()))
        clusters.append({
            "term": term,
            "count": len(keys),
            "gaps": gaps_covering,
            "uncovered": not gaps_covering,
            "examples": sorted(keys)[:5],
        })
    clusters.sort(key=lambda c: (not c["uncovered"], -int(c["count"])))  # uncovered first, then dense
    return clusters


def _render(topic_name: str, clusters: list[dict[str, object]]) -> str:
    lines = [
        f"# {topic_name} — 引文聚类 gap 覆盖 (C12, advisory)",
        "",
        "_标题关键词聚类（≥min-count 条）；coverage 按 gap 赋值判定。uncovered=该关键词"
        "簇下没有任何已声明 gap，是候选缺口（交 completeness critic/人裁决）。_",
        "",
        "| 关键词 | 文献数 | 覆盖 gap | uncovered | 示例 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for c in clusters:
        gaps = ", ".join(c["gaps"]) if c["gaps"] else "—"  # type: ignore[arg-type]
        examples = ", ".join(c["examples"])  # type: ignore[arg-type]
        flag = "⚠️ 是" if c["uncovered"] else "否"
        lines.append(f"| {c['term']} | {c['count']} | {gaps} | {flag} | {examples} |")
    if not clusters:
        lines.append("| (无达到阈值的聚类) | | | | |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("topic_dir")
    parser.add_argument("--min-count", type=int, default=3,
                        help="Minimum entries sharing a term to form a cluster.")
    args = parser.parse_args()

    topic_dir = pathlib.Path(args.topic_dir)
    with testflight.timer("gap_graph", "main", topic_dir=topic_dir):
        store = refs.load(topic_dir)
        if store is None:
            print(f"[ERROR] missing references store: {topic_dir}")
            raise SystemExit(1)
        clusters = term_clusters(store, min_count=args.min_count)
        report = _render(topic_dir.name, clusters)
        meta_dir = topic_dir / "meta"
        meta_dir.mkdir(exist_ok=True)
        (meta_dir / "gap_graph_report.md").write_text(report, encoding="utf-8")

    uncovered = [c for c in clusters if c["uncovered"]]
    print(f"[OK] {len(clusters)} clusters (min-count={args.min_count}); "
          f"{len(uncovered)} uncovered → meta/gap_graph_report.md")
    for c in uncovered[:10]:
        print(f"  ⚠️ '{c['term']}' in {c['count']} papers, no declared gap")


if __name__ == "__main__":
    main()
