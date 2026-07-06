"""Shared primitives for the mechanical hard-block gates (workflow_spec §4).

plan v3.1 P0 builds a two-gate spine — ``round_gate`` (轮转, spec N5) and
``write_gate`` (写作/收尾, spec N6/N11). Both compose a list of checks; this
module gives them the common vocabulary so the gates and the Stop hook stay in
sync (conductor_spec §4: "the proactive gate and the end-gate never drift").

A check yields one of three states:

* ``pass``    — condition met.
* ``fail``    — condition violated → the gate must HARD-BLOCK (non-zero exit).
* ``pending`` — the check enforces a feature that is *not yet implemented*
                (its input artifact cannot exist yet, e.g. ``meta/quarantine.jsonl``
                before R2, ``claim_evidence_map`` before F1/F3). Reported so the
                coverage gap is visible, but NOT a hard-block. As the upstream
                feature lands, the same check flips ``pending`` → ``pass``/``fail``
                with no rewiring (plan v3.1 §5: P0 is the 落点, P1/P2 fill it).

The ``pending`` state is what lets P0 ship a *real* hard-block today (on the
checkable conditions) without making every gate un-passable before P1/P2 exist.
"""
from __future__ import annotations

import dataclasses
import pathlib
import re
from typing import Literal

Status = Literal["pass", "fail", "pending"]


@dataclasses.dataclass
class CheckResult:
    name: str
    status: Status
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "message": self.message}


def passed(name: str, message: str = "") -> CheckResult:
    return CheckResult(name, "pass", message)


def failed(name: str, message: str) -> CheckResult:
    return CheckResult(name, "fail", message)


def pending(name: str, message: str) -> CheckResult:
    return CheckResult(name, "pending", message)


def is_blocked(results: list[CheckResult]) -> bool:
    """A gate blocks iff any check FAILED. ``pending`` never blocks."""
    return any(r.status == "fail" for r in results)


def grandfather(results: list[CheckResult]) -> list[CheckResult]:
    """R2-F5: when `.gates_legacy` grandfathers a topic, EVERY check is exempt — convert
    any residual `fail` to `pending` so the gate function's verdict matches the Stop
    hook's full-skip semantics (a legacy review is never hard-blocked, uniformly, not
    just on the few checks that individually honor the marker)."""
    return [
        pending(r.name, r.message + " [.gates_legacy grandfathered]") if r.status == "fail" else r
        for r in results
    ]


def summarize(results: list[CheckResult]) -> dict[str, object]:
    return {
        "blocked": is_blocked(results),
        "fail": [r.message for r in results if r.status == "fail"],
        "pending": [r.message for r in results if r.status == "pending"],
        "pass": [r.name for r in results if r.status == "pass"],
        "checks": [r.to_dict() for r in results],
    }


# ── research_log.md marker parsing ────────────────────────────────────────────
#
# The gates read declarative markers the main thread / tools leave in
# research_log.md. Conventions (documented in docs/tool_internals.md §gates):
#
#   prune recorded   : a non-empty "## Exclusion audit log" section
#                      (exclude.py / regap.py auto-append), OR an explicit
#                      "本轮无需 prune" / "no-prune" line.
#   skip-analyst     : "<!-- skip-analyst: gap-N round R 理由 -->" OR a line
#                      containing 跳过 analyst / skip analyst that names the gap.
#   writer method    : "<!-- phase5-method: ... -->" OR write-loop / 直写 mention.
#   recall audit     : meta/recall_audit.* artifact OR "<!-- recall-audit ... -->"
#                      / 召回审计 / known-item / forward-citation mention.

