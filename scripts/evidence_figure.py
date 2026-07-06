"""tools/evidence_figure.py — generate a GROUNDED forest plot from the validated
evidence_table (v3.2 C19 / spec §0.6.l).

Opt-in: the writer (or main) runs this AFTER evidence_extract --validate. It plots the
effect + 95% CI of every evidence_table row that has a clean point estimate and CI band
(uncertain / unparseable rows are skipped and reported), writes the SVG to
``figures/forest_evidence.svg``, and prints a ready-to-paste Markdown block:

  · the figure reference line carries a ``type:inference`` claim_id (it merely visualises
    grounded data — the §0.6.l write_gate requires a sidecar on the figure line);
  · a data table where every plotted number sits on a ``[@key]`` row, so faithfulness
    validates those numbers against source (the figure can't fabricate a value the table
    doesn't ground). caption lists the source DOIs.

It NEVER edits review.md and is never on the write_gate path — a bad auto-figure can't
block delivery; the writer pastes the block only if it helps. Delivery inlines the SVG
via md_to_html.

    python tools/evidence_figure.py reviews/<topic> [--min-rows 2]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import faithfulness
import refs
from lib import layout


def _point_and_ci(row: dict) -> tuple[float, float, float] | None:
    """(point, lo, hi) from a row's effect + ci, or None if not a clean single
    estimate with a 2-number CI (uncertain fields are excluded upstream)."""
    if row.get("effect") == "uncertain" or row.get("ci") == "uncertain":
        return None
    eff = faithfulness._extract_numbers(str(row.get("effect") or ""))
    ci = faithfulness._extract_numbers(str(row.get("ci") or ""))
    if len(eff) != 1 or len(ci) != 2:
        return None
    try:
        point, lo, hi = float(eff[0]), float(ci[0]), float(ci[1])
    except ValueError:
        return None
    if lo > hi:
        lo, hi = hi, lo
    if not (lo <= point <= hi):
        return None  # point estimate must sit inside its own CI
    return point, lo, hi


def build(topic_dir: pathlib.Path, min_rows: int) -> tuple[pathlib.Path | None, str]:
    store = refs.load(topic_dir)
    table_path = topic_dir / layout.META_DIRNAME / "evidence_table.json"
    if store is None or not table_path.exists():
        return None, "[evidence_figure] need a store + meta/evidence_table.json (run evidence_extract --validate first)"
    table = json.loads(table_path.read_text(encoding="utf-8"))

    plotted: list[tuple[str, str, float, float, float]] = []  # key, doi, point, lo, hi
    skipped: list[str] = []
    for key, row in table.items():
        if not isinstance(row, dict):
            continue
        pci = _point_and_ci(row)
        if pci is None:
            skipped.append(key)
            continue
        doi = refs.resolve_citation_key(store, key) or "?"
        plotted.append((key, doi, *pci))

    if len(plotted) < min_rows:
        return None, (
            f"[evidence_figure] only {len(plotted)} row(s) with a clean effect+CI "
            f"(need ≥{min_rows}); no figure made. skipped: {', '.join(skipped[:8]) or '—'}"
        )

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plotted.sort(key=lambda r: r[2])
    labels = [r[0] for r in plotted]
    points = [r[2] for r in plotted]
    los = [r[3] for r in plotted]
    his = [r[4] for r in plotted]
    ys = list(range(len(plotted)))

    fig, ax = plt.subplots(figsize=(7, 0.5 * len(plotted) + 1.5))
    ax.errorbar(
        points, ys,
        xerr=[[p - lo for p, lo in zip(points, los)], [hi - p for p, hi in zip(points, his)]],
        fmt="o", color="#2b6cb0", ecolor="#718096", capsize=3, linewidth=1.2,
    )
    ax.set_yticks(ys)
    ax.set_yticklabels(labels, fontsize=8)
    ax.axvline(1.0, color="#a0aec0", linestyle="--", linewidth=0.8)  # null line (ratio measures)
    # Keep in-SVG text ASCII (the default matplotlib font lacks CJK glyphs → tofu boxes);
    # the Chinese caption lives in the Markdown block, not the figure.
    ax.set_xlabel("Effect size (point estimate, 95% CI)")
    ax.set_title("Evidence forest plot (values from validated evidence_table)", fontsize=10)
    fig.tight_layout()

    fig_dir = topic_dir / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    out = fig_dir / "forest_evidence.svg"
    fig.savefig(out, format="svg")
    plt.close(fig)

    rel = out.relative_to(topic_dir).as_posix()
    dois = "；".join(sorted({r[1] for r in plotted}))
    lines = [
        f"![森林图：各研究效应量与 95% CI]({rel}) <!-- claim:figForest type:inference -->",
        "",
        f"**图 1.** 森林图汇总下列研究的效应量（点估计 + 95% CI），数值取自各被引文献已校验原文（evidence_table）。来源 DOI：{dois}。",
        "",
        "| 研究 | 效应量 | 95% CI |",
        "| --- | --- | --- |",
    ]
    for i, (key, _doi, point, lo, hi) in enumerate(plotted, 1):
        lines.append(f"| [@{key}] | {point:g} | {lo:g}–{hi:g} | <!-- claim:figRow{i} -->")
    block = "\n".join(lines)
    return out, block


def main() -> None:
    parser = argparse.ArgumentParser(description="evidence_figure — grounded forest plot from evidence_table (C19).")
    parser.add_argument("topic_dir")
    parser.add_argument("--min-rows", type=int, default=2, help="Minimum clean effect+CI rows to plot (default 2).")
    args = parser.parse_args()
    out, block = build(pathlib.Path(args.topic_dir), args.min_rows)
    if out is None:
        print(block, file=sys.stderr)
        raise SystemExit(1)
    print(f"[evidence_figure] wrote {out}", file=sys.stderr)
    print(block)
    raise SystemExit(0)


if __name__ == "__main__":
    main()
