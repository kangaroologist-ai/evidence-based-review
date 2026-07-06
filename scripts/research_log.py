"""Scaffold the mechanical parts of a research_log.md round section.

D2 (docs/research_tooling.md §5.2): the *judgment* in a research_log round
(why open/close the next round, how a contradiction is adjudicated, which
entries to prune) is the main thread's / analyst's reasoning and stays a
``<!-- judgment -->`` placeholder. But the *transcription* around it is purely
mechanical and is assembled here from artifacts that already exist on disk:

- per-gap status table        ← gaps_status._bucket_entries (refs store)
- this-round additions by src ← entries with added_round == R, grouped by source
- analyst four-bucket lists    ← notes/round-R/gap-X.annotated.md (cite/keep/uncertain)
- post-round pruning audit     ← the "## Exclusion audit log" section (exclude.py auto-writes it)
- genealogy provenance count   ← source=genealogy verified added this round

Read-only by default: prints the scaffolded section to stdout for the main
thread to review, fill the judgment placeholders, and paste. ``--write``
appends it (non-destructively) to research_log.md under a relocatable marker.

CLI:
    python tools/research_log.py scaffold reviews/<topic> --round 2
    python tools/research_log.py scaffold reviews/<topic> --round 2 --write
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from gaps_status import _bucket_entries, _evidence_tier_summary
from lib import annotated, testflight
import refs

_KEY_RE = re.compile(r"\[@([^\]\s]+)\]")
_VERDICTS = ("cite", "keep", "uncertain")


def _round_additions(
    store: refs.Store, round_n: int
) -> dict[str, collections.Counter[str]]:
    """gap_id -> Counter({source: count}) for verified, non-excluded entries
    whose added_round == round_n."""
    out: dict[str, collections.Counter[str]] = collections.defaultdict(
        collections.Counter
    )
    for entry in store["entries"].values():
        if entry.get("added_round") != round_n:
            continue
        if entry.get("excluded_reason"):
            continue
        if entry.get("verification_status") != "verified":
            continue
        gap_id = entry.get("gap") if isinstance(entry.get("gap"), str) else "<no gap>"
        source = entry.get("source") if isinstance(entry.get("source"), str) else "?"
        out[gap_id][source or "?"] += 1
    return out


def _parse_annotated(path: pathlib.Path) -> dict[str, list[tuple[str, str]]]:
    """Parse gap-X.annotated.md into {verdict: [(citation_key, study_type)]}.

    Delegates to the shared tolerant parser (lib.annotated, C11) which accepts the
    Markdown pipe table ``| [@key] | study_type | 数字 | cite — 理由 |`` AND the
    section-list / inline shapes the analyst sometimes emits under pressure — the
    pipe-table-only parser used to drop the whole gap's cites for those (testflight F7).
    """
    return annotated.parse(path)


def _exclusion_audit_lines(research_log: pathlib.Path) -> list[str]:
    """The bullet lines under the '## Exclusion audit log' section (exclude.py /
    regap.py auto-append them). Returned verbatim; round attribution is the
    main thread's call (the audit log is timestamped, not round-stamped)."""
    if not research_log.exists():
        return []
    text = research_log.read_text(encoding="utf-8")
    marker = "## Exclusion audit log"
    idx = text.find(marker)
    if idx == -1:
        return []
    section = text[idx + len(marker):]
    # stop at the next H2 if any
    nxt = section.find("\n## ")
    if nxt != -1:
        section = section[:nxt]
    return [ln.rstrip() for ln in section.splitlines() if ln.lstrip().startswith("- ")]