_EXCLUSION_AUDIT_HEADING = "## Exclusion audit log"
# hyphen required for the English form so prose like "no prune was needed" does
# not false-positive as a declaration; the structured comment also matches.
_NO_PRUNE_RE = re.compile(r"本轮无需\s*prune|无需\s*剪枝|本轮不剪枝|<!--\s*no-prune|no-prune\b", re.I)
_WRITER_METHOD_RE = re.compile(
    r"<!--\s*phase5-method\s*:|write[\-\s]?loop|主线程直写|直写", re.I
)
_RECALL_AUDIT_RE = re.compile(
    r"<!--\s*recall[\-\s]?audit|召回审计|known[\-\s]?item|forward[\-\s]?citation", re.I
)
_SKIP_ANALYST_RE = re.compile(r"跳过\s*analyst|skip[\-\s]?analyst|未派\s*analyst", re.I)
_NEEDS_REVIEW_CLEARED_RE = re.compile(
    r"needs[\-\s_]?review[\-\s_]?cleared|needs[\-\s_]?review\s*已清",
    re.I,
)
# R2-F4: per-claim_id clearing — `<!-- needs-review-cleared: c1 c2 -->` / `needs-review
# 已清: c1, c2` names exactly which needs_review claim_ids a human resolved (or `ALL` as
# an explicit, deliberate blanket). Replaces the stray-substring blanket clear, which let
# one line nullify the entire deterministic needs_review layer.
# R3-A1: bound the capture to the SAME LINE and stop at the comment-close '>' — the
# old `[\w\-,\s]+` class matched newlines, so a marker greedily swallowed claim_ids from
# a later unrelated paragraph into the cleared set (a fail-OPEN gate bypass).
_NR_CLEAR_IDS_RE = re.compile(
    # R3-A1 + R4: capture is line-bounded ([^\n>]) AND the post-colon whitespace is
    # HORIZONTAL-only ([^\S\n], not \s*) — else a colon at end-of-line would let \s*
    # eat the newline and the capture would grab the entire NEXT line (residual fail-open).
    r"needs[\-\s_]?review[\-\s_]?(?:cleared|已清)\s*[:：][^\S\n]*([^\n>]*)",
    re.I,
)


def read_text(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def has_exclusion_audit(research_log_text: str) -> bool:
    """True if the Exclusion audit log section exists with ≥1 bullet line."""
    idx = research_log_text.find(_EXCLUSION_AUDIT_HEADING)
    if idx == -1:
        return False
    section = research_log_text[idx + len(_EXCLUSION_AUDIT_HEADING):]
    # stop at the next ## heading
    next_h = section.find("\n## ")
    if next_h != -1:
        section = section[:next_h]
    return any(line.strip().startswith(("-", "*", "|")) for line in section.splitlines())


def has_no_prune_marker(research_log_text: str) -> bool:
    return bool(_NO_PRUNE_RE.search(research_log_text))


def has_writer_method(research_log_text: str) -> bool:
    return bool(_WRITER_METHOD_RE.search(research_log_text))


def has_recall_audit_marker(research_log_text: str) -> bool:
    return bool(_RECALL_AUDIT_RE.search(research_log_text))


def has_needs_review_cleared(research_log_text: str) -> bool:
    """A declaration that faithfulness ``needs_review`` items were human-cleared
    (spec §0.6 gate: "needs_review 已清"). Back-compat boolean; prefer
    ``needs_review_cleared_ids`` for per-claim accountability."""
    return bool(_NEEDS_REVIEW_CLEARED_RE.search(research_log_text))


def needs_review_cleared_ids(research_log_text: str) -> set[str]:
    """R2-F4: the SET of claim_ids a human explicitly cleared via a structured marker
    `<!-- needs-review-cleared: c1 c2 -->` (or `needs-review 已清: c1, c2`). The token
    ``ALL`` is an explicit, deliberate blanket. A marker with NO ids clears nothing —
    per-claim accountability replaces the stray-substring blanket clear."""
    ids: set[str] = set()
    for match in _NR_CLEAR_IDS_RE.finditer(research_log_text):
        chunk = match.group(1) or ""
        for tok in re.split(r"[\s,，]+", chunk.strip()):
            # keep real ids (must contain a word char); drop comment-close noise ('--', '->')
            if tok and re.search(r"\w", tok):
                ids.add(tok)
    return ids


def has_skip_analyst(research_log_text: str, gap_id: str, round_n: int) -> bool:
    """A skip-analyst declaration that names this gap (round optional)."""
    structured = re.compile(
        r"<!--\s*skip-analyst:[^>]*\b" + re.escape(gap_id) + r"\b[^>]*-->", re.I
    )
    if structured.search(research_log_text):
        return True
    # loose: a line mentioning skip-analyst AND the gap id. Use a word-boundary
    # match (not a raw substring) so "gap-10" does not clear "gap-1" — matching
    # the structured branch's `\b...\b` and closing a fail-OPEN collision.
    gap_word = re.compile(r"\b" + re.escape(gap_id) + r"\b")
    for line in research_log_text.splitlines():
        if _SKIP_ANALYST_RE.search(line) and gap_word.search(line):
            return True
    return False
