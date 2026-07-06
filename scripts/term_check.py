"""Tool-enforced termination checks for review topics.

This intentionally reads only the references store. The research log remains
human-facing context, not an input to the gate.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import pathlib
import sys
from typing import Literal

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib import config, patches, testflight
import refs
from refs import latest_round

TermStatus = Literal["not_ready", "saturated", "hard_stop"]


@dataclass(frozen=True)
class TermCheckResult:
    ok: bool
    status: TermStatus
    latest_round: int
    messages: list[str]

    @property
    def ok_for_writing(self) -> bool:
        return self.status in {"saturated", "hard_stop"}

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "ok": self.ok,
            "ok_for_writing": self.ok_for_writing,
            "latest_round": self.latest_round,
            "messages": self.messages,
        }


def _is_eligible(entry: refs.Entry) -> bool:
    return (
        entry.get("verification_status") == "verified"
        and not entry.get("retracted")
        and not entry.get("excluded_reason")
    )


def _gap_verified_entries(store: refs.Store, gap_id: str) -> list[refs.Entry]:
    return [
        entry
        for entry in store["entries"].values()
        if entry.get("gap") == gap_id and _is_eligible(entry)
    ]


def _gap_evidence_pool(store: refs.Store, gap_id: str) -> list[refs.Entry]:
    """Return the evidence pool for term_check on ``gap_id``: its own
    verified non-excluded entries, plus its parent gap's entries when
    this is a subgap (``subgap_of`` is set). The parent's pool is shared
    so subgaps don't need to re-collect ≥3 verified / RCT-meta supports
    on their own — they inherit from the parent's body of evidence."""
    pool_gap_ids = {gap_id}
    gap_meta = store.get("gaps", {}).get(gap_id, {})
    parent = gap_meta.get("subgap_of") if isinstance(gap_meta, dict) else None
    if isinstance(parent, str) and parent:
        pool_gap_ids.add(parent)
    return [
        entry
        for entry in store["entries"].values()
        if entry.get("gap") in pool_gap_ids and _is_eligible(entry)
    ]


def _is_rct_or_meta(entry: refs.Entry) -> bool:
    return entry.get("study_type") in {"rct", "meta"}