def _render(store: refs.Store, round_n: int, topic_dir: pathlib.Path) -> str:
    buckets = _bucket_entries(store)
    additions = _round_additions(store, round_n)
    declared = sorted(store.get("gaps", {}).keys())
    notes_dir = topic_dir / "notes" / f"round-{round_n}"

    lines: list[str] = []
    lines.append(f"## Round {round_n}（scaffold 自动生成 — judgment 节点待主线程填）\n")

    # 1. gap status table
    lines.append("### 识别的空白（gap 状态表）\n")
    lines.append("| gap | type | verified | pending | excluded | created_round |")
    lines.append("|---|---|---:|---:|---:|---:|")
    for gid in declared:
        meta = store["gaps"][gid]
        b = buckets.get(gid, {"verified": [], "pending": [], "excluded": []})
        lines.append(
            f"| {gid} | {meta.get('gap_type', '?')} | {len(b['verified'])} | "
            f"{len(b.get('pending', []))} | {len(b.get('excluded', []))} | "
            f"{meta.get('created_round', '?')} |"
        )
    lines.append("")

    # 2. per-gap this-round additions by source
    lines.append(f"### 每条 gap 的定向扩展（Round {round_n} 新增 verified，按 source）\n")
    for gid in declared:
        srcs = additions.get(gid)
        if not srcs:
            lines.append(f"- {gid} → 本轮无新增 verified")
            continue
        total = sum(srcs.values())
        by_src = " / ".join(f"{s}={n}" for s, n in sorted(srcs.items()))
        tier = _evidence_tier_summary(store, buckets.get(gid, {}).get("verified", []))
        lines.append(f"- {gid} → +{total}（{by_src}）；累计 verified evidence: {tier}")
    lines.append("")

    # 3. analyst four-bucket transcription from annotated.md
    lines.append(
        f"### 各 gap analyst 四类清单（转录自 notes/round-{round_n}/gap-X.annotated.md）\n"
    )
    any_annotated = False
    for gid in declared:
        ann = _parse_annotated(notes_dir / f"{gid}.annotated.md")
        if not any(ann.values()):
            continue
        any_annotated = True
        lines.append(f"- **{gid}**")
        for verdict in _VERDICTS:
            items = ann[verdict]
            if items:
                keys = ", ".join(f"[{k}]" for k, _st in items)
                lines.append(f"  - {verdict}: {keys}")
    if not any_annotated:
        lines.append(
            f"_（notes/round-{round_n}/ 下无 *.annotated.md；analyst 跑完后重跑本 scaffold）_"
        )
    lines.append("")

    # 4. post-round pruning: judgment placeholder + auto-collected audit
    lines.append("### 轮后取舍\n")
    lines.append(
        "<!-- judgment: 主线程填 — exclude/regap 决定与理由、是否再开一轮、"
        "新 gap candidate 取舍、矛盾如何裁决 -->\n"
    )
    audit = _exclusion_audit_lines(topic_dir / "research_log.md")
    if audit:
        lines.append("自动收集的 exclude/regap 审计（按需筛出属于本轮的）：")
        lines.extend(audit)
    else:
        lines.append("_（本主题 Exclusion audit log 暂无条目）_")
    lines.append("")

    # 5. genealogy provenance (Stop hook requires ≥1 genealogy entry/round).
    # genealogy.py writes source='genealogy_ancestor'/'genealogy_descendant';
    # match review_workflow_check.sh which counts via src.startswith('genealogy').
    gen_total = sum(
        n
        for srcs in additions.values()
        for source, n in srcs.items()
        if source.startswith("genealogy")
    )
    flag = "✓ 满足 hook ≥1/round" if gen_total >= 1 else "✗ 本轮缺 genealogy entry，hook 会拦"
    lines.append("### genealogy 留痕\n")
    lines.append(f"- Round {round_n} 新增 source=genealogy verified：{gen_total} 条（{flag}）")
    lines.append("")
    return "\n".join(lines)


def _do_scaffold(args: argparse.Namespace) -> int:
    topic_dir = pathlib.Path(args.topic_dir)
    store = refs.load(topic_dir)
    if store is None:
        print(f"[ERROR] no references store under {topic_dir}", file=sys.stderr)
        return 1
    section = _render(store, args.round_number, topic_dir)
    if args.write:
        log_path = topic_dir / "research_log.md"
        if not log_path.exists():
            print(f"[ERROR] {log_path} not found", file=sys.stderr)
            return 1
        banner = (
            f"\n<!-- scaffold:round-{args.round_number} — 移到正确位置并填 judgment 后删除本注释 -->\n"
        )
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(banner + section + "\n")
        print(
            f"[OK] appended scaffold for round {args.round_number} to {log_path} "
            "(relocate + fill judgment)"
        )
    else:
        print(section)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scaffold the mechanical parts of a research_log round section (read-only by default)."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    scaffold = sub.add_parser(
        "scaffold", help="Emit a round section (gap table + additions + annotated transcription + audit)."
    )
    scaffold.add_argument("topic_dir")
    scaffold.add_argument("--round", dest="round_number", type=int, required=True)
    scaffold.add_argument(
        "--write",
        action="store_true",
        help="Append the scaffold to research_log.md (non-destructive; relocate manually).",
    )
    args = parser.parse_args()
    with testflight.timer("research_log", args.command, topic_dir=pathlib.Path(args.topic_dir)):
        if args.command == "scaffold":
            raise SystemExit(_do_scaffold(args))


if __name__ == "__main__":
    main()
