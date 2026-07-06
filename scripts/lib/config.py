from __future__ import annotations

from typing import Literal

CITED_RATIO_THRESHOLD = 0.5
BROAD_GAP_VERIFIED_THRESHOLD = 20
BROAD_GAP_MIN_CITED = 5
TOP_N_UNCITED_IN_FAIL = 10
BROAD_GAP_BLACKLIST = [
    "的作用",
    "机制综述",
    "概览",
    "研究现状",
    "X 综述",
    "研究进展",
]
TERM_MIN_ROUNDS = 3
TERM_MIN_VERIFIED_PER_GAP = 3
TERM_SATURATION_RATIO = 0.10
# plan v3 §3.3 C3: a single low-growth round can be a cap artifact, so
# saturation requires this many *consecutive* non-addendum rounds each adding
# ≤ TERM_SATURATION_RATIO of the eligible pool. 1 restores the old single-round
# behaviour.
TERM_SATURATION_CONSECUTIVE = 2
TERM_HARD_MAX_ROUNDS = 5
CITED_RATIO_PER_PATCH: dict[str, float] = {}

# plan v3 §2 C4 / W2: genealogy cap policy as a function of round + current
# eligible-pool size, so the workflow no longer hard-codes `g.cap || 15`.
# Early rounds stay wide (recall-first expansion); confirmation rounds tighten
# to ~eligible/10 (the saturation-ratio denominator) with a floor so a small
# library can still pull a handful. Pure + side-effect-free for easy testing.
GENEALOGY_CAP_EARLY = 20
GENEALOGY_CAP_CONFIRM_FLOOR = 6
GENEALOGY_EARLY_ROUNDS = 2


def auto_genealogy_cap(round_n: int, eligible_count: int, num_gaps: int = 1) -> int:
    """Return the **per-gap** genealogy --max-add cap for ``round_n`` given the
    current eligible-pool size and how many gaps expand this round. Early rounds
    → wide constant; later (confirmation) rounds → ``max(floor, eligible // (10 *
    num_gaps))``.

    plan v3.1 R5 (口径 fix, §1.4 F1): the saturation ratio C3 compares is a
    *round-total* (Σ gaps) against the eligible pool. The old per-gap cap of
    ``eligible // 10`` made the round total ≈ ``num_gaps × 10%`` (5 gaps → 50%),
    so natural saturation could NEVER trigger and every review hard-stopped.
    Dividing the confirmation cap by ``num_gaps`` keeps the round total ≈ 10% so
    C3 saturation is reachable. The floor still lets a tiny library pull a few."""
    if round_n <= GENEALOGY_EARLY_ROUNDS:
        return GENEALOGY_CAP_EARLY
    denom = 10 * max(1, num_gaps)
    return max(GENEALOGY_CAP_CONFIRM_FLOOR, eligible_count // denom)

MismatchSeverity = Literal["WARN", "ERROR"]
MISMATCH_SEVERITY: dict[tuple[str, ...], MismatchSeverity] = {
    ("title_mismatch",): "ERROR",
    ("title_mismatch", "year_mismatch"): "ERROR",
    ("title_mismatch", "first_author_mismatch"): "ERROR",
}
FORCE_MISMATCH_ALLOWED = True

PROSE_METADATA_WINDOW_CHARS = 80  # deprecated; kept for backwards-compat reads
PROSE_ANCHOR_BEFORE_CHARS = 30
# only check prose-vs-metadata when an "Author et al./等 (YEAR)?" or
# "(Author, YEAR)" anchor sits within this many chars before [@key].
# Wide windows (80) caused systematic false positives where background
# years / other-citation author names got matched to the wrong entry —
# see 木浆海绵 case study, 2026-05-22 audit. Anchor-tight matching keeps
# Hazari-class real errors (prose "Hazari 等 (2007)" vs entry year=2008)
# while skipping background-year noise.
SUSPICIOUS_TITLE_PATTERNS = [
    r"^Congratulations to",
    r"^Tribute to",
    r"^Editorial:",
    r"^Erratum:",
    r"^In memoriam",
    r"^Reply to",
    r"^Correction to",
]