def evaluate_store(store: refs.Store) -> TermCheckResult:
    current_round = latest_round(store)
    # Addendum rounds — targeted single-gap additions to an ALREADY-saturated
    # topic, declared with `verify.py --declare-gap ... --addendum` — are not
    # counted against the lifetime hard cap and do not force an extra
    # consolidation round. This relaxes ONLY round-bookkeeping (the "new gap →
    # another round" blocker, the store-wide saturation ratio, and the hard
    # cap); every per-gap evidence floor below is still enforced, so an
    # addendum can never publish an under-evidenced gap. Default (no
    # addendum_rounds key) behaviour is unchanged.
    addendum_rounds = {
        int(r) for r in store.get("addendum_rounds", []) if isinstance(r, (int, float))
    }
    latest_is_addendum = current_round in addendum_rounds
    effective_round = current_round - len({r for r in addendum_rounds if r <= current_round})
    is_hard_stop = effective_round >= config.TERM_HARD_MAX_ROUNDS

    # Three channels with different force:
    #   blocking — per-gap / structural evidence floors. Enforced ALWAYS,
    #              including at hard_stop: hitting the round cap is not a
    #              licence to publish a review where a declared gap has
    #              almost no evidence. The author must add evidence, drop
    #              the gap, or merge it.
    #   waivable — round-count + saturation ratio. These say "keep
    #              expanding"; at the hard round cap there is nothing more
    #              to expand, so they are waived (the writer must instead
    #              disclose the limit in §限定与争议).
    #   advisory — WARN only; never flips ``ok``.
    blocking: list[str] = []
    waivable: list[str] = []
    advisory: list[str] = []

    if current_round < config.TERM_MIN_ROUNDS:
        waivable.append(
            f"[FAIL] latest_round={current_round} < min={config.TERM_MIN_ROUNDS}"
        )

    newly_created = [
        gap_id
        for gap_id, gap in sorted(store.get("gaps", {}).items())
        if gap.get("created_round", 0) >= current_round and current_round > 0
    ]
    if newly_created and not latest_is_addendum:
        blocking.append(
            "[FAIL] gap(s) created in latest round; another READ & NOTE round "
            f"required: {newly_created}"
        )

    # ≥1 RCT/meta per gap is only enforced if the patch (or default
    # health domain) requires it. Physics / food-science / animals
    # set ``term_check_overrides.require_rct_or_meta: false`` in their
    # patches/<domain>.md frontmatter — these domains don't routinely
    # produce RCT/meta and rely on different primary evidence types
    # (experimental_measurement, peer_replication, codex_standard, ...).
    # CLAUDE.md §Evidence hierarchy "where field has them" is enforced
    # via the patch frontmatter, not by guessing from the store.
    domain = store.get("domain", "health")
    patch = patches.load_patch(domain)
    require_rct_or_meta = patches.requires_rct_or_meta(patch)
    store_has_rct_or_meta = any(
        _is_eligible(entry) and _is_rct_or_meta(entry)
        for entry in store["entries"].values()
    )
    # When the domain requires RCT/meta but the store auto-classified ZERO,
    # the per-gap check below silently no-ops. That is usually a study_type
    # mis-classification (CrossRef drops cohorts/RCTs to "other"), not a
    # genuine absence — surface it as a WARN instead of passing silently so
    # the author can fix labels (verify.py --study-type) or disclose the gap.
    if require_rct_or_meta and not store_has_rct_or_meta:
        advisory.append(
            f"[WARN] domain={domain} 要求每个 gap 至少 1 条 RCT/meta，但全库自动分类为 0 条"
            " —— 多半是 study_type 未正确分类（cohort/RCT 常被 CrossRef 落到 'other'）。"
            "用 `verify.py --add ... --study-type {rct|meta|cohort|...}` 手动校正，"
            "或在 §限定与争议 写明该证据不足。"
        )

    for gap_id in sorted(store.get("gaps", {})):
        entries = _gap_evidence_pool(store, gap_id)
        if len(entries) < config.TERM_MIN_VERIFIED_PER_GAP:
            blocking.append(
                f"[FAIL] {gap_id} verified={len(entries)} "
                f"< min={config.TERM_MIN_VERIFIED_PER_GAP}"
            )
        # The RCT/meta floor only fits gaps that ask "does X work / which is
        # better" — decision & comparison. diagnostic / safety / mechanism /
        # descriptive / methodology legitimately rest on cohort, case-series,
        # cross-sectional or mechanistic evidence and must not be forced to an
        # RCT/meta the question type doesn't produce. Untyped (legacy) gaps keep
        # the old strict behavior.
        gap_meta = store.get("gaps", {}).get(gap_id, {})
        gap_type = gap_meta.get("gap_type") if isinstance(gap_meta, dict) else None
        floor_applies = gap_type in {"decision", "comparison", None}
        if require_rct_or_meta and store_has_rct_or_meta and floor_applies:
            has_rct_or_meta = any(_is_rct_or_meta(entry) for entry in entries)
            if not has_rct_or_meta:
                blocking.append(
                    f"[FAIL] {gap_id} has no RCT/meta evidence "
                    f"(gap_type={gap_type}; domain={domain} requires it for "
                    "decision/comparison gaps — other gaps here have RCT/meta)"
                )

    eligible_entries = [entry for entry in store["entries"].values() if _is_eligible(entry)]
    if not eligible_entries:
        blocking.append("[FAIL] no verified non-excluded entries")
    else:
        latest_entries = [
            entry
            for entry in eligible_entries
            if entry.get("added_round") == current_round
        ]
        ratio = len(latest_entries) / len(eligible_entries)
        if ratio > config.TERM_SATURATION_RATIO and not latest_is_addendum:
            waivable.append(
                f"[FAIL] latest round added {len(latest_entries)}/{len(eligible_entries)} "
                f"verified entries ({ratio:.1%}) > saturation={config.TERM_SATURATION_RATIO:.0%}"
            )

    effective_fails = blocking if is_hard_stop else blocking + waivable
    if effective_fails:
        return TermCheckResult(
            ok=False,
            status="not_ready",
            latest_round=current_round,
            messages=effective_fails + advisory,
        )
    if is_hard_stop:
        messages = [
            f"[OK] hard_stop: latest_round={current_round} reached hard max "
            f"{config.TERM_HARD_MAX_ROUNDS}; per-gap evidence floors met. 停止扩库，"
            "但必须在 research_log.md 与 review.md §限定与争议 写明轮次上限 / 剩余证据不足。"
        ]
        if waivable:
            messages.append("[INFO] waived at hard_stop: " + "; ".join(waivable))
        return TermCheckResult(
            ok=True,
            status="hard_stop",
            latest_round=current_round,
            messages=messages + advisory,
        )
    return TermCheckResult(
        ok=True,
        status="saturated",
        latest_round=current_round,
        messages=[f"[OK] termination conditions satisfied at round {current_round}"]
        + advisory,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check whether a review topic satisfies tool-enforced termination conditions."
    )
    parser.add_argument("topic_dir")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable status JSON instead of text lines.",
    )
    args = parser.parse_args()

    topic_dir = pathlib.Path(args.topic_dir)
    with testflight.timer("term_check", "main", topic_dir=topic_dir) as detail:
        store = refs.load(topic_dir)
        if store is None:
            message = f"[ERROR] no references store under {topic_dir}"
            if args.json:
                print(
                    json.dumps(
                        {
                            "status": "not_ready",
                            "ok": False,
                            "ok_for_writing": False,
                            "latest_round": 0,
                            "messages": [message],
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                print(message)
            raise SystemExit(1)
        result = evaluate_store(store)
        if args.json:
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        else:
            for message in result.messages:
                print(message)
        detail.update(
            {
                "ok": result.ok,
                "status": result.status,
                "latest_round": result.latest_round,
            }
        )
        raise SystemExit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
