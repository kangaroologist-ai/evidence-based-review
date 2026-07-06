"""Per-round reading-notes scaffolder.

After fetching abstracts in round N, run this tool to materialize
per-gap notes files under `reviews/<topic>/notes/round-N/gap-X.md`,
plus a small index at `reviews/<topic>/notes/round-N.md` listing them.

Per-gap default exists because a single Round-N file with 300+ entries
across 7 gaps is too big for an LLM to read holistically in one pass —
context window blows up before synthesis. Per-gap files (~30-50 entries
each) are the natural unit for "spawn one summarization subagent per gap"
in the CLAUDE.md Step 5 workflow.

The `--gap GAP_ID` filter limits the run to a single gap (still under
the per-round directory). The `--single-file` flag falls back to the
legacy "one Round-N.md with everything" layout for callers that want
the old behaviour.

If a target file already exists the tool refuses to overwrite unless
`--force` is passed — accidental rerun should not wipe accumulated notes.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib import analyst_prompt, project, testflight
import refs


def _entry_section(entry: refs.Entry, abstract_text: str) -> str:
    citation_key = entry.get("citation_key", "")
    title = entry.get("title", "")
    year = entry.get("year", "n.d.")
    authors = ", ".join(entry.get("authors", []) or [])
    journal = entry.get("journal", "")
    study_type = entry.get("study_type", "other")
    doi = entry.get("doi", "")
    gap = entry.get("gap") or "<no gap>"

    body_lines = [
        f"### [@{citation_key}] {title}",
        "",
        f"- **作者**: {authors}",
        f"- **年份/期刊**: {year} / {journal}",
        f"- **研究类型**: {study_type}",
        f"- **DOI**: {doi}",
        f"- **Gap**: {gap}",
        "",
        "#### 摘要",
        "",
        abstract_text.strip() or "_（本轮未抓到摘要 — 检查 fetch_state.abstract）_",
        "",
        "#### 笔记（agent 填）",
        "",
        "- 一句话结论：",
        "- 关键数字 / 反例：",
        "- 对哪条 gap 的支撑强度（强 / 中 / 弱 / 反例）：",
        "- 可在正文引用的原话或数据点：",
        "",
        "---",
        "",
    ]
    return "\n".join(body_lines)


def _read_abstract(entry: refs.Entry) -> str:
    abstract_rel = entry.get("paths", {}).get("abstract")
    if not isinstance(abstract_rel, str):
        return ""
    abstract_path = project.to_abs(abstract_rel)
    if abstract_path is None or not abstract_path.exists():
        return ""
    text = abstract_path.read_text(encoding="utf-8")
    # Strip the YAML frontmatter and "## Abstract" header so the note file
    # reads cleanly. write_card emits frontmatter delimited by ---.
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4 :]
    marker = "## Abstract"
    if marker in text:
        text = text.split(marker, 1)[1]
    # Stop before the next H2 (Introduction / Conclusion blocks).
    next_section = text.find("\n## ")
    if next_section != -1:
        text = text[:next_section]
    return text.strip()


def _select_round_entries(
    store: refs.Store, round_number: int, gap_filter: str | None
) -> list[refs.Entry]:
    selected: list[refs.Entry] = []
    for entry in store["entries"].values():
        if entry.get("verification_status") != "verified":
            continue
        if entry.get("retracted"):
            continue
        if entry.get("excluded_reason"):
            continue
        if entry.get("added_round") != round_number:
            continue
        if gap_filter is not None and entry.get("gap") != gap_filter:
            continue
        selected.append(entry)
    selected.sort(
        key=lambda e: (e.get("year") or 0, (e.get("authors") or [""])[0])
    )
    return selected


def _write_gap_file(
    output_path: pathlib.Path,
    topic_name: str,
    round_number: int,
    gap_id: str,
    gap_description: str,
    entries: list[refs.Entry],
) -> None:
    header_lines = [
        f"# {topic_name} — Round {round_number} · {gap_id}",
        "",
        f"_{len(entries)} 条本轮新增 verified 文献_",
    ]
    if gap_description:
        header_lines.append(f"_gap: {gap_description}_")
    header_lines.append("")
    header = "\n".join(header_lines) + "\n"
    body = "\n".join(_entry_section(e, _read_abstract(e)) for e in entries)
    output_path.write_text(header + body, encoding="utf-8")


def _write_round_index(
    index_path: pathlib.Path,
    topic_name: str,
    round_number: int,
    gap_summaries: list[tuple[str, str, int, pathlib.Path]],
    orphan_count: int,
) -> None:
    """Tiny index pointing at the per-gap files. Built so an LLM can read
    just this 30-line file to know what to delegate to subagents, instead
    of pulling 7000 lines of abstracts into context."""
    total = sum(count for _, _, count, _ in gap_summaries) + orphan_count
    lines = [
        f"# {topic_name} — Round {round_number} 笔记索引",
        "",
        f"_本轮新增 verified 文献 {total} 条；按 gap 拆分到 round-{round_number}/_",
        "",
        "| gap | 条数 | 文件 |",
        "| --- | --- | --- |",
    ]
    for gap_id, _description, count, gap_path in gap_summaries:
        rel = gap_path.relative_to(index_path.parent).as_posix()
        lines.append(f"| {gap_id} | {count} | [{rel}]({rel}) |")
    if orphan_count:
        lines.append(
            f"| <no gap> | {orphan_count} | (跳过——orphan 条目未拆 gap 文件) |"
        )
    # Schema is the single copy in lib/analyst_prompt (see its docstring) —
    # do not re-inline it here, that is exactly the drift this fixes.
    # Pass round_number so the per-entry annotation step (schema item 7) cites
    # the concrete notes/round-N/<gap-id>.annotated.md path for this round.
    lines.append("")
    lines.extend(analyst_prompt.index_block(round_number=round_number))
    lines.append("")
    index_path.write_text("\n".join(lines), encoding="utf-8")


def _write_single_file(
    output_path: pathlib.Path,
    topic_name: str,
    round_number: int,
    gap_filter: str | None,
    entries: list[refs.Entry],
) -> None:
    header_scope = f"gap {gap_filter} · " if gap_filter else ""
    header = (
        f"# {topic_name} — Round {round_number} 笔记\n"
        f"\n_{header_scope}{len(entries)} 条本轮新增 verified 文献_\n\n"
    )
    body = "\n".join(_entry_section(e, _read_abstract(e)) for e in entries)
    output_path.write_text(header + body, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("topic_dir")
    parser.add_argument(
        "--round",
        dest="round_number",
        type=int,
        required=True,
        help="Round to scaffold notes for (matches added_round on entries).",
    )
    parser.add_argument(
        "--gap",
        metavar="GAP_ID",
        help=(
            "Limit to entries attached to this gap. Output goes to "
            "notes/round-N/<gap>.md (per-gap layout) unless --single-file."
        ),
    )
    parser.add_argument(
        "--single-file",
        action="store_true",
        help=(
            "Legacy layout: write one notes/round-N.md (or "
            "round-N-<gap>.md with --gap) instead of the per-gap "
            "directory. Useful for one-off small reviews; the default "
            "per-gap layout is the right unit for the Step 5 subagent "
            "workflow."
        ),
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    topic_dir = pathlib.Path(args.topic_dir)
    with testflight.timer(
        "notes",
        "main",
        topic_dir=topic_dir,
        round=args.round_number,
        gap=args.gap,
        single_file=args.single_file,
    ) as detail:
        store = refs.load(topic_dir)
        if store is None:
            print(f"[ERROR] missing references store: {topic_dir}")
            raise SystemExit(1)

        if args.gap is not None and args.gap not in store.get("gaps", {}):
            print(f"[ERROR] gap not declared: {args.gap}")
            raise SystemExit(1)

        notes_dir = topic_dir / "notes"
        notes_dir.mkdir(exist_ok=True)

        # --- Legacy single-file path ---------------------------------
        if args.single_file:
            suffix = f"-{args.gap}" if args.gap else ""
            output_path = notes_dir / f"round-{args.round_number}{suffix}.md"
            if output_path.exists() and not args.force:
                print(f"[ERROR] {output_path} exists; pass --force to overwrite")
                raise SystemExit(1)
            selected = _select_round_entries(store, args.round_number, args.gap)
            if not selected:
                print(
                    f"[WARN] no verified entries with added_round={args.round_number}"
                    + (f" gap={args.gap}" if args.gap else "")
                )
            _write_single_file(
                output_path, topic_dir.name, args.round_number, args.gap, selected
            )
            print(f"[OK] wrote {output_path} ({len(selected)} entries)")
            detail["entries"] = len(selected)
            return

        # --- Per-gap default path ------------------------------------
        round_dir = notes_dir / f"round-{args.round_number}"
        round_dir.mkdir(exist_ok=True)
        index_path = notes_dir / f"round-{args.round_number}.md"

        # Pre-flight: refuse to overwrite unless --force.
        targets: list[pathlib.Path] = []
        if index_path.exists():
            targets.append(index_path)
        if round_dir.exists():
            for child in round_dir.iterdir():
                if child.is_file():
                    targets.append(child)
        if targets and not args.force:
            print(f"[ERROR] existing notes files (pass --force to overwrite):")
            for path in targets:
                print(f"    {path}")
            raise SystemExit(1)

        gaps_meta = store.get("gaps", {})
        if args.gap is not None:
            target_gaps = [args.gap]
        else:
            # Cover all declared gaps, even ones with 0 round-N entries (so
            # the index is exhaustive). Callers usually only care about the
            # non-empty ones; we filter the index list below.
            target_gaps = list(gaps_meta.keys())

        gap_summaries: list[tuple[str, str, int, pathlib.Path]] = []
        total_entries = 0
        for gap_id in target_gaps:
            entries = _select_round_entries(store, args.round_number, gap_id)
            if not entries:
                continue
            gap_path = round_dir / f"{gap_id}.md"
            description = (gaps_meta.get(gap_id) or {}).get("description", "")
            _write_gap_file(
                gap_path,
                topic_dir.name,
                args.round_number,
                gap_id,
                description,
                entries,
            )
            gap_summaries.append((gap_id, description, len(entries), gap_path))
            total_entries += len(entries)
            print(f"[OK] wrote {gap_path} ({len(entries)} entries)")

        # Orphan entries (entry.gap not in declared gaps) — count for the
        # index but don't emit a per-gap file (lint_review will catch them).
        orphan_count = 0
        if args.gap is None:
            for entry in store["entries"].values():
                if entry.get("verification_status") != "verified":
                    continue
                if entry.get("retracted"):
                    continue
                if entry.get("excluded_reason"):
                    continue
                if entry.get("added_round") != args.round_number:
                    continue
                gap = entry.get("gap")
                if not isinstance(gap, str) or gap not in gaps_meta:
                    orphan_count += 1

        if not gap_summaries and orphan_count == 0:
            print(
                f"[WARN] no verified entries with added_round={args.round_number}"
                + (f" gap={args.gap}" if args.gap else "")
            )

        # Only write the global index when running across all gaps.
        if args.gap is None:
            _write_round_index(
                index_path,
                topic_dir.name,
                args.round_number,
                gap_summaries,
                orphan_count,
            )
            print(
                f"[OK] wrote {index_path} ({len(gap_summaries)} gaps, "
                f"{total_entries} entries)"
            )

        detail.update(
            {
                "entries": total_entries,
                "gaps_written": len(gap_summaries),
                "orphan": orphan_count,
            }
        )


if __name__ == "__main__":
    main()
