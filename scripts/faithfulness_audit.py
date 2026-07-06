"""tools/faithfulness_audit.py — sample faithful verdicts for human recheck (F13 / spec §0.6).

Symmetric to recall_audit (§0.4 召回审计 ↔ §0.6 忠实度审计). The entailment judge —
even the deterministic + LLM ensemble — has a false-NEGATIVE floor: it can pass a
claim that is actually misgrounded. So the 招牌属性 (正面蕴含判定) must not be the
only LLM step that is never audited. Periodically a human rechecks a SAMPLE of
``faithful`` verdicts (high-risk first), and failures backfill the judge prompt +
(b2)/(b3) pre-filters.

This tool produces the deterministic SAMPLE + a recheck checklist
(``meta/faithfulness_audit.md``); the human fills the verdicts. No LLM here.

    python tools/faithfulness_audit.py reviews/<topic> [--k 8]
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import faithfulness
import refs
from lib import layout


def sample(results: list["faithfulness.ClaimVerdict"], k: int = 8) -> list:
    """Deterministic sample of the LLM-JUDGED faithful verdicts, high-risk first.
    The sampling frame must equal the population the entailment judge ruled on
    (= write_gate's `_needs_judge` set: faithful + high-risk/cited-factual), so the
    audit measures THAT judge's error rate (spec §0.6.i). Priority: high-risk first,
    then cross-language / coarse-span samples (false-POSITIVE axis, S1)."""
    primary = [cv for cv in results if faithfulness._needs_judge(cv)]
    primary.sort(key=lambda cv: (cv.risk != "high", not _crosslang_or_fallback(cv), cv.key, cv.sentence[:40]))
    # Reserve a slice for EXEMPT-label faithful clauses (inference/research_log/…) that are NOT in
    # _needs_judge: a clause the empirical-predicate lexicon MISSED (a transcribed finding wrongly
    # classified non-empirical) would otherwise never be human-rechecked. This is the §0.6.i backstop
    # for the lexicon-completeness §目标边界 residual — so 'residuals are audited' is actually true.
    exempt = [cv for cv in results
              if cv.verdict == "faithful" and cv.key_type in faithfulness._LOG_KEY_TYPES
              and not faithfulness._needs_judge(cv)]
    exempt.sort(key=lambda cv: (cv.key, cv.sentence[:40]))
    n_exempt = min(len(exempt), max(1, k // 4)) if exempt else 0
    return primary[:k - n_exempt] + exempt[:n_exempt] if n_exempt else primary[:k]


def _crosslang_or_fallback(cv: "faithfulness.ClaimVerdict") -> bool:
    """§0.6.i (S1): the highest false-POSITIVE-risk samples — a CJK claim entailed by an
    English span (cross-language entailment can wrongly pass), or a claim resting on a coarse
    abstract_fallback / paragraph_entail span. Sampled first so the audit catches 'faithful
    错放' (the main cross-language risk), not only the false-negative axis."""
    if cv.span_source in ("abstract_fallback", "paragraph_entail"):
        return True
    claim = cv.atomic_claim or cv.sentence or ""
    span = (cv.span or "").strip()
    has_cjk = any("一" <= c <= "鿿" for c in claim)
    span_ascii = bool(span) and span != "-" and not any("一" <= c <= "鿿" for c in span)
    return has_cjk and span_ascii


def render_checklist(sampled: list) -> str:
    lines = [
        "# Faithfulness audit — human recheck sample (F13)",
        "",
        "抽样 `faithful` 判定（高风险优先，跨语言/兜底 span 次优先）人工复核蕴含 judge "
        "假阴**与假阳**（spec §0.6.i / S1：英文 span ⊨ 中文断言的错放是跨语言主风险）。逐条核对"
        "被引原文是否**正面支撑**该断言（来源沉默/范围蔓延=应判 not-faithful），在 "
        "`human_verdict` 填 `ok` / `miss`；`miss` 样本回填 judge prompt。",
        "",
        "| key | risk | grounding | span_section | span_source | atomic_claim | human_verdict |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for cv in sampled:
        claim = (cv.atomic_claim or cv.sentence)[:70].replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {cv.key} | {cv.risk} | {cv.grounding} | {cv.span_section or '-'} | "
            f"{cv.span_source} | {claim} |  |"
        )
    lines += ["", f"_sample size: {len(sampled)} faithful verdict(s)_", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="faithfulness_audit — sample faithful verdicts (F13).")
    parser.add_argument("topic_dir")
    parser.add_argument("--k", type=int, default=8)
    args = parser.parse_args()

    topic_dir = pathlib.Path(args.topic_dir)
    store = refs.load(topic_dir)
    review_path = topic_dir / "review.md"
    if store is None or not review_path.exists():
        print(f"[ERROR] need store + review.md under {topic_dir}", file=sys.stderr)
        raise SystemExit(1)

    # pass topic_dir so the sample's verdicts reflect the SAME blind-spot flags
    # (true annotated-set count etc.) write_gate sees — not the whole-gap fallback.
    results = faithfulness.evaluate(store, review_path.read_text(encoding="utf-8"), topic_dir=topic_dir)
    sampled = sample(results, args.k)
    meta = topic_dir / layout.META_DIRNAME
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "faithfulness_audit.md").write_text(render_checklist(sampled), encoding="utf-8")
    print(
        f"[faithfulness_audit] sampled {len(sampled)} faithful verdict(s) "
        f"(of {sum(1 for cv in results if cv.verdict == 'faithful')}) → "
        f"meta/faithfulness_audit.md — fill human_verdict, backfill judge on 'miss'"
    )
    raise SystemExit(0)


if __name__ == "__main__":
    main()
