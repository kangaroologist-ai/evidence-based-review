from __future__ import annotations

import argparse
import collections
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib import enrich, layout, testflight
from lib.citation_scan import CITE_RE
import refs

QUESTIONS = [
    "每个主要结论是否有 ≥2 个独立来源支撑？",
    "是否呈现了至少一组冲突证据或不同解释？",
    "摘要中的建议是否都能在正文证据中找到支撑？",
    "有没有断言超出文献支持范围？",
    "是否引用了任何 retracted、superseded 或弱 journal signals 的文献？",
    "是否标注了利益冲突 / 行业资助相关风险？",
    "中文人群证据是否被讨论或显式标缺？",
]
AUTO_BEGIN = "<!-- auto-detection:start -->"
AUTO_END = "<!-- auto-detection:end -->"
_AUTO_BLOCK_RE = re.compile(
    re.escape(AUTO_BEGIN) + r".*?" + re.escape(AUTO_END),
    re.S,
)


def weak_journal(signals: refs.JournalSignals | None) -> bool:
    if not signals:
        return False
    h_index = signals.get("h_index", 0)
    in_doaj = signals.get("in_doaj", False)
    return h_index < 10 and not in_doaj


def _build_auto_section(
    store: refs.Store,
    by_key: dict[str, refs.Entry],
    used_keys: list[str],
) -> list[str]:
    retracted = [key for key in used_keys if by_key.get(key, {}).get("retracted", False)]
    weak = [
        key
        for key in used_keys
        if weak_journal(by_key.get(key, {}).get("journal_signals"))
    ]
    superseded = [
        key
        for key in used_keys
        if isinstance(by_key.get(key, {}).get("superseded_by"), str)
    ]
    partial = [
        key
        for key in used_keys
        if bool(by_key.get(key, {}).get("verification", {}).get("partial"))
    ]

    flags: list[str] = []
    if retracted:
        flags.append(f"引用了 {len(retracted)} 条 retracted: {retracted}")
    if weak:
        flags.append(f"引用了 {len(weak)} 条弱 journal signals 文献: {weak}")
    if superseded:
        flags.append(f"引用了 {len(superseded)} 条 superseded preprint: {superseded}")
    if partial:
        flags.append(
            f"{len(partial)} 条 citation 仅使用了 Semantic Scholar fallback: {partial}"
        )

    study_counts = collections.Counter(
        by_key[key].get("study_type", "other")
        for key in used_keys
        if key in by_key
    )

    gap_lines: list[str] = []
    gaps = store.get("gaps", {})
    if gaps:
        key_to_gap = {
            entry["citation_key"]: entry.get("gap")
            for entry in store["entries"].values()
            if isinstance(entry.get("citation_key"), str)
        }
        for gap_id, meta in sorted(gaps.items()):
            cited = [key for key in used_keys if key_to_gap.get(key) == gap_id]
            status = meta.get("status", "pending")
            gap_lines.append(
                f"- {gap_id} [{status}] — cited {len(cited)} / "
                f"{meta.get('description', '')}"
            )

    lines: list[str] = [AUTO_BEGIN, "", "## 自动检测"]
    if flags:
        lines.extend(f"- {flag}" for flag in flags)
    else:
        lines.append("- 无自动检测告警")

    lines.extend(["", "## 引用类型分布"])
    if study_counts:
        lines.extend(
            f"- {study_type}: {count}"
            for study_type, count in study_counts.most_common()
        )
    else:
        lines.append("- 暂无正文引用")

    if gap_lines:
        lines.extend(["", "## Gap 覆盖率", *gap_lines])

    lines.extend(["", AUTO_END])
    return lines


def _build_qa_template() -> list[str]:
    lines: list[str] = ["## 自评问题（请回答）", ""]
    for index, question in enumerate(QUESTIONS, start=1):
        lines.extend([f"### {index}. {question}", "", "**答**：", "", ""])
    return lines


def _splice_auto_section(existing: str, auto_lines: list[str]) -> str:
    block = "\n".join(auto_lines)
    if _AUTO_BLOCK_RE.search(existing):
        return _AUTO_BLOCK_RE.sub(block, existing)

    heading = "# 交付前自评"
    if existing.startswith(heading):
        remainder = existing[len(heading):].lstrip("\n")
        pieces = [heading, "", block]
        if remainder:
            pieces.extend(["", remainder.rstrip()])
        return "\n".join(pieces).rstrip() + "\n"
    return block + "\n\n" + existing


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("topic_dir")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reset the Q&A template section.",
    )
    args = parser.parse_args()

    topic_dir = pathlib.Path(args.topic_dir)
    with testflight.timer("self_review", "main", topic_dir=topic_dir):
        output_path = layout.self_review_path(topic_dir)
        review_path = topic_dir / "review.md"
        store = refs.load(topic_dir)
        if store is None or not review_path.exists():
            print(f"[ERROR] missing review or references under {topic_dir}")
            raise SystemExit(1)

        review_text = review_path.read_text(encoding="utf-8")
        by_key: dict[str, refs.Entry] = {
            entry["citation_key"]: entry
            for entry in store["entries"].values()
            if isinstance(entry.get("citation_key"), str)
        }
        used_keys = sorted(set(CITE_RE.findall(review_text)))

        signals_updated = False
        for key in used_keys:
            entry = by_key.get(key)
            if entry is None:
                continue
            before = dict(entry.get("journal_signals") or {})
            enrich.ensure_journal_signals(entry)
            if entry.get("journal_signals") != before:
                signals_updated = True
        if signals_updated:
            refs.save(topic_dir, store)

        auto_lines = _build_auto_section(store, by_key, used_keys)
        layout.ensure_subdirs(topic_dir)
        if output_path.exists() and not args.force:
            existing = output_path.read_text(encoding="utf-8")
            merged = _splice_auto_section(existing, auto_lines)
            output_path.write_text(merged, encoding="utf-8")
            print(f"refreshed auto-detection in {output_path}")
            return

        qa_lines = _build_qa_template()
        output_path.write_text(
            "\n".join(["# 交付前自评", "", *auto_lines, "", *qa_lines]).rstrip() + "\n",
            encoding="utf-8",
        )
        print(f"created {output_path}")


if __name__ == "__main__":
    main()
