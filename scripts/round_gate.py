"""tools/round_gate.py — the 轮转闸 (workflow_spec §1 N5, §4; plan v3.1 G1).

HARD-BLOCK gate run before opening round R+1 (round-loop) and at session end
(Stop hook). It checks that the *just-closed* round(s) were properly wound down
— pruned, analyst-judged, weak evidence quarantined rather than deleted — so a
手驱动 shortcut can't skip straight to the next round. It does NOT look at
claim_map / faithfulness (those don't exist until writing; that is write_gate).

    python tools/round_gate.py reviews/<topic> [--before-round N] [--json]
    # exit 0 if not blocked, 1 if any check FAILED, 2 on bad topic path.

A round R is "closed" once a later round exists OR review.md exists; the latest
in-progress round is not yet enforced (its analyst/prune may legitimately still
be pending this turn). ``--before-round N`` gates the N-1 → N transition,
checking only round N-1.

Check states (see lib.gatelib): pass / fail / pending. ``pending`` marks checks
whose enforcement waits on an unbuilt feature (quarantine = R2, recall audit =
R4); they are reported but do not block, and flip to fail/pass when that feature
lands — no rewiring (plan v3.1 §5).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import refs
import term_check
from lib import gatelib, layout

# A round with this many new eligible entries MUST have an analyst pass
# (CLAUDE.md 纪律 H / plan v3.1 G5 #5). Mirrors the Stop-hook default.
ANALYST_MIN_NEW = 5


def _is_eligible(entry: refs.Entry) -> bool:
    return (
        entry.get("verification_status") == "verified"
        and not entry.get("retracted")
        and not entry.get("excluded_reason")
    )


def _new_eligible_by_gap(store: refs.Store, round_n: int) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in store.get("entries", {}).values():
        if entry.get("added_round") != round_n:
            continue
        if not _is_eligible(entry):
            continue
        gap = entry.get("gap")
        if isinstance(gap, str):
            counts[gap] = counts.get(gap, 0) + 1
    return counts


def _closed_rounds(
    store: refs.Store, topic_dir: pathlib.Path, before_round: int | None
) -> list[int]:
    latest = term_check.latest_round(store)
    if before_round is not None:
        prev = before_round - 1
        return [prev] if prev >= 1 else []
    review_exists = (topic_dir / "review.md").exists()
    if latest < 1:
        return []
    # the latest round is in-progress unless writing has started
    cutoff = latest if review_exists else latest - 1
    return list(range(1, cutoff + 1))


def _quarantine_available(topic_dir: pathlib.Path) -> bool:
    return (topic_dir / layout.META_DIRNAME / "quarantine.jsonl").exists()


# ── checks (each → CheckResult) ───────────────────────────────────────────────


def _check_analyst_per_round(
    store: refs.Store, topic_dir: pathlib.Path, log_text: str, rounds: list[int]
) -> gatelib.CheckResult:
    name = "analyst-per-round"
    offenders: list[str] = []
    for round_n in rounds:
        for gap_id, count in sorted(_new_eligible_by_gap(store, round_n).items()):
            if count < ANALYST_MIN_NEW:
                continue
            annotated = topic_dir / "notes" / f"round-{round_n}" / f"{gap_id}.annotated.md"
            if annotated.exists():
                continue
            if gatelib.has_skip_analyst(log_text, gap_id, round_n):
                continue
            offenders.append(f"round {round_n} {gap_id} (+{count} new, no annotated.md / skip note)")
    if offenders:
        return gatelib.failed(
            name,
            "round(s) with ≥%d new verified but no analyst annotated.md or skip "
            "declaration: %s" % (ANALYST_MIN_NEW, "; ".join(offenders)),
        )
    return gatelib.passed(name, "every ≥%d-new closed round has analyst/skip" % ANALYST_MIN_NEW)


def _check_prune_recorded(
    store: refs.Store, log_text: str, rounds: list[int]
) -> gatelib.CheckResult:
    name = "prune-recorded"
    latest = term_check.latest_round(store)
    # only required once at least one R→R+1 transition has happened
    if latest < 2:
        return gatelib.passed(name, "no R→R+1 transition yet — prune not required")
    if gatelib.has_exclusion_audit(log_text) or gatelib.has_no_prune_marker(log_text):
        return gatelib.passed(name, "exclusion audit log present or no-prune declared")
    return gatelib.failed(
        name,
        "latest_round=%d but research_log has no '## Exclusion audit log' entries "
        "and no '本轮无需 prune' declaration (spec N5 / G5 #6)" % latest,
    )


def _check_weak_not_excluded(
    store: refs.Store, topic_dir: pathlib.Path, legacy: bool
) -> gatelib.CheckResult:
    name = "weak-not-excluded"
    weak = {"title_only", "abstract"}
    offenders = [
        key
        for key, entry in store.get("entries", {}).items()
        if entry.get("excluded_reason") and refs.grounding(entry) in weak
    ]
    if not offenders:
        return gatelib.passed(name, "no abstract/title-only entry permanently excluded")
    if legacy:
        return gatelib.pending(name, f".gates_legacy — {len(offenders)} weak-excluded grandfathered")
    return gatelib.failed(
        name,
        "abstract/title-only entries permanently excluded (must quarantine instead, "
        "spec N4): %s" % ", ".join(sorted(offenders)),
    )


def _check_quarantine_present(
    topic_dir: pathlib.Path, store: refs.Store, legacy: bool
) -> gatelib.CheckResult:
    """C2 隔离池 (spec §0.4) is mandatory once rounds have run — genealogy's relevance
    gate writes meta/quarantine.jsonl (empty if nothing rejected) when --relevance-terms
    is used, so its absence on a round-based topic means C2 was not run."""
    name = "quarantine-present"
    if _quarantine_available(topic_dir):
        return gatelib.passed(name, "meta/quarantine.jsonl present (C2 ran)")
    if legacy or term_check.latest_round(store) < 1:
        return gatelib.pending(name, "meta/quarantine.jsonl absent (legacy / no rounds yet)")
    return gatelib.failed(
        name,
        "meta/quarantine.jsonl absent on a round-based topic — C2 隔离池 not run "
        "(genealogy needs --relevance-terms; spec §0.4); or add .gates_legacy",
    )


def _check_recall_audit(
    topic_dir: pathlib.Path, store: refs.Store, log_text: str, legacy: bool
) -> gatelib.CheckResult:
    """Recall audit (spec §0.4) is mandatory per topic. M10: read the artifact, don't
    just check existence — a ``seed-entries`` fallback audit (hit_rate≈1.0 by
    construction, ZERO detection power) must not silently satisfy the gate; it is
    surfaced as ``pending`` (incomplete) rather than counted as a real known-item test."""
    name = "recall-audit"
    meta = topic_dir / layout.META_DIRNAME
    json_path = meta / "recall_audit.json"
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        source = data.get("known_source")
        hit_rate = data.get("hit_rate")
        if source == "known_items.txt":
            # R2-F10: a known landmark sitting in quarantine is a C2 false-drop — the
            # audit's highest-priority signal — and is mechanically recoverable. FAIL.
            quarantined = data.get("misses_in_quarantine") or []
            if not isinstance(quarantined, list):  # fail-soft on a malformed artifact (matches the corrupt-JSON branch)
                quarantined = []
            if quarantined and not legacy:
                return gatelib.failed(
                    name,
                    "召回审计发现已知 landmark 被 C2 误丢进 quarantine（可从隔离池恢复，spec §0.4 "
                    "『省钱不得以无审计召回损失为代价』）: " + ", ".join(str(x) for x in quarantined[:5]),
                )
            return gatelib.passed(name, f"recall-audit ran on known_items.txt (hit_rate={hit_rate})")
        # seed-fallback / unknown source → real detection power not established
        if legacy:
            return gatelib.pending(name, f".gates_legacy — recall-audit source={source!r}")
        return gatelib.pending(
            name,
            f"recall-audit ran but source={source!r}（seed-fallback 零检测力）—真召回审计需 "
            "meta/known_items.txt（spec §0.4 known-item 命中测试）",
        )
    if (meta / "recall_audit.md").exists() or gatelib.has_recall_audit_marker(log_text):
        return gatelib.passed(name, "recall-audit artifact/marker present")
    if legacy or term_check.latest_round(store) < 1:
        return gatelib.pending(name, "no recall-audit yet (legacy / no rounds)")
    return gatelib.failed(
        name,
        "no recall-audit artifact/marker on a round-based topic — run recall_audit.py "
        "(known-item / forward-citation 命中测试, spec §0.4); or add .gates_legacy",
    )


def _check_term(store: refs.Store) -> gatelib.CheckResult:
    """spec N5 'term 已就位': term_check must produce a status."""
    name = "term-check"
    try:
        status = term_check.evaluate_store(store).to_dict().get("status")
    except Exception as exc:  # noqa: BLE001 — any malformed-store error → fail
        return gatelib.failed(name, f"term_check did not evaluate: {exc}")
    if status in ("not_ready", "saturated", "hard_stop"):
        return gatelib.passed(name, f"term_check evaluable (status={status})")
    return gatelib.failed(name, f"term_check produced no valid status (got {status!r})")


def gate(topic_dir: pathlib.Path, before_round: int | None = None) -> list[gatelib.CheckResult]:
    store = refs.load(topic_dir)
    if store is None:
        return [gatelib.failed("store", f"no references store under {topic_dir}")]
    legacy = (topic_dir / ".gates_legacy").exists()
    rounds = _closed_rounds(store, topic_dir, before_round)
    log_text = gatelib.read_text(topic_dir / "research_log.md")
    results = [
        _check_analyst_per_round(store, topic_dir, log_text, rounds),
        _check_prune_recorded(store, log_text, rounds),
        _check_weak_not_excluded(store, topic_dir, legacy),
        _check_quarantine_present(topic_dir, store, legacy),
        _check_recall_audit(topic_dir, store, log_text, legacy),
        _check_term(store),
    ]
    # R2-F5: `.gates_legacy` grandfathers the WHOLE gate uniformly (matches Stop hook).
    return gatelib.grandfather(results) if legacy else results


def main() -> None:
    parser = argparse.ArgumentParser(description="round_gate — 轮转硬阻断闸 (spec N5 / G1).")
    parser.add_argument("topic_dir")
    parser.add_argument(
        "--before-round",
        type=int,
        default=None,
        help="Gate the N-1→N transition (checks round N-1 only).",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    topic_dir = pathlib.Path(args.topic_dir)
    if not topic_dir.is_dir():
        print(f"[ERROR] not a topic dir: {topic_dir}", file=sys.stderr)
        raise SystemExit(2)

    results = gate(topic_dir, before_round=args.before_round)
    summary = gatelib.summarize(results)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"round_gate: {'BLOCKED' if summary['blocked'] else 'ok'}")
        for result in results:
            mark = {"pass": "✓", "fail": "✗", "pending": "…"}[result.status]
            print(f"  {mark} [{result.status}] {result.name}: {result.message}")
    raise SystemExit(1 if summary["blocked"] else 0)


if __name__ == "__main__":
    main()
