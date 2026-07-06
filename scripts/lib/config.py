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
TERM_HARD_MAX_ROUNDS = 5
CITED_RATIO_PER_PATCH: dict[str, float] = {}

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
