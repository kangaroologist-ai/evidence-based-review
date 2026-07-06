"""tools/conductor.py — the mechanical phase-gate for a review (conductor_spec §6).

STATELESS: derives the whole state from on-disk artifacts via
workflow_status.build_status() (no state file), then emits, at the current
FRONTIER (the first phase boundary not yet cleared):

  - gate     : BLOCKED (frontier not met) or done (all cleared)
  - blocking : the specific MECHANICAL items still missing
  - next     : a one-line human-readable next action
  - params   : ready-made data for that action so the main agent doesn't have to
               reassemble it — round gap list / writing brief / rework brief

It runs the SAME checks the Stop hook enforces (lint exit code, term_check via
build_status, reviewer tally via build_status), so the proactive gate and the
end-gate never drift. It is the mechanical control spine (conductor_spec §4);
ALL judgment (seeds, pruning, saturation, 立意, writing, rework) stays with the
main agent. It never spawns agents or mutates the store — its subprocesses are
read-only lint_review.py plus, in the write/final gate, write_gate.py (which
regenerates the derived faithfulness_report + claim_evidence_map artifacts; spec
N6/N11 require the write gate consume faithfulness, so the conductor runs it).

    python tools/conductor.py reviews/<topic> [--json]
    # exit 0 if a frontier action is available (gate BLOCKED or done),
    # exit 2 only on a bad topic path.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import refs
import research_log
import term_check
import workflow_status
from lib import annotated

_HERE = pathlib.Path(__file__).parent


def _topic_ref(topic_dir: pathlib.Path) -> str:
    return f"reviews/{topic_dir.name}"


def _run_lint(topic_dir: pathlib.Path) -> tuple[int, str]:
    """Run the read-only linter; return (exit_code, tail). exit 0 = clean,
    2 = WARN-only (both acceptable), 1 / other = FAIL."""
    proc = subprocess.run(
        [sys.executable, str(_HERE / "lint_review.py"), _topic_ref(topic_dir)],
        cwd=str(_HERE.parent),
        capture_output=True,
        text=True,
        env={**os.environ, "HEALTH_REVIEW_DAEMON": "0"},
    )
    tail = (proc.stdout + proc.stderr).strip().splitlines()
    return proc.returncode, "\n".join(tail[-6:])


# ── gate evaluators (each → (met: bool, blocking: list[str])) ────────────────


def _gate_build(status: dict[str, Any]) -> tuple[bool, list[str]]:
    if not status.get("store_exists"):
        return False, ["no references store (bootstrap + declare Round-1 gaps)"]
    gaps = status.get("gaps") or {}
    if (gaps.get("total") or 0) < 1:
        return False, ["no declared gap (do Phase-1 gap alignment, then verify --declare-gap)"]
    return True, []


def _gate_rounds(status: dict[str, Any]) -> tuple[bool, list[str]]:
    blocking: list[str] = []
    term = status.get("term_check") or {}
    if not term.get("ok_for_writing"):
        msgs = [m for m in term.get("messages", []) if "[OK]" not in m]
        blocking.append(f"term_check={term.get('status')} (not saturated/hard_stop)")
        blocking.extend(f"  {m}" for m in msgs[:4])
    missing_gen = (status.get("round") or {}).get("missing_genealogy_rounds") or []
    if missing_gen:
        blocking.append(f"rounds missing a genealogy entry: {missing_gen}")
    return (not blocking), blocking


def _run_write_gate(topic_dir: pathlib.Path) -> tuple[int, str]:
    """Run write_gate (it regenerates faithfulness_report + claim_evidence_map, so
    unlike lint it is not purely read-only — but those artifacts are derived and
    idempotent). exit 0 ok / 1,3 blocked / 2 bad path (no review yet → skip)."""
    proc = subprocess.run(
        [sys.executable, str(_HERE / "write_gate.py"), _topic_ref(topic_dir)],
        cwd=str(_HERE.parent),
        capture_output=True,
        text=True,
        env={**os.environ, "HEALTH_REVIEW_DAEMON": "0"},
    )
    tail = (proc.stdout + proc.stderr).strip().splitlines()
    return proc.returncode, "\n".join(tail[-8:])


def _gate_write(status: dict[str, Any], topic_dir: pathlib.Path) -> tuple[bool, list[str]]:
    blocking: list[str] = []
    render = status.get("render_refs") or {}
    if not render.get("rendered"):
        blocking.append("review.md not rendered (no PRISMA flow / References — write + render_refs)")
    lint_rc, lint_tail = _run_lint(topic_dir)
    if lint_rc not in (0, 2):
        blocking.append(f"lint_review FAIL (exit {lint_rc}):\n{_indent(lint_tail)}")
    # write_gate (spec N6/N11): faithfulness suspect=insufficient=0 + claim-map +
    # high-risk grounding + evidence-uncertain + cross-gap + metadata. Only once a
    # review exists (rendered); rc 2 = no review / bad path → skip.
    if render.get("rendered"):
        wg_rc, wg_tail = _run_write_gate(topic_dir)
        if wg_rc not in (0, 2):
            blocking.append(f"write_gate BLOCKED (exit {wg_rc}):\n{_indent(wg_tail)}")
    return (not blocking), blocking


def _gate_review(status: dict[str, Any]) -> tuple[bool, list[str]]:
    rv = status.get("reviewer") or {}
    if rv.get("approved"):
        return True, []
    st = rv.get("status")
    if st == "not_started":
        return False, ["reviewer panel not started (3 independent Opus reviewers, Round 1)"]
    if st == "missing_reviewers":
        return False, [f"missing reviewer(s) {rv.get('missing_reviewers')} this round"]
    if st == "request_changes":
        return False, [f"reviewer request_changes ({rv.get('approve_count')}/3 approve) — revise + re-review"]
    return False, [f"reviewer status={st} (not 3/3 approve)"]


def _run_recheck(topic_dir: pathlib.Path) -> tuple[int, str]:
    """M2 (spec N11): realtime retraction/EoC recheck against Crossref/PubMed (not
    the N3 cache). recheck.py exit: 0 clean / 1 warnings-or-retracted-or-failed /
    2 error. Network — only run at the explicit finalize step (env-gated below)."""
    proc = subprocess.run(
        [sys.executable, str(_HERE / "recheck.py"), _topic_ref(topic_dir)],
        cwd=str(_HERE.parent),
        capture_output=True,
        text=True,
        env={**os.environ, "HEALTH_REVIEW_DAEMON": "0"},
    )
    tail = (proc.stdout + proc.stderr).strip().splitlines()
    return proc.returncode, "\n".join(tail[-8:])


def _gate_final(
    write_result: tuple[bool, list[str]], topic_dir: pathlib.Path
) -> tuple[bool, list[str]]:
    # Final gate = the write-gate checks (passed in, reusing the cached run) …
    met, blocking = write_result
    blocking = list(blocking)
    # … PLUS, at the explicit finalize step (HEALTH_REVIEW_FINALIZE=1, set by
    # tools/finalize.py — not on every conductor consult, which would pay network
    # each turn), a realtime retraction recheck (M2 / spec §0.6.k / N11).
    if os.environ.get("HEALTH_REVIEW_FINALIZE") == "1":
        rc, tail = _run_recheck(topic_dir)
        if rc == 1 and "retract" in tail.lower():
            blocking.append(f"finalize recheck found newly-retracted cite(s) (M2):\n{_indent(tail)}")
            met = False
    return met, blocking


def _indent(text: str) -> str:
    return "\n".join("    " + line for line in text.splitlines())


# ── params (ready-made data for the frontier's unblock action) ───────────────


def _gap_specs(store: refs.Store) -> list[dict[str, Any]]:
    # Keys match round.js's operator contract (id/desc/gapType/query/cap) so the
    # main agent can pass them straight to round-loop. These gaps are ALREADY
    # declared+seeded (rounds have started), so round.js skips declare/seed for
    # them — subfields/seeds aren't needed here and are omitted on purpose
    # (re-declaring with empty subfields would clobber the gap's fields).
    return [
        {
            "id": gid,
            "desc": gap.get("description", ""),
            "gapType": gap.get("gap_type"),
            # Prefer the gap's curated spec-N2 `query` (English search angle, no
            # `[gap_type]` prefix); fall back to description only when unset. The
            # bare description carries the `[gap_type]` tag + is the topic's native
            # language, so emitting it as the search query degrades recall / adds
            # cross-domain noise (see testflight: desc-as-query failure mode).
            "query": gap.get("query") or gap.get("description", ""),  # refine per round
            "relevance_terms": gap.get("relevance_terms", ""),
            "cap": 15,
            "status": gap.get("status", "pending"),
            "created_round": gap.get("created_round", 0),
        }
        for gid, gap in sorted((store.get("gaps") or {}).items())
    ]


def _rounds_params(status: dict[str, Any], store: refs.Store) -> dict[str, Any]:
    latest = (status.get("round") or {}).get("latest_round") or 0
    return {
        "next_round": latest + 1,
        "gaps": _gap_specs(store),
        "hint": "these declared gaps are round-loop-ready (id/desc/gapType/query/cap); "
        "REFINE each gap's `query` to this round's search angle before invoking "
        "Workflow({name:'round-loop', args:{topicDir, gaps}}). round.js skips "
        "re-declaring/re-seeding them (already done), so no subfields/seeds needed.",
    }


def _parse_cite_rows(path: pathlib.Path) -> list[dict[str, str]]:
    """Like research_log._parse_annotated but keeps the key NUMBER + POINT cells
    (annotated rows: ``| [@key] | study_type | 关键数字 | cite — 一句论点 |``), so
    the writing brief carries the §2-mandated number + point, not just the key."""
    rows: list[dict[str, str]] = []
    if not path.exists():
        return rows
    text = path.read_text(encoding="utf-8")
    seen: set[str] = set()
    # pipe-table rows carry the §2-mandated number + one-line point
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        key_cell, study_type, number, verdict_cell = cells[0], cells[1], cells[2], cells[3]
        match = research_log._KEY_RE.search(key_cell)
        if not match or "见上" in key_cell:
            continue
        # C11/R2: cite-inclusive verdict resolution (handles reason-first '不该 exclude，故 cite'
        # and Chinese 引用), so a cite row keeps its number/point instead of being dropped.
        if annotated._verdict_in(verdict_cell) != "cite":
            continue
        key = f"@{match.group(1)}"
        rows.append({"key": key, "study_type": study_type, "number": number, "point": verdict_cell})
        seen.add(key)
    # C11: tolerate non-table annotated shapes (section-list / inline) so a cite key is never
    # silently dropped from the writing brief — enrich with empty number/point.
    for key, study_type in annotated.parse_text(text).get("cite", []):
        if key not in seen:
            rows.append({"key": key, "study_type": study_type, "number": "", "point": "cite"})
            seen.add(key)
    return rows


def _writing_brief(topic_dir: pathlib.Path, store: refs.Store) -> dict[str, Any]:
    """Aggregate the analyst cite-verdicts across rounds × gaps (from the
    persisted annotated.md files) into a per-gap cite list (key + study_type +
    key number + one-line point) for the writer."""
    latest = term_check.latest_round(store)
    gap_ids = sorted((store.get("gaps") or {}).keys())
    cite_by_gap: dict[str, list[dict[str, str]]] = {}
    for gid in gap_ids:
        seen: dict[str, dict[str, str]] = {}  # key -> row (dedup across rounds)
        for round_n in range(1, latest + 1):
            ann = topic_dir / "notes" / f"round-{round_n}" / f"{gid}.annotated.md"
            for row in _parse_cite_rows(ann):
                seen.setdefault(row["key"], row)
        cite_by_gap[gid] = list(seen.values())
    return {
        "intent_doc": f"{_topic_ref(topic_dir)}/research_log.md",
        "cite_by_gap": cite_by_gap,
        "gap_chapters": [
            {"gap": gid, "proposition_title_hint": (store.get("gaps") or {})[gid].get("description", "")}
            for gid in gap_ids
        ],
        "hint": "spawn writer (Opus) fed research_log.md (intent) + this cite_by_gap; "
        "chapter titles must be decision propositions, not evidence-type buckets",
    }


def _rework_brief(topic_dir: pathlib.Path, status: dict[str, Any]) -> dict[str, Any]:
    import reviewer
    from lib import layout

    rv = status.get("reviewer") or {}
    round_n = rv.get("latest_round")
    fails: list[dict[str, Any]] = []
    if round_n:
        for n in range(1, reviewer.REVIEWER_COUNT + 1):
            path = layout.reviewer_round_path(topic_dir, round_n, n)
            _raw, effective, _c = reviewer._read_reviewer_verdict(path)
            if effective == "request_changes":
                fails.append({"reviewer": n, "file": f"{_topic_ref(topic_dir)}/reviewers/round_{round_n}_{n}.md"})
    return {
        "reviewer_round": round_n,
        "next_round": int(round_n or 0) + 1,
        "request_changes_from": fails,
        "hint": "read each FAIL reviewer file, revise review.md, re-render if cites changed, "
        "then reviewer.py prompt --round <next_round> + re-spawn/resume reviewers (only judge fixed + regressions)",
    }


# ── frontier walk ────────────────────────────────────────────────────────────

# Ordered phase-exit gates. The first one not MET is the frontier.
_PHASES = [
    ("1_build", "1->2"),
    ("2-4_rounds", "4->5"),
    ("5_write", "5->6"),
    ("6_review", "6->7"),
    ("7_final", "7"),
]


def gate(topic_dir: pathlib.Path) -> dict[str, Any]:
    status = workflow_status.build_status(topic_dir)
    store = refs.load(topic_dir)

    # The write gate runs a lint subprocess; cache it so the 5->6 and 7 boundaries
    # don't lint twice in one call.
    _write_cache: dict[str, tuple[bool, list[str]]] = {}

    def _write_gate() -> tuple[bool, list[str]]:
        if "r" not in _write_cache:
            _write_cache["r"] = _gate_write(status, topic_dir)
        return _write_cache["r"]

    evaluators = {
        "1->2": lambda: _gate_build(status),
        "4->5": lambda: _gate_rounds(status),
        "5->6": _write_gate,
        "6->7": lambda: _gate_review(status),
        # 7 = finalize: the write-gate checks (reuse cache) PLUS the N11 realtime
        # retraction recheck (only when HEALTH_REVIEW_FINALIZE=1). Previously this
        # was `_write_gate`, so _gate_final / M2 was dead code (gap #3).
        "7": lambda: _gate_final(_write_gate(), topic_dir),
    }

    frontier_phase = "done"
    frontier_boundary = None
    blocking: list[str] = []
    for phase_label, boundary in _PHASES:
        met, why = evaluators[boundary]()
        if not met:
            frontier_phase = phase_label
            frontier_boundary = boundary
            blocking = why
            break

    result: dict[str, Any] = {
        "topic_dir": str(topic_dir),
        "phase": frontier_phase,
        "boundary": frontier_boundary,
        "gate": "done" if frontier_boundary is None else "BLOCKED",
        "blocking": blocking,
        "underlying_status_next_command": status.get("next_command"),
    }

    # next-action + ready-made params for the frontier's unblock action.
    if frontier_boundary is None:
        result["next"] = "review complete — nothing to gate"
        result["params"] = {}
    elif frontier_boundary == "1->2":
        result["next"] = "Phase 1: align Round-1 gaps with the user, then declare + seed them"
        result["params"] = {} if store is None else {"declared_gaps": _gap_specs(store)}
    elif frontier_boundary == "4->5":
        result["next"] = (
            "Phase 2-4: run another round via round-loop (gaps not saturated / missing "
            "genealogy). round-loop stops on saturated/hard_stop, or hands back "
            "operator_failed / analyst_incomplete / needs_new_gaps — inspect its "
            "history and fix the named gap before re-running."
        )
        result["params"] = _rounds_params(status, store) if store is not None else {}
    elif frontier_boundary == "5->6":
        result["next"] = "Phase 5: write review.md (spawn writer) then lint + render_refs"
        brief = _writing_brief(topic_dir, store) if store is not None else {}
        result["params"] = brief
        # P1a last-line defense: a gap can be saturated (≥3 verified) yet have NO
        # annotated cite rows — the analyst skipped annotated.md or marked nothing
        # 'cite', so the writer would have no auditable evidence to cite for it.
        # round-loop's analyst_incomplete stop should catch this upstream; surface
        # it here too so the write boundary never silently hands the writer an
        # empty cite_by_gap for a declared gap.
        empty_gaps = [gid for gid, rows in (brief.get("cite_by_gap") or {}).items() if not rows]
        if empty_gaps:
            result["blocking"] = list(result["blocking"]) + [
                f"missing annotated cite rows for gap(s): {', '.join(empty_gaps)} "
                "(analyst didn't write annotated.md or marked nothing 'cite' — re-run that gap's round)"
            ]
    elif frontier_boundary == "6->7":
        rv = status.get("reviewer") or {}
        if rv.get("status") == "request_changes":
            result["next"] = "Phase 6: revise review.md per reviewer FAIL, then re-review"
            result["params"] = _rework_brief(topic_dir, status)
        else:
            # not_started → round 1; missing_reviewers at round K → finish K (no
            # bump); invalid → K+1. Build the prompt_cmd from the SAME number, so
            # a resumed/missing-reviewer round doesn't regenerate the round-1 prompt.
            rr = (rv.get("latest_round") or 0) + (0 if rv.get("status") == "missing_reviewers" else 1) or 1
            result["next"] = "Phase 6: spawn the 3 Opus reviewers and tally"
            result["params"] = {
                "reviewer_round": rr,
                "prompt_cmd": f"python tools/reviewer.py prompt {_topic_ref(topic_dir)} --round {rr}",
            }
    elif frontier_boundary == "7":
        result["next"] = "Phase 7: fix lint / re-render, then final render_refs + (opt-in) signoff"
        result["params"] = {}
    return result


def print_text(g: dict[str, Any]) -> None:
    print(f"Conductor: {g['topic_dir']}")
    print(f"Phase: {g['phase']}  gate({g.get('boundary') or '—'})={g['gate']}")
    if g["blocking"]:
        print("Blocking:")
        for item in g["blocking"]:
            print(f"  - {item}")
    print(f"Next: {g['next']}")
    if g.get("params"):
        print("Params (ready-made; full JSON via --json):")
        for key in g["params"]:
            print(f"  - {key}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Mechanical phase-gate for a review (read-only).")
    parser.add_argument("topic_dir")
    parser.add_argument("--json", action="store_true", help="Emit the full gate result as JSON.")
    args = parser.parse_args()

    topic_dir = pathlib.Path(args.topic_dir)
    if not topic_dir.exists():
        print(f"[ERROR] topic dir not found: {topic_dir}", file=sys.stderr)
        raise SystemExit(2)

    result = gate(topic_dir)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_text(result)


if __name__ == "__main__":
    main()
