"""Read-only status panel for a review topic.

The command intentionally inspects files and existing stores only. It does not
run lint/render/self_review because those tools may write derived artifacts.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import refs
import reviewer
import term_check
from lib import layout, project


PRISMA_RE = re.compile(r"<!-- prisma-flow:start -->(.*?)<!-- prisma-flow:end -->", re.S)
REFS_RE = re.compile(r"<!-- refs:start -->(.*?)<!-- refs:end -->", re.S)
PROMPT_HASH_RE = re.compile(r"^<!--\s*prompt_sha256:\s*([0-9a-fA-F]+)\s*-->")


def _topic_ref(topic_dir: pathlib.Path) -> str:
    return f"reviews/{topic_dir.name}"


def _is_eligible(entry: refs.Entry) -> bool:
    return (
        entry.get("verification_status") == "verified"
        and not entry.get("retracted")
        and not entry.get("excluded_reason")
    )


def _round_status(store: refs.Store) -> dict[str, Any]:
    latest = term_check.latest_round(store)
    genealogy_by_round: dict[int, int] = {round_num: 0 for round_num in range(1, latest + 1)}
    for entry in store.get("entries", {}).values():
        round_num = entry.get("added_round")
        source = entry.get("source", "")
        if isinstance(round_num, int) and isinstance(source, str) and source.startswith("genealogy"):
            genealogy_by_round[round_num] = genealogy_by_round.get(round_num, 0) + 1

    missing_genealogy = [
        round_num for round_num, count in sorted(genealogy_by_round.items()) if count == 0
    ]
    return {
        "latest_round": latest,
        "store_rounds": store.get("rounds", 0),
        "genealogy_by_round": genealogy_by_round,
        "missing_genealogy_rounds": missing_genealogy,
    }


def _gap_status(store: refs.Store) -> dict[str, Any]:
    gaps = store.get("gaps", {})
    by_status = collections.Counter(gap.get("status", "pending") for gap in gaps.values())
    gap_rows: list[dict[str, Any]] = []
    unassigned = collections.Counter()

    for gap_id, gap in sorted(gaps.items()):
        counts = collections.Counter()
        for entry in store.get("entries", {}).values():
            if entry.get("gap") != gap_id:
                continue
            if entry.get("excluded_reason"):
                counts["excluded"] += 1
            else:
                counts[str(entry.get("verification_status", "unknown"))] += 1
            if _is_eligible(entry):
                counts["eligible"] += 1
        gap_rows.append(
            {
                "id": gap_id,
                "status": gap.get("status", "pending"),
                "created_round": gap.get("created_round", 0),
                "description": gap.get("description", ""),
                "counts": dict(counts),
            }
        )

    for entry in store.get("entries", {}).values():
        if entry.get("gap") in gaps:
            continue
        status = "excluded" if entry.get("excluded_reason") else str(
            entry.get("verification_status", "unknown")
        )
        unassigned[status] += 1

    return {
        "total": len(gaps),
        "by_status": dict(by_status),
        "gaps": gap_rows,
        "unassigned_entries": dict(unassigned),
    }


def _render_block_status(pattern: re.Pattern[str], text: str, expected_heading: str) -> bool:
    match = pattern.search(text)
    return bool(match and expected_heading in match.group(1))


def _render_refs_status(topic_dir: pathlib.Path) -> dict[str, Any]:
    review_path = project.review_path(topic_dir)
    if not review_path.exists():
        return {
            "review_exists": False,
            "has_prisma_flow": False,
            "has_references": False,
            "citation_stats_exists": False,
        }

    text = review_path.read_text(encoding="utf-8")
    has_prisma = _render_block_status(PRISMA_RE, text, "PRISMA flow")
    has_refs = _render_block_status(REFS_RE, text, "References")
    return {
        "review_exists": True,
        "has_prisma_flow": has_prisma,
        "has_references": has_refs,
        "citation_stats_exists": layout.citation_stats_path(topic_dir).exists(),
        "rendered": has_prisma and has_refs,
    }


def _reviewer_files_by_round(topic_dir: pathlib.Path) -> dict[int, set[int]]:
    rounds: dict[int, set[int]] = {}
    reviewers_dir = layout.reviewers_dir(topic_dir)
    if not reviewers_dir.is_dir():
        return rounds
    for path in reviewers_dir.glob("round_*_*.md"):
        match = layout.REVIEWER_FILE_RE.match(path.name)
        if not match:
            continue
        round_num = int(match.group(1))
        reviewer_num = int(match.group(2))
        rounds.setdefault(round_num, set()).add(reviewer_num)
    return rounds


def _prompt_hash(path: pathlib.Path) -> str | None:
    if not path.exists():
        return None
    first_line = path.read_text(encoding="utf-8").splitlines()[:1]
    if not first_line:
        return None
    match = PROMPT_HASH_RE.match(first_line[0])
    return match.group(1).lower() if match else None


def _reviewer_status(topic_dir: pathlib.Path) -> dict[str, Any]:
    rounds = _reviewer_files_by_round(topic_dir)
    if not rounds:
        return {
            "status": "not_started",
            "latest_round": None,
            "approved": False,
            "approve_count": 0,
            "request_changes_count": 0,
            "missing_reviewers": list(range(1, reviewer.REVIEWER_COUNT + 1)),
            "prompt_exists": False,
            "prompt_hashes": {},
            "prompt_hash_consistent": None,
        }

    latest = max(rounds)
    verdicts: dict[int, str | None] = {}
    prompt_hashes: dict[int, str | None] = {}
    for reviewer_num in range(1, reviewer.REVIEWER_COUNT + 1):
        path = layout.reviewer_round_path(topic_dir, latest, reviewer_num)
        _raw, effective, _contradicted = reviewer._read_reviewer_verdict(path)
        verdicts[reviewer_num] = effective
        prompt_hashes[reviewer_num] = _prompt_hash(path)

    missing = [
        reviewer_num for reviewer_num, effective in verdicts.items() if effective is None
    ]
    approve_count = sum(1 for effective in verdicts.values() if effective == "approve")
    request_count = sum(
        1 for effective in verdicts.values() if effective == "request_changes"
    )
    nonempty_hashes = {value for value in prompt_hashes.values() if value}
    hash_consistent = None
    if nonempty_hashes:
        hash_consistent = len(nonempty_hashes) == 1 and not any(
            value is None for value in prompt_hashes.values()
        )

    if missing:
        status = "missing_reviewers"
    elif approve_count == reviewer.REVIEWER_COUNT:
        status = "approved"
    elif request_count:
        status = "request_changes"
    else:
        status = "invalid"

    return {
        "status": status,
        "latest_round": latest,
        "approved": status == "approved",
        "approve_count": approve_count,
        "request_changes_count": request_count,
        "missing_reviewers": missing,
        "prompt_exists": layout.reviewer_prompt_path(topic_dir, latest).exists(),
        "prompt_hashes": prompt_hashes,
        "prompt_hash_consistent": hash_consistent,
    }


def _exists_entry(topic_dir: pathlib.Path, relative: str, kind: str) -> dict[str, Any]:
    path = topic_dir / relative
    exists = path.is_dir() if kind == "dir" else path.exists()
    return {"path": relative, "kind": kind, "exists": exists}


def _artifact_status(
    topic_dir: pathlib.Path,
    store: refs.Store | None,
    render_status: dict[str, Any],
    reviewer_status: dict[str, Any],
) -> dict[str, Any]:
    required = [
        _exists_entry(topic_dir, "research_log.md", "file"),
        _exists_entry(topic_dir, "references_store.json", "file"),
        _exists_entry(topic_dir, "references", "dir"),
        _exists_entry(topic_dir, project.review_path(topic_dir).name, "file"),
        _exists_entry(topic_dir, "figures", "dir"),
    ]

    conditional: list[dict[str, Any]] = []
    latest = term_check.latest_round(store) if store is not None else 0
    for round_num in range(1, latest + 1):
        conditional.append(_exists_entry(topic_dir, f"notes/round-{round_num}.md", "file"))
    if render_status.get("rendered"):
        conditional.append(
            _exists_entry(topic_dir, f"{layout.META_DIRNAME}/citation_stats.md", "file")
        )
    if reviewer_status.get("latest_round"):
        latest_reviewer_round = reviewer_status["latest_round"]
        conditional.append(
            _exists_entry(
                topic_dir,
                f"{layout.REVIEWERS_DIRNAME}/prompt_round_{latest_reviewer_round}.md",
                "file",
            )
        )
        for reviewer_num in range(1, reviewer.REVIEWER_COUNT + 1):
            conditional.append(
                _exists_entry(
                    topic_dir,
                    f"{layout.REVIEWERS_DIRNAME}/round_{latest_reviewer_round}_{reviewer_num}.md",
                    "file",
                )
            )
    if reviewer_status.get("approved"):
        conditional.extend(
            [
                _exists_entry(topic_dir, f"{layout.DRAFTS_DIRNAME}/self_review.md", "file"),
                _exists_entry(topic_dir, f"{layout.DRAFTS_DIRNAME}/signoff.md", "file"),
            ]
        )

    optional = [
        _exists_entry(topic_dir, f"{layout.DRAFTS_DIRNAME}/gaps_draft.md", "file"),
        _exists_entry(topic_dir, f"{layout.DRAFTS_DIRNAME}/outline_draft.md", "file"),
        _exists_entry(topic_dir, f"{layout.REVIEWERS_DIRNAME}/failure_report.md", "file"),
        _exists_entry(topic_dir, "testflight.jsonl", "file"),
    ]
    pdfs = sorted(path.name for path in topic_dir.glob("*.pdf"))
    optional.append({"path": "*.pdf", "kind": "glob", "exists": bool(pdfs), "matches": pdfs})

    def missing(items: list[dict[str, Any]]) -> list[str]:
        return [str(item["path"]) for item in items if not item.get("exists")]

    return {
        "required": required,
        "conditional": conditional,
        "optional": optional,
        "missing_required": missing(required),
        "missing_conditional": missing(conditional),
    }


def _infer_phase(
    store: refs.Store | None,
    term_status: dict[str, Any] | None,
    render_status: dict[str, Any],
    reviewer_status: dict[str, Any],
    artifacts: dict[str, Any],
) -> str:
    if store is None:
        return "bootstrap"
    if not render_status.get("review_exists"):
        return "bootstrap"
    if term_status and not term_status.get("ok_for_writing"):
        return "round"
    if not render_status.get("rendered"):
        return "write_render"
    if not reviewer_status.get("approved"):
        return "reviewer"
    missing_conditional = artifacts.get("missing_conditional", [])
    self_review_rel = f"{layout.DRAFTS_DIRNAME}/self_review.md"
    signoff_rel = f"{layout.DRAFTS_DIRNAME}/signoff.md"
    if self_review_rel in missing_conditional or signoff_rel in missing_conditional:
        return "final"
    return "done"


def _next_command(
    topic_dir: pathlib.Path,
    phase: str,
    term_status: dict[str, Any] | None,
    render_status: dict[str, Any],
    reviewer_status: dict[str, Any],
    artifacts: dict[str, Any],
) -> str:
    topic = _topic_ref(topic_dir)
    if phase == "bootstrap":
        return 'python scripts/bootstrap_topic.py "<topic>" --domain health'
    if term_status and not term_status.get("ok_for_writing"):
        return f"python scripts/gaps_status.py {topic}"
    if not render_status.get("rendered"):
        return f"python scripts/render_refs.py {topic}/{project.review_path(topic).name} {topic}"
    reviewer_round = reviewer_status.get("latest_round")
    if reviewer_status.get("status") == "not_started":
        return f"python scripts/reviewer.py prompt {topic} --round 1"
    if reviewer_status.get("status") == "missing_reviewers":
        return "spawn missing Opus reviewer subagents, then rerun workflow_status.py"
    if reviewer_status.get("status") == "request_changes":
        next_round = int(reviewer_round or 0) + 1
        return f"revise review.md, then python scripts/reviewer.py prompt {topic} --round {next_round}"
    missing = artifacts.get("missing_conditional", [])
    self_review_rel = f"{layout.DRAFTS_DIRNAME}/self_review.md"
    signoff_rel = f"{layout.DRAFTS_DIRNAME}/signoff.md"
    if self_review_rel in missing:
        return f"python scripts/self_review.py {topic}"
    if signoff_rel in missing:
        return f"write {topic}/{signoff_rel}"
    return "none"


def build_status(topic_dir: pathlib.Path) -> dict[str, Any]:
    store = refs.load(topic_dir)
    if store is None:
        render = _render_refs_status(topic_dir)
        reviewer_info = _reviewer_status(topic_dir)
        artifacts = _artifact_status(topic_dir, None, render, reviewer_info)
        phase = _infer_phase(None, None, render, reviewer_info, artifacts)
        return {
            "topic_dir": str(topic_dir),
            "phase": phase,
            "store_exists": False,
            "round": None,
            "gaps": None,
            "term_check": {
                "status": "not_ready",
                "ok": False,
                "ok_for_writing": False,
                "latest_round": 0,
                "messages": [f"[ERROR] no references store under {topic_dir}"],
            },
            "reviewer": reviewer_info,
            "render_refs": render,
            "artifacts": artifacts,
            "next_command": _next_command(topic_dir, phase, None, render, reviewer_info, artifacts),
        }

    term = term_check.evaluate_store(store).to_dict()
    round_info = _round_status(store)
    gaps = _gap_status(store)
    render = _render_refs_status(topic_dir)
    reviewer_info = _reviewer_status(topic_dir)
    artifacts = _artifact_status(topic_dir, store, render, reviewer_info)
    phase = _infer_phase(store, term, render, reviewer_info, artifacts)
    return {
        "topic_dir": str(topic_dir),
        "phase": phase,
        "store_exists": True,
        "round": round_info,
        "gaps": gaps,
        "term_check": term,
        "reviewer": reviewer_info,
        "render_refs": render,
        "artifacts": artifacts,
        "next_command": _next_command(topic_dir, phase, term, render, reviewer_info, artifacts),
    }


def _format_bool(value: object) -> str:
    return "yes" if value else "no"


def print_text(status: dict[str, Any]) -> None:
    print(f"Workflow status: {status['topic_dir']}")
    print(f"Phase: {status['phase']}")
    if status.get("round"):
        round_info = status["round"]
        print(
            "Round: latest={latest} store_rounds={store_rounds} missing_genealogy={missing}".format(
                latest=round_info["latest_round"],
                store_rounds=round_info["store_rounds"],
                missing=round_info["missing_genealogy_rounds"] or "none",
            )
        )
    if status.get("gaps"):
        gaps = status["gaps"]
        print(f"Gaps: total={gaps['total']} by_status={gaps['by_status']}")
    term = status["term_check"]
    print(
        f"Term: status={term['status']} ok_for_writing={_format_bool(term['ok_for_writing'])} "
        f"latest_round={term['latest_round']}"
    )
    for message in term.get("messages", []):
        print(f"  {message}")

    render = status["render_refs"]
    print(
        "Render refs: prisma={prisma} references={refs} citation_stats={stats}".format(
            prisma=_format_bool(render.get("has_prisma_flow")),
            refs=_format_bool(render.get("has_references")),
            stats=_format_bool(render.get("citation_stats_exists")),
        )
    )
    reviewer_info = status["reviewer"]
    print(
        "Reviewer: status={status} latest_round={round} approve={approve}/{total} missing={missing}".format(
            status=reviewer_info["status"],
            round=reviewer_info["latest_round"],
            approve=reviewer_info["approve_count"],
            total=reviewer.REVIEWER_COUNT,
            missing=reviewer_info["missing_reviewers"] or "none",
        )
    )
    artifacts = status["artifacts"]
    print(f"Artifacts: missing_required={artifacts['missing_required'] or 'none'}")
    print(f"Artifacts: missing_conditional={artifacts['missing_conditional'] or 'none'}")
    print(f"Next: {status['next_command']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only workflow status panel for a review topic."
    )
    parser.add_argument("topic_dir")
    parser.add_argument("--json", action="store_true", help="Emit JSON status.")
    args = parser.parse_args()

    status = build_status(pathlib.Path(args.topic_dir))
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        print_text(status)


if __name__ == "__main__":
    main()
