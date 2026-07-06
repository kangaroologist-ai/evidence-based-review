"""Deterministic faithfulness gate — 正文↔来源忠实度检查 (C5 + C11).

No network calls, no LLM. Checks every in-text citation [@key] in
review.md against the best available source text for that entry and
assigns a verdict per claim.

Verdicts (precedence: suspect > needs_review > insufficient > faithful):
  faithful      — grounded, all numbers found in source, no study-type
                  conflict, no polarity word.
  suspect       — a number from the claim is NOT found in source text,
                  OR the prose study-type label conflicts with
                  entry.study_type / source text.
  needs_review  — claim contains a polarity word; direction cannot be
                  confirmed deterministically (gershon-class failure).
  insufficient  — entry is title-only (no source text to verify against).

Usage:
    python tools/faithfulness.py reviews/<topic> [--strict]

With --strict: exits 1 if any verdict is 'suspect'.

Public API (importable, no side effects):
    from faithfulness import evaluate, ClaimVerdict
    results: list[ClaimVerdict] = evaluate(store, review_text, loader=None)
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib import claimids, project, testflight
import refs


# ---------------------------------------------------------------------------
# Sentence / claim extraction
# ---------------------------------------------------------------------------

_CITATION_RE = re.compile(r"\[@([\w\-:]+)\]")

# Sentence boundary: CJK and ASCII punctuation. We split AFTER the punctuation
# (lookbehind) but will re-join orphaned citation-only fragments with prior text.
_SENT_SPLIT = re.compile(r"(?<=[。！？；.!?;])(?=\s*(?:[^@\[]|$))")


def _split_sentences(text: str) -> list[str]:
    """Split prose into sentence segments; merge citation-only fragments back."""
    # Simple line-level split first, then paragraph/sentence refinement.
    # Strategy: scan for [@key] markers and capture the surrounding line/segment.
    # We work at the level of "paragraphs" (double-newline delimited), then
    # split each paragraph into sentence units, ensuring that [@key] at the
    # end of a clause stays attached to the clause.
    sentences: list[str] = []
    # Split on double newlines (paragraph breaks)
    for para in re.split(r"\n{2,}", text):
        para = para.strip()
        if not para:
            continue
        # Split on sentence-ending punctuation (keeping the punctuation)
        parts = re.split(r"(?<=[。！？；.!?;])\s*", para)
        buf = ""
        for part in parts:
            buf += part
            # Only commit the buffer as a sentence if it has real content
            # (not just a citation marker) OR if the buffer contains a citation
            # and some preceding text. We always keep building until a natural break.
            if re.search(r"[。！？；.!?;]\s*$", buf) or not part.strip():
                if buf.strip():
                    sentences.append(buf.strip())
                buf = ""
        if buf.strip():
            sentences.append(buf.strip())
    return sentences


def _claim_ids_by_occurrence(review_text: str) -> list[str | None]:
    """For each ``[@key]`` occurrence in document order, the claim_id sidecar on
    its line (or None). Line-based so a sidecar trailing the sentence-ending
    period — which the sentence splitter drops — is still associated with its
    [@key]. Pairs by index when a line has equal #sidecars and #keys, else the
    line's single sidecar covers all its keys."""
    out: list[str | None] = []
    for line in review_text.split("\n"):
        # Only a FACTUAL sidecar (no type / type:factual) annotates a cited [@key]. An EXEMPT-label
        # sidecar (type:inference/research_log/…) annotates non-keyed synthesis and is left for
        # Phase 2 (round-2 finding #5): else 'cited [@key]，fabricated 99 倍 <!-- claim:c1
        # type:inference -->' index-bound c1 to the key, and the fabricated empirical text after the
        # key (outside the Phase-1 claim_span) was adjudicated NOWHERE — an arrangement bypass.
        cid_ms = [m for m in claimids._CLAIM_FULL_RE.finditer(line)
                  if (m.group(2) or "factual") not in _LOG_KEY_TYPES]
        key_ms = list(_CITATION_RE.finditer(line))
        cids = [m.group(1) for m in cid_ms]
        for index, km in enumerate(key_ms):
            if len(cids) == len(key_ms):
                out.append(cids[index])
            elif cid_ms:
                # R1 fix: counts mismatch (e.g. an inference sidecar shares the line with a
                # cited clause) → bind each [@key] to the NEAREST sidecar by char offset, not
                # cids[0] — else the cited clause stole the inference id and became insufficient.
                kpos = km.start()
                out.append(min(cid_ms, key=lambda cm: abs(cm.start() - kpos)).group(1))
            else:
                out.append(None)
    return out


# Non-factual sidecar types (spec §0.6.a): grounding-exempt because they rest on
# grounded atoms (inference) or 过程日志 (method/search/store facts → research_log).
_LOG_KEY_TYPES = {"inference", "research_log", "search_log", "store_stat", "reviewer_log"}


def _sentence_by_claim_id(review_text: str) -> dict[str, str]:
    """Map each claim_id → the prose clause it annotates (the text preceding the
    sidecar on its line, citation markers + sidecars stripped). Used to populate
    the ``sentence``/``atomic_claim`` of non-[@key] map rows (inference / method
    facts) so the claim_map is never blank for them (spec §0.6.a)."""
    out: dict[str, str] = {}
    for line in review_text.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "|", ">")):
            continue
        last = 0
        for match in claimids._CLAIM_FULL_RE.finditer(line):
            cid = match.group(1)
            seg = claimids.strip(_CITATION_RE.sub("", line[last:match.start()])).strip()
            if cid not in out and seg:
                out[cid] = seg
            last = match.end()
    return out


def _claim_sentences(review_text: str) -> list[tuple[str, str]]:
    """Return (key, context_sentence) for every [@key] occurrence in review_text.

    The 'context_sentence' is the full line/paragraph segment surrounding the
    citation, so both the claim text and the citation marker are present.
    We split on newlines first to get line-level granularity, then look for
    [@key] in each line. For citation-heavy prose, multiple citations per line
    each get the same containing line as their sentence context.
    """
    result: list[tuple[str, str]] = []
    # Split into lines/segments, preserving structure.
    lines: list[str] = []
    for line in review_text.split("\n"):
        line = line.strip()
        if line:
            lines.append(line)
    # Further split long lines on sentence punctuation, but keep [@key] with
    # the text BEFORE it by using a "split before [@" approach.
    segments: list[str] = []
    for line in lines:
        # Split on sentence boundaries but reattach trailing [@key]
        # Use a two-pass: split on CJK/ASCII sentence enders, then merge
        # any segment that starts with [@key] back onto the previous segment.
        parts = re.split(r"(?<=[。！？；.!?;])\s+(?=[^\[@])", line)
        if len(parts) <= 1:
            segments.append(line)
        else:
            buf = ""
            for part in parts:
                if not buf:
                    buf = part
                elif re.match(r"^\[@", part.strip()):
                    buf += " " + part
                else:
                    if buf:
                        segments.append(buf)
                    buf = part
            if buf:
                segments.append(buf)

    for seg in segments:
        # Find all citation positions in this segment.
        citation_matches = list(_CITATION_RE.finditer(seg))
        if not citation_matches:
            continue
        # For each citation, determine the "claim span": the text from the
        # previous citation's end (or segment start) up to and including this
        # citation marker. This prevents numbers from citation A's clause from
        # being attributed to citation B's source check.
        prev_end = 0
        for i, m in enumerate(citation_matches):
            # Claim text = from previous citation end to current citation end
            claim_span = seg[prev_end: m.end()]
            result.append((m.group(1), claim_span))
            prev_end = m.end()
    return result


# ---------------------------------------------------------------------------
# Source text loading
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)
_H2_RE = re.compile(r"\n## ")


def _strip_source(text: str, full: bool = False) -> str:
    """Strip YAML frontmatter. For an ABSTRACT-only source also isolate the ## Abstract section
    (the abstract slot occasionally carries a stray following section). For a FULL-TEXT source
    (full=True) keep ALL sections — R28: a fulltext/pdf_text/ocr-grounded entry whose pdf_text/ocr
    paths are None falls back to the abstract slot, which holds the WHOLE document; truncating it to
    just ## Abstract discarded real numbers in Introduction/Results/Conclusion → 85 false-suspects
    across 38 topics. The writer grounded on the full text, so the full text is the source."""
    # Strip frontmatter block
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    if full:
        return text.strip()  # keep every section — full-text grounding checks against full text
    # Abstract-only: isolate the ## Abstract section
    marker = "## Abstract"
    if marker in text:
        text = text.split(marker, 1)[1]
    # Stop at next H2
    nxt = text.find("\n## ")
    if nxt != -1:
        text = text[:nxt]
    return text.strip()


def _load_source_text(entry: refs.Entry) -> str:
    """Load the best available source text for the entry; return '' if none."""
    paths = entry.get("paths") or {}
    grounding_level = refs.grounding(entry)

    # Try in order: pdf_text, ocr, abstract — whichever grounding resolves to
    # 'fulltext' is handled via fulltext_xml which we don't have a direct path
    # for, so fall through to pdf_text as next best.
    candidates: list[str | None] = []
    if grounding_level in ("fulltext", "pdf_text"):
        candidates.append(paths.get("pdf_text"))
    if grounding_level in ("fulltext", "pdf_text", "ocr"):
        candidates.append(paths.get("ocr"))
    candidates.append(paths.get("abstract"))

    for rel in candidates:
        if not isinstance(rel, str):
            continue
        abs_path = project.to_abs(rel)
        if abs_path is None or not abs_path.exists():
            continue
        raw = abs_path.read_text(encoding="utf-8")
        # R28: full-text grounding → keep all sections (the abstract-slot fallback holds the whole
        # document; truncating to ## Abstract dropped real Results/Conclusion numbers).
        return _strip_source(raw, full=grounding_level in ("fulltext", "pdf_text", "ocr"))
    return ""


# ---------------------------------------------------------------------------
# Number extraction and matching
# ---------------------------------------------------------------------------

# Matches: 45%, 12.3, N=1000, n = 240, 2.5-fold, RR 0.8, 1,000, and leading-dot .85 (R11)
# R30: a comma inside a number token must be a genuine thousands separator — `,` followed by
# EXACTLY 3 digits (`\d+(?:,\d{3})*`), not the old `[\d,]*` which greedily ate a CJK clause
# separator. `_extract_numbers` already collapses every real thousands group (_THOUSANDS_RE,
# line ~417) BEFORE this regex runs, so a surviving comma is always a clause boundary: the old
# class glued '74,291，6–17岁'→'742916' (full-width '，' folded to ',' then absorbed) → a faithful
# sample-size claim went false-suspect → write_gate HARD FAIL. `\d+(?:,\d{3})*` keeps '1,000' /
# '74,291' intact while stopping at a comma not trailed by a 3-digit block.
_NUM_TOKEN_RE = re.compile(
    r"(?:N\s*=\s*|n\s*=\s*|RR\s+|OR\s+|HR\s+|p\s*[<=]\s*)?"
    r"(\d+(?:,\d{3})*(?:\.\d+)?|\.\d+)"
    r"(?:\s*[-–]\s*(\d+(?:,\d{3})*(?:\.\d+)?|\.\d+))?"  # group(2) = range upper bound (R8)
    r"(?:\s*(?:%|fold|倍|×|x))?",
    re.IGNORECASE,
)

# R11: fold the number-relevant full-width forms to ASCII (full-width digits U+FF10–19, ．／％) —
# CJK abstracts/prose often carry full-width digits, and `\d` is Unicode-aware so '４５' was
# extracted verbatim and never matched an ASCII '45' source → false 'suspect'.
# R30: do NOT fold the full-width comma U+FF0C '，'. In this corpus it is ALWAYS a CJK clause /
# list separator, NEVER a thousands grouper (corpus scan: 0 '\d，\d{3}' ambiguous shapes, 32
# clause shapes like 'n=20，1.3 g' / '率比 0.61，95% CI'). Folding it to ASCII ',' let it be
# absorbed as a thousands separator ('74,291，6–17岁'→'742916'; '102,865，3,358'→'102865335' after
# the genuine '3,358' collapsed) → faithful sample-size claims went false-suspect → write_gate HARD
# FAIL. ASCII thousands ('74,291' / '1,234,567') are untouched — they use ASCII ',' directly.
_FULLWIDTH_NUM = {0xFF10 + i: ord("0") + i for i in range(10)}
_FULLWIDTH_NUM[0xFF0E] = ord(".")
_FULLWIDTH_NUM[0xFF05] = ord("%")


def _fold_fullwidth(text: str) -> str:
    return text.translate(_FULLWIDTH_NUM)

# R7/R8: an analyte / biomarker / entity name with a glued digit — its internal digit is NOT
# a claim number; mis-extracting it made _number_in_source miss → false 'suspect' → ENFORCED
# write-gate FAIL on a faithful claim. Two shapes:
#  · LETTER-led containing a digit: HbA1c, CoQ10, B12, PM2.5, VO2max
#  · DIGIT-led followed by an UPPERCASE letter (chemical/receptor naming): 5-HT, 8-OH,
#    25(OH)D, 3-PBA — but NOT '5-year'/'12-month' (lowercase word → a real duration number).
_GLUED_DIGIT_NAME_RE = re.compile(
    # R9b: ASCII-only trailing class — `\w` matched CJK, so 'CoQ10可降低33%' swallowed the
    # real number 33 (→ false-NEGATIVE, weakened number-faithfulness on CJK reviews).
    # R25: NO '/' in the letter-led classes — '/' let the name greedily bridge a slash-separated
    # label/unit onto a REAL value ('K/300 mmol Na'→'K/300' ate the 300 sodium dose; 'fan/30°C'→
    # 'fan/30' ate the 30°C; 'C/0.2 m/s'→ate the 0.2). Without '/', 'K/' stops the match (no digit
    # before '/') so 300/30/0.2 survive; real analytes (CD62L/PM2.5/5-HT/IGF-1/25(OH)D) carry no
    # pre-digit '/' so they are still stripped, and units (mg/dL, mL/min) were never analytes.
    # R26: a glued name with a digit may carry a trailing '/digit' as RATIO notation (T1/2 half-
    # life, omega-3/6) — its right operand is not a claim number. The '(?:/\d...)?' fires only when
    # a digit precedes the '/' (the name has a digit), so a bare value 'K/300' (no digit before
    # the '/') is untouched and 300 still survives.
    # R32: a '(' that opens a DECIMAL data value '(0.67)' is NOT analyte naming — 'BW(0.67)' (the
    # Kleiber metabolic-weight exponent, written 'MJ/kg BW(0.67)' in source) had its 0.67 eaten as
    # if part of the name → a faithful '0.46 MJ/kg^0.67' claim went suspect → write_gate HARD FAIL.
    # Real analyte parens hold letters '(OH)' or a charge '(2+)', never a bare decimal; the
    # '\((?!\d+\.\d+\))' lookahead lets the name span those but stops it before a decimal-paren, so
    # the decimal survives as a checkable number. HbA1c/CoQ10/B12/PM2.5/IGF-1/Ca(OH)2 still strip.
    r"[A-Za-z](?:[A-Za-z.\-)]|\((?!\d+\.\d+\)))*\d(?:[A-Za-z0-9.\-)]|\((?!\d+\.\d+\)))*(?:/\d[\d.]*)?"
    # digit-led chemical/receptor/isotope/count naming: 5-HT, 8-OH-dG, 25(OH)D, 3-PBA, AND the
    # NO-PUNCTUATION class 13C / 15N / 18F / 26G / 16S / 1M / 13M / 8OHdG. R22 tried to require a
    # hyphen/paren here (to keep a glued-unit dose '5000IU' checkable) but that REGRESSED the
    # no-punctuation class into spurious claim numbers (13C → leaks '13') → real faithful→suspect
    # flips on the corpus (R23). Reverted: strip all digit-led-uppercase tokens. The glued-dose
    # fail-open it leaves is latent (0/62 topics; project convention writes spaced units '5000 IU',
    # which are checked) and backstopped by the entailment judge.
    r"|\d[\d().\-]*[A-Z][A-Za-z0-9()./-]*"
)

# R24: PDF-extracted SOURCE text loses spaces, gluing a real data VALUE to a comparator
# ('55.6%vs48.5%') or to a unit ('-12.9Nm'). The source-side analyte strip (R20) then eats the
# real value as if it were an analyte name → the claim's number is reported missing → false
# 'suspect'. Re-insert a space at these specific boundaries BEFORE the strip so the value survives.
# This does NOT reopen R20's analyte anti-collision (PM2.5 / CoQ10 / B12 / 25OHD / 8OHdG / 13C are
# neither a comparator+digit nor a digit+unit, so they are still stripped).
_DEGLUE_COMPARATOR_RE = re.compile(r"(?<![A-Za-z])(vs|versus|or|rr|hr|ci|sd|se|md|smd|nnt)(?=\d)", re.IGNORECASE)
# shared measurement-unit alternation (bare 'L' kept here for the digit+unit deglue, excluded in
# the RANGE rule below since it would split the analyte 'IL-6').
_UNIT_ALT = (r"Nm|kg|mg|µg|μg|mcg|ng|pg|mmHg|mmol|mol|mEq|mL|ml|dL|kPa|MPa|Hz|kHz|"
             r"kcal|kJ|IU|nm|cm|mm|µm|μm|ms")
_DEGLUE_UNIT_RE = re.compile(r"(\d)(?=(?:" + _UNIT_ALT + r"|L)\b)")
# R34: a measurement-unit RANGE written 'unit-digit unit' ('1 μm-10 μm', '200 nm-400 nm', '5 mg-10
# mg') — the letter-led _GLUED_DIGIT_NAME_RE matched the substring 'm-10' (the unit's ASCII letter +
# the hyphen + the upper bound) and stripped it as if it were an analyte, deleting the real range
# upper bound from source → a faithful range claim went false-suspect → write_gate HARD FAIL (topic
# 空调异味, '1–10 μm' vs source '1 μm-10 μm'). Insert a space before the hyphen so the analyte regex
# can't bridge unit→digit.
# R35/R36: the hyphen-digit must be followed by the SAME unit repeated (a true range '1 μm-10 μm',
# '0.5 cm-30 cm'), NOT a bare 'unit-digit' inverse-power exponent ('kg-1', 'mg kg-1', 'ml.min-1') nor
# an inverse-COMPOUND ('mL-1 kg') — degluing either exposed a spurious standalone '1' that a fabricated
# claim ('约 1 个数量级') then matched (fail-open, topic 木浆海绵). The '\1' backreference requires the
# trailing unit to equal the leading one; real ranges always repeat the unit, inverse units never do.
# Corpus: this fires on exactly 2 real sources (μm-10 μm, cm-30 cm), 0 inverse — so it's tight.
_DEGLUE_UNIT_RANGE_RE = re.compile(
    r"(" + _UNIT_ALT + r")(?=-\d[\d.]*\s*\1\b)"
)


def _deglue_source(text: str) -> str:
    text = _DEGLUE_COMPARATOR_RE.sub(lambda m: m.group(1) + " ", text)
    text = _DEGLUE_UNIT_RANGE_RE.sub(lambda m: m.group(1) + " ", text)
    return _DEGLUE_UNIT_RE.sub(lambda m: m.group(1) + " ", text)


# R25: the project MANDATES LaTeX math ($...$), whose thousands separator is the brace-comma '{,}'
# (or '\,'). '$n=15{,}226$' was fragmented into ['15','226'] → '226' reported missing though the
# value 15226 IS in source → false 'suspect'. Collapse a digit-grouping '{,}' / '\,' (followed by
# exactly 3 digits) so 15{,}226 → 15226. Range-gated to 3-digit groups so 'H_{0}' / 'A_{2A}' /
# non-grouping '2{,}5' are untouched.
_LATEX_THOUSANDS_RE = re.compile(r"(?<=\d)(?:\{,\}|\\[,;\s])(?=\d{3}(?!\d))")


def _normalize_latex_thousands(text: str) -> str:
    return _LATEX_THOUSANDS_RE.sub("", text)

# R12: a 4-digit publication YEAR (1900–2099) in a CITATION/temporal context is NOT a claim
# number — prose cites studies as 'Suez 等 2022' / '2022 年' / '(Schiffman 2023)' / 'WHO 2023',
# and the source abstract rarely repeats its own publication year, so extracting it made
# _number_in_source miss → false 'suspect' → write-gate HARD FAIL. ~48% (910/1894) of all
# missing-number suspects were pure-year. The filter is CONTEXT-gated, NOT range-gated: a
# real in-range data number ('纳入 1998 名受试者', 'N=2000') has no author/年/paren cue and
# survives; only a year cued by an author token, a trailing 年, or an open paren is dropped.
_Y = r"(?:19|20)\d\d"
# R14: pre-extraction we strip ONLY the two publication-year forms that can NEVER be a dose/count,
# so no unit whitelist is needed and nothing data-like is ever silently dropped:
#   · 'YEAR 年'   — a 4-digit year bound to 年 (excluding the compounds 年龄 'age' / 年代 'era')
#   · '(YEAR)'    — a year sitting ALONE inside closing parens ('(2022)', '(2022a)'); a dose
#                   would read '(2000 mg)' with a unit before ')', so it never matches.
# The dominant 'Author YEAR' form ('Suez 等 2022', 'WHO 2023', '(Schiffman 2023)') is NOT
# heuristically stripped here — it is handled PRECISELY in evaluate() by suppressing a *missing*
# claim number equal to the cited entry's own publication year (ground truth). This replaces
# R12/R13's CapitalWord-author + unit-whitelist heuristic, which over-stripped real data numbers
# ('Selenium 2000 mcg', '(2050 参与者)', 'Sodium 2000 mg') → fail-open.
_PAREN_YEAR_RE = re.compile(r"([（(]\s*)" + _Y + r"[a-z]?(\s*[）)])")
_NIAN_YEAR_RE = re.compile(r"(?<![.\d])" + _Y + r"(?=\s*年(?![龄代]))")


def _strip_prose_years(text: str) -> str:
    text = _NIAN_YEAR_RE.sub(" ", text)
    return _PAREN_YEAR_RE.sub(lambda m: m.group(1) + m.group(2), text)


# A number followed by a unit/counter is a MEASURED value, never a bare publication-year mention.
# R15: gate the entry.year suppression on this so a real data value that coincidentally equals the
# cited work's pub year ('2000 mcg' citing a 2000 paper) is still checked (no fail-open), while a
# bare 'Suez 等 2022' year is suppressed.
# R16 (this audit): the data suffix can be (a) a GLUED ASCII letter/percent (no space — '45%',
# '2000mg'), (b) a percent/per-mille after optional space ('45 %'), (c) a CJK counter ('2050 例'),
# OR (d) a space + a RECOGNIZED short unit token ('2000 mg', '5 kg', '300 IU'). The old rule treated
# a space + ANY ASCII letter as a unit — so the ubiquitous 'Author YEAR studytype' phrasing
# ('Lertrit 2018 RCT', 'Suez 2022 RCT', 'Smith 2019 meta', 'Swithers 2013 theory') had its year
# read as a measured value → NOT suppressed → false 'suspect' → write-gate HARD FAIL on a faithful
# claim. A study-descriptor word (RCT/meta/cohort/trial/study/review/theory…) is NOT a unit, so the
# year stays bare and is correctly suppressed; '2000 mg' (real unit) still counts as data.
_GLUED_DATA_RE = re.compile(r"[A-Za-z%‰]|[例名人位份项个篇条次岁]")  # no leading space: unit glued to digit
_PCT_OR_COUNTER_RE = re.compile(r"\s*(?:[%‰]|[例名人位份项个篇条次岁])")  # %/per-mille/CJK counter, optional space
# recognized measurement units that may sit after a single space; matched whole-word so 'meta'/'RCT'
# (study descriptors) never qualify. Case-insensitive; covers mass/volume/energy/concentration/IU.
_SPACED_UNIT_RE = re.compile(
    r"\s+(?:mg|mcg|µg|μg|ug|ng|pg|kg|g|mL|ml|L|dL|dl|IU|U|kcal|cal|kJ|J|"
    r"mmol|nmol|µmol|μmol|mol|mEq|mmHg|mg/kg|mg/dL|g/L|ppm|ppb|Gy|Sv|Bq|Hz|"
    r"mm|cm|m|km|nm|µm|μm|h|hr|min|s|d|wk|mo|yr)(?![A-Za-z])",
    re.IGNORECASE,
)


def _claim_has_bare_year(num: str, claim_text: str) -> bool:
    """True if `num` occurs in claim_text at least once NOT immediately followed by a unit/counter
    — i.e. as a bare year mention rather than a measured value (R16: a space-separated study
    descriptor like 'RCT'/'meta'/'theory' is NOT a unit, so it leaves the year bare)."""
    for m in re.finditer(r"(?<![\d.])" + re.escape(num) + r"(?![\d.])", claim_text):
        rest = claim_text[m.end():]
        is_data = (
            bool(_GLUED_DATA_RE.match(rest))        # 45% / 2000mg — unit glued to the digit
            or bool(_PCT_OR_COUNTER_RE.match(rest)) # 45 % / 2050 例 — percent/CJK counter
            or bool(_SPACED_UNIT_RE.match(rest))    # 2000 mg / 5 kg — recognized spaced unit
        )
        if not is_data:
            return True
    return False


# R23: a LaTeX subscript/superscript OPERAND is notation, not a claim number — 'H_0' / '\\mu_1' /
# 'A_{2A}' / '\\sigma^2' / '\\chi^2' / 'R^2' / '(I_1-I_2)' leaked their 0/1/2 as ghost claim numbers
# → false 'suspect' → write-gate HARD FAIL on math-heavy reviews (ANOVA, rigid-body, OLS/WLS…).
# Strip the operand after a '_' or '^' (a brace group, an alnum run, or a single digit). This does
# NOT touch real data values, which never sit in a _/^ operand position ($OR=3.18$ / $\\geq 60$ /
# $n=15{,}226$ / $I_b-I_a=17.2$ all survive); it deliberately does NOT blanket-strip $...$ (real
# data lives there — blanket-stripping would let a fabricated dose hide in math → fail-open).
_LATEX_SUBSUP_RE = re.compile(r"[_^]\s*(?:\{[^{}]*\}|[A-Za-z0-9]+|.)")


def _extract_numbers(text: str) -> list[str]:
    """Extract bare numeral strings from text (fold full-width → ASCII, strip commas); skip
    digits glued inside analyte/biomarker names (R7); strip LaTeX sub/superscript operands (R23);
    drop citation/temporal publication years (R12); include a range's upper bound (R8); normalize a
    leading-dot decimal '.85' → '0.85' (R11) so value comparison is exact."""
    text = _normalize_latex_thousands(_LATEX_SUBSUP_RE.sub(" ", _fold_fullwidth(text)))
    # R26: collapse genuine 3-digit thousands grouping on the CLAIM side too (mirror the source
    # side, R9) — a claim 'n=77 917' / '纳入 74 291 名' was split into ['77','917'] while the source
    # joins to 77917 → false 'suspect'. _THOUSANDS_RE range-gates to real groups so 'RR 0.27 95% CI'
    # is untouched.
    text = _THOUSANDS_RE.sub(lambda m: re.sub(r"[,\s]", "", m.group(0)), text)
    text = _GLUED_DIGIT_NAME_RE.sub(" ", _strip_prose_years(text))
    nums = []
    for m in _NUM_TOKEN_RE.finditer(text):
        for g in (m.group(1), m.group(2)):  # lower bound + optional range upper bound
            if g:
                g = g.replace(",", "")
                if g.startswith("."):
                    g = "0" + g
                nums.append(g)
    return nums


# R9: collapse ONLY genuine thousands-grouping ('74 291'→'74291', '1,234,567'→'1234567')
# — a digit run grouped into trailing 3-digit blocks, not preceded by a decimal point and
# not followed by more digits. The old `(?<=\d)[,\s](?=\d)` glued ANY two space-separated
# numbers, so 'RR 0.27 95% CI' became '0.2795' and both numbers vanished → false 'suspect'
# (the commonest effect-size format). C11 European-grouping cases still collapse.
# R31: second alternation = South-Asian *lakh* grouping ('1,18,171'→'118171', '12,34,567') — a
# 1–2 digit head, ≥1 TWO-digit comma group, then a final THREE-digit group. R30's `\d+(?:,\d{3})*`
# token class cut '1,18,171' into ['1181','71'] (the stray 2-digit group defeats the 3-digit rule)
# → a faithful sample-size claim went false-suspect → write_gate HARD FAIL (topic 食材embedding…).
# The 2-digit-group requirement keeps single-digit chemical locants (2,4-TDI / 1,3-丙二醇 / β-1,4)
# and standard 3-grouping out of this branch (corpus: 1 lakh run, 30 locant runs, 115 standard).
_THOUSANDS_RE = re.compile(
    r"(?<![\d.])(?:\d{1,3}(?:[,\s]\d{3})+|\d{1,2}(?:,\d{2})+,\d{3})(?!\d)"
)


def _collapse_thousands(text: str) -> str:
    # R9b: strip ALL matched separators (comma + ANY whitespace incl. NBSP U+00A0 /
    # thin-space U+2009 / tab / newline) — the prior `.replace(",","").replace(" ","")`
    # left non-ASCII whitespace residue, so '74<NBSP>291' didn't collapse → false suspect.
    # R11: fold full-width digits first so source tokenization sees ASCII numerals.
    # R25: also collapse a LaTeX brace-comma / '\,' thousands separator ('15{,}226' → '15226').
    text = _normalize_latex_thousands(_fold_fullwidth(text))
    return _THOUSANDS_RE.sub(lambda m: re.sub(r"[,\s]", "", m.group(0)), text)


# R11: a candidate numeric token in (already full-width-folded + thousands-collapsed) source —
# integer, standard decimal, or leading-dot decimal. `(?<![\d.])` keeps the scan off mid-number
# positions so '45' never starts inside '145' / '0.45'.
_SOURCE_NUM_RE = re.compile(r"(?<![\d.])(?:\d[\d]*(?:\.\d+)?|\.\d+)")


def _parse_decimal(tok: str) -> Decimal | None:
    try:
        return Decimal(tok.replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _source_number_tokens(norm: str) -> list[tuple[int, int, Decimal]]:
    """Every numeric token in a normalized source as (start, end, value)."""
    out: list[tuple[int, int, Decimal]] = []
    for m in _SOURCE_NUM_RE.finditer(norm):
        val = _parse_decimal(m.group())
        if val is not None:
            out.append((m.start(), m.end(), val))
    return out


def _number_in_source(bare: str, source: str) -> tuple[bool, str]:
    """R11 structural fix: a claim number is 'found' iff some source numeric token is
    Decimal-EQUAL by VALUE — replacing the regex-substring stack whose lexical patches
    (decimal boundary, thousands, symmetric trailing-zero, leading-dot, full-width) each
    leaked a fresh false-'suspect' edge across R8–R11. Value equality settles them all at
    once: 0.80≡0.8, 45≡45.0, ４５≡45, .85≡0.85 match; 1.2≠1.25, 45≠145, 45≠4.50 don't.
    Thousands grouping ('74 291'→74291) and 'RR 0.27 95% CI' (two tokens, not glued) are
    handled by _collapse_thousands before tokenizing. Returns (found, span).

    R36: matching is strictly EXACT — the R34/R35 marker-gated rounding tolerance (约85% ≡ 85.52%)
    was REVERTED. It kept introducing fail-opens: a legitimately-marked approximate integer
    coincidentally round/truncate-matched an UNRELATED source decimal of a different quantity
    ('≈4倍' risk ↔ source '4.5 years'; '~1.0–1.2' protein ↔ source '1.2 g calcium'), and truncation
    (needed for 85.52→85) is inseparable-by-value from the small-number spills. The deliberate
    zero-hallucination contract (tests assert 45≠45.5) is the right default — approximate prose
    should cite the verbatim source value (or one-decimal) rather than have the gate guess."""
    # R20: strip analyte/biomarker names from the SOURCE too (symmetric with _extract_numbers'
    # claim-side strip) — else a digit GLUED inside a name (the 2.5 in 'PM2.5', the 12 in 'B12',
    # the 10 in 'CoQ10') is tokenized as a numeric value, and a FABRICATED claim number whose
    # value collides with it is judged 'found' → a true hallucinated effect size passes the
    # primary number gate (fail-open). Health/nutrition abstracts are saturated with such names.
    norm = _collapse_thousands(_GLUED_DIGIT_NAME_RE.sub(" ", _deglue_source(source)))
    target = _parse_decimal(_fold_fullwidth(bare))
    if target is None:
        return False, "-"
    for start, end, val in _source_number_tokens(norm):
        if val == target:
            s = max(0, start - 30)
            e = min(len(norm), end + 30)
            return True, norm[s:e].replace("\n", " ")
    return False, "-"


_COLOCATION_WINDOW = 160  # chars — a claim's numbers must co-occur within one window


def _colocation_span(numbers: list[str], source: str) -> str | None:
    """M5 (spec §0.6.e b): a claim's numbers must appear TOGETHER in one source
    window — not scattered. Returns the smallest window (≤_COLOCATION_WINDOW chars)
    containing every number, or None if they don't co-locate (→ suspect: the claim
    likely stitched numbers from different rows, e.g. RR 0.27 vs adherence 0.72, or
    swapped CI bounds). Single-number claims always co-locate trivially."""
    # R20: strip analyte/biomarker names from the SOURCE too (symmetric with _extract_numbers'
    # claim-side strip) — else a digit GLUED inside a name (the 2.5 in 'PM2.5', the 12 in 'B12',
    # the 10 in 'CoQ10') is tokenized as a numeric value, and a FABRICATED claim number whose
    # value collides with it is judged 'found' → a true hallucinated effect size passes the
    # primary number gate (fail-open). Health/nutrition abstracts are saturated with such names.
    norm = _collapse_thousands(_GLUED_DIGIT_NAME_RE.sub(" ", _deglue_source(source)))
    if len(numbers) < 2:
        return None
    # all VALUE-equal source positions of each claim number (R11: Decimal, not regex)
    tokens = _source_number_tokens(norm)
    positions: list[list[int]] = []
    for bare in numbers:
        target = _parse_decimal(_fold_fullwidth(bare))
        if target is None:
            return None
        hits = [start for (start, _end, val) in tokens if val == target]
        if not hits:
            return None  # a missing number is handled by _number_in_source separately
        positions.append(hits)
    # greedy: for the first number's each occurrence, can the others fall within window?
    for p0 in positions[0]:
        lo, hi = p0, p0
        ok = True
        for others in positions[1:]:
            near = [q for q in others if abs(q - p0) <= _COLOCATION_WINDOW]
            if not near:
                ok = False
                break
            lo, hi = min(lo, min(near)), max(hi, max(near))
        if ok and (hi - lo) <= _COLOCATION_WINDOW:
            s, e = max(0, lo - 10), min(len(norm), hi + 20)
            return norm[s:e].replace("\n", " ")
    return None


# A CI / numeric-range bound pair in a claim: "a (95% CI b to c)", "a–b", "a 至 b".
# R3-A10: include fullwidth tilde 〜/～ used in CJK prose ranges.
_RANGE_PAIR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:[-–—~～〜]|至|to)\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
# R3-A4: ratio-like number (0.x / N.x) — used to gate the restored scattered-swap flag
# to numbers that plausibly belong to ONE estimate (RR/HR/OR/CI), so a legit
# multi-outcome decision sentence (23% + 10%, mixed magnitudes) is not false-flagged.
_RATIO_NUM_RE = re.compile(r"^\d\.\d+$")


def _range_pair_issue(claim_text: str, source: str) -> str | None:
    """M5 refocused (R2-F4): each CI/range bound pair the claim states must appear in the
    source in the SAME order. The claim writes (a, b); if the source contains the pair
    ADJACENT only in the reverse order (b…a) and not (a…b), the claim inverted the bounds
    (e.g. 0.89 至 0.65 when the source says 0.65 to 0.89) → flag. Order-based, so a legit
    multi-outcome decision sentence (two independent stats) is never false-flagged."""
    norm_src = _norm_digits(source)
    for m in _RANGE_PAIR_RE.finditer(_norm_digits(claimids.strip(_CITATION_RE.sub("", claim_text)))):
        a, b = m.group(1), m.group(2)
        if a == b:
            continue
        # R37: only a DESCENDING claim pair (a > b, e.g. '0.72–0.27') is a suspected bound swap — the
        # writer wrote the CI high-to-low. A properly ASCENDING claim ('1.50–1.78') is correctly
        # ordered and must NOT be flagged just because the source lists the two values in a different
        # order — that source order is often two PARALLEL point estimates ('women and men of 1.78 and
        # 1.50'), not a reversed CI (居家体脂 @mccarthy2023 was a needs_review false-positive). A real
        # inversion is always a descending claim, so this drops 0 true swaps (no fail-open).
        av, bv = _parse_decimal(a), _parse_decimal(b)
        if av is None or bv is None or av <= bv:
            continue
        fwd = re.search(re.escape(a) + r"\D{0,8}" + re.escape(b), norm_src)
        rev = re.search(re.escape(b) + r"\D{0,8}" + re.escape(a), norm_src)
        if rev and not fwd:
            return f"区间/CI 上下界与源相反（claim {a}-{b}，源作 {b}-{a}）—互换疑似（b/M5）"
    return None


# ---------------------------------------------------------------------------
# Study-type conflict detection
# ---------------------------------------------------------------------------

# Maps study-type keywords in prose to canonical category
_PROSE_STUDY_TYPES: dict[str, str] = {
    "rct": "rct",
    "随机对照": "rct",
    "随机分组": "rct",
    "随机双盲": "rct",
    "randomized controlled": "rct",
    "randomised controlled": "rct",
    "longitudinal": "longitudinal",
    "纵向": "longitudinal",
    "前瞻": "cohort",
    "prospective": "cohort",
    "队列": "cohort",
    "cohort": "cohort",
    "meta-analysis": "meta",
    "meta分析": "meta",
    "荟萃": "meta",
    "横断面": "cross_sectional",
    "cross-sectional": "cross_sectional",
    "cross sectional": "cross_sectional",
}
# R8: bare ambiguous keys removed — '随机' matched 随机森林/随机抽样/随机误差; 'meta' matched
# metabolic/metadata/metastatic; both false-fired _study_type_conflict → false 'suspect' →
# enforced write-gate FAIL on a faithful claim. Real RCTs/metas keep their explicit keys.

# What prose claims vs what source / entry says that CONTRADICTS
_CONFLICT_PAIRS: list[tuple[str, str]] = [
    ("longitudinal", "cross_sectional"),
    ("rct", "cross_sectional"),
    ("rct", "cohort"),
    ("cohort", "cross_sectional"),
    ("meta", "cross_sectional"),
]


def _detect_study_type(text: str) -> str | None:
    """Return the first study-type category found in text, or None. R8: ASCII keywords
    match on WORD boundaries (so 'meta' doesn't fire inside 'metabolic'/'metadata'); CJK
    keywords stay substring (no word delimiter), with the ambiguous bare keys removed."""
    t_lower = text.lower()
    for kw, cat in sorted(_PROSE_STUDY_TYPES.items(), key=lambda x: -len(x[0])):
        if kw.isascii():
            if re.search(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])", t_lower):
                return cat
        elif kw in t_lower:
            return cat
    return None


# R25: broad design keywords for confirming a design IN THE SOURCE (the actual paper). Broader
# than the prose-label keywords — a source RCT says 'randomized' (not necessarily 'randomized
# controlled'), so the prose 'RCT' label is faithful even when entry.study_type is the CrossRef
# under-classification 'other' and the source ALSO mentions a weaker incidental design.
_SOURCE_DESIGN_KW: dict[str, re.Pattern] = {
    # R26: exclude 'non-randomized' / 'quasi-randomized' / '非随机' / '准随机' — confirming RCT on
    # those would let a prose 'RCT' label over a NON-randomized source pass (fail-open). ('\b'
    # already excludes the no-separator 'nonrandomized' — no word boundary before 'randomi'.)
    "rct": re.compile(
        r"(?<!non[-\s])(?<!quasi[-\s])\brandomi[sz]ed?\b|\brct\b|(?<![非准不未])随机(?:对照|分组|双盲|化)?"
        # R28: 'randomly assigned/allocated to treatment/arms/groups' IS RCT allocation (it had been
        # confirmable only when the abstract also wrote 'randomized'; close that latent gap).
        r"|\brandom(?:ly)?\s+(?:assign|allocat)(?:ed)?\s+(?:to\s+)?(?:the\s+|a\s+|each\s+)?(?:treatment|group|arm|intervention)",
        re.IGNORECASE,
    ),
    "meta": re.compile(r"\bmeta[-\s]?analys[ei]s\b|systematic\s+review|荟萃|meta\s*分析", re.IGNORECASE),
    "cohort": re.compile(r"\bcohort\b|\bprospective\b|队列|前瞻", re.IGNORECASE),
    "longitudinal": re.compile(r"\blongitudinal\b|纵向", re.IGNORECASE),
    "cross_sectional": re.compile(r"\bcross[-\s]?sectional\b|横断面", re.IGNORECASE),
}
# R25: a NEGATED / wishlist design mention ('没有直接 RCT', '缺乏头对头 RCT', 'no RCT exists',
# 'lack of cohort data') is the prose saying the design is ABSENT — NOT asserting the cited paper
# HAS it — so it is never an over-claim.
_NEGATED_DESIGN_RE = re.compile(
    r"(?:没有|缺乏|缺少|尚无|未见|不存在|未有|缺)[^。\n；;]{0,20}"
    r"(?:rct|随机|meta|荟萃|队列|cohort|cross[-\s]?sectional|横断)"
    r"|(?:\bno\b|lack(?:s|ing)?(?:\s+of)?|absence\s+of|without)\s+(?:[a-z-]+\s+){0,4}"
    r"(?:rct|randomi[sz]ed?|meta[-\s]?analys|cohort)",
    re.IGNORECASE,
)


# R26: a 'randomized' string in the SOURCE that is wishlist/future ('future randomized trials are
# needed'), negated ('no randomized comparison was possible'), or sampling/allocation ('randomized
# sampling frame', 'randomly allocated sequence') is NOT a design assertion about the cited paper —
# confirming RCT on it let a real prose-RCT-vs-cross-sectional over-claim pass (fail-open). Remove
# those contexts before confirming.
# R27: tightened so benign words that merely sit near 'randomized' in a real RCT title
# ('Recommended Daily Allowance in randomized trials', 'IBS Without Constipation Randomized
# Controlled Trial') no longer strip the genuine design signal. Three NARROW non-design shapes:
#  (A) a negation/absence DIRECTLY before 'randomized' (no words between): 'no randomized',
#      'lack of randomized', 'without randomized' — NOT 'without <disease> randomized';
#  (B) a future/larger/further word DIRECTLY before 'randomized': 'future randomized trials';
#  (C) a wishlist TAIL after 'randomized … (trial|study|data)': '… needed/warranted/required';
#  (D) randomization used for SAMPLING (not RCT allocation): 'random sampling', 'randomized
#      number'. 'randomly assigned/allocated to treatment' is RCT randomization → NOT stripped.
_RCT_NONDESIGN_RE = re.compile(
    # (A) negation/absence DIRECTLY before 'randomized'
    r"(?:\bno|\black(?:s|ing)?\s+of|\bwithout|\babsence\s+of|缺乏|尚无|没有|未见|缺少)\s+\brandomi[sz]ed?"
    # (B) 'future' DIRECTLY before randomized (unambiguous wishlist). NOT larger/further/additional
    #     — 'a larger randomized trial of 900' is a REAL completed RCT; those are excluded only by
    #     the wishlist TAIL in (C).
    r"|(?:future|未来)\s+\brandomi[sz]ed?"
    # (C) a wishlist TAIL after 'randomized …': '… needed/warranted/required' (covers 'larger
    #     randomized trials are needed' without stripping 'larger randomized trial of 900').
    r"|\brandomi[sz]ed?\b[^.;:\n。；]{0,40}?\b(?:needed|warranted|required|awaited|pending|lacking|unavailable)\b"
    # (D) randomization used for SAMPLING (not RCT allocation)
    r"|\brandomi[sz]ed?\s+(?:sampl|order|number)"
    r"|random(?:ly)?\s+sampl"
    # CJK wishlist/negation — unambiguous triggers + a design noun (NOT bare 进一步/更大, which
    # describe a real bigger RCT)
    r"|(?:需要|缺乏|尚无|呼吁|缺少|没有|未来)[^。\n；;]{0,12}?随机[^。\n；;]{0,8}?(?:试验|研究|对照|证据|数据)"
    r"|随机(?:对照)?(?:试验|研究)?[^。\n；;]{0,10}?(?:仍\s*)?(?:缺乏|不足|尚未|有待|未能)",
    re.IGNORECASE,
)


def _source_has_design(cat: str, source: str) -> bool:
    rx = _SOURCE_DESIGN_KW.get(cat)
    if not rx:
        return False
    if cat == "rct":
        source = _RCT_NONDESIGN_RE.sub(" ", source)  # drop wishlist/negated/sampling randomized
    return bool(rx.search(source))


def _study_type_conflict(claim: str, entry: refs.Entry, source: str) -> bool:
    """True if prose asserts a stronger design than entry metadata or source."""
    prose_type = _detect_study_type(claim)
    if prose_type is None:
        return False
    # R25: prose merely NOTING the absence of a design is not an over-claim.
    if _NEGATED_DESIGN_RE.search(claim):
        return False

    entry_st = (entry.get("study_type") or "").lower()
    source_type = _detect_study_type(source)

    # Map entry study_type to our category
    _ENTRY_TO_CAT: dict[str, str] = {
        "rct": "rct",
        "meta": "meta",
        "cohort": "cohort",
        "case_control": "cross_sectional",
        "review": "meta",
        "cross_sectional": "cross_sectional",
        "other": "",
    }
    entry_cat = _ENTRY_TO_CAT.get(entry_st, "")

    # R9b: entry.study_type (filled by verify) is the reliable design signal. If it CONFIRMS
    # the prose label, the claim is faithful — don't let the source's incidental mention of
    # another design (a cohort abstract noting 'unlike cross-sectional surveys'; a meta-
    # analysis listing its included cross-sectional studies) false-fire a conflict → suspect.
    if entry_cat and entry_cat == prose_type:
        return False

    # R25: if the SOURCE confirms the prose's OWN design (prose 'RCT' and source says 'randomized'/
    # '随机'), the label is faithful even when the source ALSO mentions a weaker INCIDENTAL design
    # (a cross-over RCT noting 'a larger cohort to confirm'; 'cross-sectional area' as a
    # measurement). This closes the entry.study_type='other' trap — for 'other' entry_cat='' so the
    # R9b confirm-guard above cannot fire, and the source-fallback below would mis-fire.
    if _source_has_design(prose_type, source):
        return False

    # R25: if the prose ALSO names the source's actual (weaker) design, the citation refers to THAT
    # design — the stronger design keyword sits elsewhere in a multi-design sentence ('多数运动 RCT
    # 按跌倒设计；大型队列显示…[@cohort-paper]' cites the cohort, not an RCT) → not an over-claim.
    if source_type and _source_has_design(source_type, claim):
        return False

    for stronger, weaker in _CONFLICT_PAIRS:
        if prose_type == stronger:
            if entry_cat == weaker:  # entry metadata is the reliable signal — always trust it
                return True
            # R17: a meta-analysis / cohort claim ROUTINELY lists or contrasts the weaker designs
            # it INCLUDES ('we included 8 RCTs and 4 cross-sectional studies', 'unlike cross-
            # sectional surveys'), so a source mention of the weaker design is NOT an over-claim
            # for them → don't deterministically hard-FAIL (caught instead by high-risk-grounding
            # + the LLM entailment judge). A longitudinal/RCT claim does not list other designs,
            # so a source that describes ITSELF as the weaker design IS a real over-claim signal.
            if prose_type not in ("meta", "cohort") and source_type == weaker:
                return True
    return False


# ---------------------------------------------------------------------------
# Polarity word detection
# ---------------------------------------------------------------------------

_POLARITY_WORDS = [
    "最高", "最低", "most", "least",
    "增加", "减少", "increase", "decrease",
    "升高", "降低",
    "有效", "无效", "effective", "ineffective",
    "改善", "恶化", "improve", "worsen",
    "正相关", "负相关", "positive correlation", "negative correlation",
    "最多", "最少",
]

def _has_polarity(text: str) -> bool:
    t_lower = text.lower()
    return any(w in t_lower for w in _POLARITY_WORDS)


# ---------------------------------------------------------------------------
# Risk grading (F12), section location (F5/F6), negation (F7), scope-creep (F4)
# ---------------------------------------------------------------------------

# Deterministic surface cues that force a claim to high-risk (spec §0.6 分档
# fail-safe: numbers / comparison / negation / counts / author-conclusion). When
# in doubt the grade is high — the floor must be strict.
_COMPARISON_RE = re.compile(
    r"\bvs\.?\b|对比|相比|更(?:高|低|多|少|强|弱|好|差)|高于|低于|优于|劣于|≥|≤|倍|fold|\bthan\b",
    re.IGNORECASE,
)
# Broadened (M7, spec §0.6.e b3): mainstream CN/EN negation & non-significance
# phrasings — 未发现/未达/未观察到, 无统计学差异/意义, 不具显著性, 阴性, no (significant)
# effect/difference/benefit, null result. Used by risk grading AND the b3 span check.
_NEGATION_RE = re.compile(
    r"\bnot\b|\bno\b|\bnone\b|non[-\s]?significant|null\s*result|"
    r"no\s+(?:significant\s+)?(?:effect|difference|benefit|association)|"
    r"未(?:见|能|会|显著|发现|达|观察到|检出)|没有|无(?:显著|效|关联|获益|差异|意义)|"
    r"无统计学(?:差异|意义|显著)|不(?:显著|能|会|存在|具(?:有)?(?:统计|显著)|改善)|"
    r"差异(?:无统计学意义|不(?:显著|明显))|阴性(?:结果)?|缺乏|absence of|fail(?:ed|s)? to",
    re.IGNORECASE,
)
_COUNT_SET_RE = re.compile(
    r"\d+\s*项(?:研究|试验|证据)?中|多数|大多数|majority|均(?:一致|支持)|"
    r"\d+\s+of\s+\d+|\d+\s*/\s*\d+|[四三两五六七八九十]项(?:研究|试验|中)",
    re.IGNORECASE,
)
_AUTHOR_CONCL_RE = re.compile(
    r"作者(?:认为|结论|指出)|结论(?:认为|是|为)|推荐|建议|应当|应该|\brecommend|\bconclude|guideline|指南",
    re.IGNORECASE,
)
# R3-A2: markers that UNAMBIGUOUSLY attribute a conclusion to an EXTERNAL source — a
# clause tagged type:inference carrying these is transcribing a paper's conclusion (=
# factual, high-risk per spec §0.6.a line 87), not the writer's own synthesis. Narrower
# than _AUTHOR_CONCL_RE: it excludes bare 推荐/建议/应当 (legit decision-review writer
# voice) so it doesn't re-open the R2-F3 false positive.
_TRANSCRIBED_CONCL_RE = re.compile(
    r"作者(?:认为|结论|指出|发现)|该研究(?:认为|结论|发现|推荐|建议)|研究(?:结论|发现)|"
    r"原文(?:结论|推荐|认为)|文献(?:认为|指出|报告)|\bconclude|guideline",
    re.IGNORECASE,
)
# Surrogate→clinical-outcome (M6 / spec §0.6.b 招牌例「降低死亡率」): an effect asserted
# on a HARD clinical outcome is high-risk regardless of surface numbers — it must ground
# on fulltext + face the 3-judge ensemble, not slip through on an abstract + single judge.
_CLINICAL_OUTCOME_RE = re.compile(
    r"死亡率|全因死亡|心血管(?:事件|死亡)|卒中|中风|心梗|心肌梗死|住院(?:率|风险)?|再住院|"
    r"急诊就诊|截肢|透析|肾衰竭?|骨折(?:风险)?|复发(?:率|风险)|生存(?:率|期)|致残|"
    r"mortality|survival|stroke|\bMACE\b|hospitali[sz]ation|incidence of|fracture|recurrence",
    re.IGNORECASE,
)
_EFFECT_VERB_RE = re.compile(
    r"降低|减少|升高|增加|提高|改善|预防|延长|缩短|影响|reduc|increas|improv|lower|raise|prevent|"
    r"associated with|降|升", re.IGNORECASE,
)
_CAUSAL_RE = re.compile(
    r"导致|引起|致使|因果|促使|\bcauses?\b|caused by|leads? to|results? in|gives? rise to",
    re.IGNORECASE,
)
_CORRELATION_RE = re.compile(
    r"相关|关联|伴随|\bassociat|correlat|\blinked\b|相伴", re.IGNORECASE
)

# canonical section a finding/effect must NOT be drawn from (b2). 'methods' added
# (R2): an effect size doesn't come from Methods. 'body' (undetectable) deliberately
# NOT here — extraction often drops fulltext headings, flagging it over-fires.
_NON_FINDING_SECTIONS = {"introduction", "background", "related-work", "hypothesis", "methods"}

_SECTION_HEADINGS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bresults?\b|\bfindings\b|结果", re.IGNORECASE), "results"),
    (re.compile(r"\bmethods?\b|\bmaterials\b|methodology|方法", re.IGNORECASE), "methods"),
    (re.compile(r"\bintroduction\b|引言|前言", re.IGNORECASE), "introduction"),
    (re.compile(r"\bbackground\b|背景", re.IGNORECASE), "background"),
    (re.compile(r"\bdiscussion\b|讨论", re.IGNORECASE), "discussion"),
    (re.compile(r"\bconclusions?\b|结论", re.IGNORECASE), "conclusion"),
    (re.compile(r"related work|prior work", re.IGNORECASE), "related-work"),
    (re.compile(r"\bhypothes[ie]s\b|假设", re.IGNORECASE), "hypothesis"),
    (re.compile(r"\babstract\b|摘要", re.IGNORECASE), "abstract"),
]


# F11 三类「逐-key 盲区」surface cues
_NEGATION_CLAIM_RE = re.compile(
    r"无(?:证据|研究|数据|关联)|未(?:见|发现|检索到|有)|没有(?:证据|研究|发现)|"
    r"no evidence|not\s+found|lack of evidence",
    re.IGNORECASE,
)
_SCOPE_PHRASE_RE = re.compile(
    r"检索范围内|本(?:文|综述)(?:未|尚未)?(?:检索|纳入|发现)|检索到的(?:文献|范围)|"
    r"in\s+(?:our|the)\s+search|within (?:our|the) search",
    re.IGNORECASE,
)
# M12: natural CN phrasings ("8 项研究中 6 项", "6/8") + EN, not just "\d+项中\d+".
_SET_CLAIM_RE = re.compile(
    r"\d+\s*项(?:研究|试验|证据)?中\s*\d+|\d+\s*/\s*\d+\s*(?:项|studies|trials)?|"
    r"多数(?:研究|证据|试验)|大多数(?:研究|试验|证据)|majority of|"
    r"\d+\s+of\s+\d+\s+(?:studies|trials|rcts)",
    re.IGNORECASE,
)


_NEGATION_VOCAB = {"无证据", "未发现", "未见", "没有", "证据", "研究", "数据", "关联"}


def _content_bigrams(text: str) -> set[str]:
    """CJK 2-char bigrams + ≥4-char latin words (CJK has no word delimiters)."""
    latin = {w.lower() for w in re.findall(r"[A-Za-z]{4,}", text)}
    cjk = re.findall(r"[一-鿿]", text)
    bigrams = {cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)}
    return (latin | bigrams) - _NEGATION_VOCAB


def _negation_corpus_hit(
    claim_text: str, store: "refs.Store", quarantine_titles: list[str] | None = None
) -> str | None:
    """F11① deterministic 语料回查: a negation/缺口 claim asserts no evidence exists.
    Checks BOTH the verified store AND the C2 quarantine pool (spec §0.6.f: 对 store +
    quarantine 检索被否定对象) — a quarantine hit is especially damning (we DID find
    it and dropped it). Returns the matching citation_key / 'quarantine:<title>'."""
    claim_words = _content_bigrams(claim_text)
    if len(claim_words) < 2:
        return None
    best_key, best_overlap = None, 1
    for entry in store.get("entries", {}).values():
        if entry.get("verification_status") != "verified" or entry.get("excluded_reason"):
            continue
        # M11: scan title AND the fetched abstract/source TEXT (a directly-contradicting
        # paper often names the negated object only in its abstract). NB: entry['abstract']
        # is a fetch-STATUS string ('succeeded')/None — the text lives on disk, loaded via
        # _load_source_text; truncate to bound cost across the store.
        haystack = str(entry.get("title") or "") + " " + (_load_source_text(entry) or "")[:1500]
        overlap = len(claim_words & _content_bigrams(haystack))
        if overlap > best_overlap:
            best_overlap, best_key = overlap, entry.get("citation_key")
    for title in quarantine_titles or []:
        overlap = len(claim_words & _content_bigrams(title))
        if overlap > best_overlap:
            best_overlap, best_key = overlap, f"quarantine:{title[:30]}"
    return best_key


def _negation_crosslang_blind(claim_text: str, store: "refs.Store") -> bool:
    """True when the deterministic negation recheck CANNOT verify across languages: the
    claim's negated object is CJK-only (no ≥4-char English term to bridge) AND the corpus
    is predominantly English (≥1 verified entry with a latin-heavy title). In that case a
    within-language miss is not evidence of absence — the caller escalates to needs_review
    rather than passing silently (closing the §0.6.f cross-language false-negative)."""
    if re.search(r"[A-Za-z]{4,}", claim_text):
        return False  # the claim carries an English term → deterministic match is possible
    if len(re.findall(r"[一-鿿]", claim_text)) < 4:
        return False
    for entry in store.get("entries", {}).values():
        if entry.get("verification_status") != "verified" or entry.get("excluded_reason"):
            continue
        title = str(entry.get("title") or "")
        latin = len(re.findall(r"[A-Za-z]", title))
        if latin >= 10 and latin > len(re.findall(r"[一-鿿]", title)):
            return True
    return False


def _gap_of_key(store: "refs.Store", key: str) -> str | None:
    doi = refs.resolve_citation_key(store, key)
    entry = store.get("entries", {}).get(doi) if doi else None
    gap = entry.get("gap") if entry else None
    return gap if isinstance(gap, str) else None


def _gap_verified_count(store: "refs.Store", key: str) -> int:
    """Count verified, non-excluded entries in the gap of the cited key (the
    whole-gap fallback denominator for the F11② set-count check)."""
    gap = _gap_of_key(store, key)
    if gap is None:
        return 0
    return sum(
        1
        for e in store.get("entries", {}).values()
        if e.get("gap") == gap
        and e.get("verification_status") == "verified"
        and not e.get("excluded_reason")
    )


def _annotated_set_size(topic_dir: "pathlib.Path | None", store: "refs.Store", key: str) -> int | None:
    """F11② TRUE annotated set (spec §0.6.f「对 annotated set 计数校验」): the count
    of DISTINCT [@key] the analyst actually judged for this gap, read from
    notes/round-*/gap-<gap>.annotated.md. None when no annotated file exists (then
    the caller falls back to the whole-gap verified count)."""
    if topic_dir is None:
        return None
    gap = _gap_of_key(store, key)
    if gap is None:
        return None
    keys: set[str] = set()
    found = False
    for path in sorted((topic_dir / "notes").glob(f"round-*/{gap}.annotated.md")):
        found = True
        keys.update(_CITATION_RE.findall(path.read_text(encoding="utf-8")))
    return len(keys) if found else None


def _blind_spot_flags(
    claim_text: str,
    grounding_level: str,
    store: "refs.Store | None" = None,
    key: str | None = None,
    quarantine_titles: list[str] | None = None,
    annotated_set_size: int | None = None,
) -> list[str]:
    """F11: the three per-[@key] blind spots (spec §0.6.f). ``annotated_set_size``
    (when given) is the TRUE analyst-annotated set count for the set-count check;
    falls back to the whole-gap verified count when None."""
    flags: list[str] = []
    if _NEGATION_CLAIM_RE.search(claim_text):
        if not _SCOPE_PHRASE_RE.search(claim_text):
            flags.append("绝对否定未限定到检索范围（应写『本文检索范围内未见…』，F11①）")
        if store is not None:
            hit = _negation_corpus_hit(claim_text, store, quarantine_titles)
            if hit:
                where = "quarantine 隔离池" if str(hit).startswith("quarantine:") else "store"
                flags.append(f"否定断言但 {where} 内疑似相关证据 [{hit}]——语料回查命中，须核（F11①）")
            elif _negation_crosslang_blind(claim_text, store):
                # the determinate corpus recheck is cross-language BLIND here: the claim's
                # negated object is CJK-only (no English gloss) while the corpus is English
                # → a within-language miss is NOT evidence of absence. Escalate instead of
                # silently passing (§0.6.c 歧义默认从严 + §0.6.f 必须真去查). Closeable by
                # adding the English term in parens per the project term-gloss rule.
                flags.append(
                    "否定断言的语料回查跨语言不可判（中文断言无英文术语对照、库内为英文文献）——"
                    "须人工核或给被否定对象补英文术语（F11①/§0.6.c）"
                )
    if _SET_CLAIM_RE.search(claim_text):
        ints = [int(n) for n in _extract_numbers(claim_text) if n.isdigit()]
        total = max(ints) if ints else None               # N (the claimed denominator)
        if len(ints) >= 2 and ints[1] > ints[0]:
            flags.append(f"集合计数不一致：支持数 {ints[1]} > 总数 {ints[0]}（F11②）")
        elif total is not None and store is not None and key is not None:
            if annotated_set_size is not None:
                # N (total claimed) must not exceed the analyst-judged set. (R3-A9: the
                # old `elif support > ann` was dead — support=min(ints) ≤ total=max(ints),
                # so support>ann implies total>ann, already caught. A genuine support-set
                # check needs the cite-direction subset of annotated.md — see §限定 residual.)
                if total > annotated_set_size:
                    flags.append(f"集合断言总数 {total} > annotated set {annotated_set_size}（F11②）")
            else:
                # M12: no annotated.md → still catch an IMPOSSIBLE count vs the (looser)
                # whole-gap verified set, and surface that the tighter denominator is absent.
                gap_n = _gap_verified_count(store, key)
                if gap_n and total > gap_n:
                    flags.append(f"集合断言总数 {total} > 该 gap 实有 verified 证据 {gap_n}（F11②）")
                else:
                    flags.append(
                        f"集合断言（声称 {total} 项）但本 gap 无 annotated.md，仅能比对更宽的 gap verified {gap_n}—须人工对计数（F11②）"
                    )
        else:
            flags.append("集合/综合断言需对 annotated set 计数校验（F11②）")
    if _AUTHOR_CONCL_RE.search(claim_text) and grounding_level == "abstract":
        flags.append("作者结论/推荐断言应核全文，当前仅 abstract grounding（F11③）")
    return flags


def _risk_level(claim_text: str, numbers: list[str]) -> str:
    """High-risk (spec §0.6) if the claim carries quantitative / comparison /
    negation / count / author-conclusion surface cues. Deterministic; defaults
    to high on any cue, low otherwise."""
    if numbers:
        return "high"
    for rx in (_COMPARISON_RE, _NEGATION_RE, _COUNT_SET_RE, _AUTHOR_CONCL_RE):
        if rx.search(claim_text):
            return "high"
    # M6: an effect asserted on a hard clinical outcome (死亡率/卒中/…) is high-risk even
    # with no surface number — the spec's flagship 来源沉默 example ("降低死亡率").
    if _CLINICAL_OUTCOME_RE.search(claim_text) and _EFFECT_VERB_RE.search(claim_text):
        return "high"
    # C10c (defense-in-depth): a no-number mechanism / causal conclusion (改变受体结合 /
    # 导致逃逸 / 介导…) is high-risk even without a surface number, so it faces the 3-judge
    # ensemble. The span_source 护栏 already blocks an abstract_fallback such claim; this
    # only upgrades the judge tier when it DOES carry a precise span.
    if _CAUSAL_RE.search(claim_text) or _MECHANISM_VERB_RE.search(claim_text):
        return "high"
    return "low"


def _norm_digits(text: str) -> str:
    return _collapse_thousands(text)


def _span_section(source: str, span: str, grounding_level: str) -> str:
    """Which section the primary evidence span sits in (F5). Abstract grounding →
    'abstract'. Fulltext → nearest preceding heading; 'body' if undetectable
    (conservative: an undetectable section never trips the b2 finding gate)."""
    if grounding_level == "abstract":
        return "abstract"
    if not span or span == "-":
        return "body"
    norm = _norm_digits(source)
    probe = span.strip()[:20]
    idx = norm.find(probe) if probe else -1
    if idx < 0:
        return "body"
    prefix = norm[:idx]
    best_section, best_pos = "body", -1
    for rx, name in _SECTION_HEADINGS:
        for match in rx.finditer(prefix):
            # R3-A8 + R4: a real section heading is a SHORT heading-SHAPED line, not a
            # section word inside running prose. The old `start()!=0` half accepted a
            # match at source position 0 (just BOF prose, e.g. 'Prior work has…') as a
            # heading → over-fired b2. Require: the chars before the match on its line are
            # only heading markup (#/>/*/-/space), and the chars after to line-end are
            # only heading punctuation (so 'Results' / '## Results' / 'Results:' count,
            # but 'the results show …' does not).
            ls = prefix.rfind("\n", 0, match.start()) + 1
            le = prefix.find("\n", match.end())
            le = len(prefix) if le < 0 else le
            lead, tail = prefix[ls:match.start()], prefix[match.end():le]
            if not re.fullmatch(r"[#>\s*\-]*", lead) or not re.fullmatch(r"[\s:：.．、，,_)\]}]*", tail):
                continue
            if match.start() > best_pos:
                best_pos, best_section = match.start(), name
    return best_section


def _span_negation_conflict(span: str, claim_text: str) -> bool:
    """b3: the source span carries a negation / non-significance cue but the claim
    is asserted positively (no negation of its own) → force human review."""
    if not span or span == "-":
        return False
    if not _NEGATION_RE.search(span):
        return False
    return not _NEGATION_RE.search(claim_text)


def _scope_creep(claim_text: str, source: str) -> bool:
    """F4 (deterministic slice): claim asserts causation but the source only
    speaks of correlation/association (no causal language) → likely scope creep."""
    if not _CAUSAL_RE.search(claim_text):
        return False
    return bool(_CORRELATION_RE.search(source)) and not _CAUSAL_RE.search(source)


# ---------------------------------------------------------------------------
# C10 (v3.2 §0.6.e) — span binding for no-number claims + empirical-predicate anchor
# ---------------------------------------------------------------------------

_FALLBACK_SPAN_LEN = 400
_LEADING_HEADING_RE = re.compile(r"^\s*(?:#+\s*[^\n]*|##?\s*Abstract[^\n]*)\n", re.IGNORECASE)


_SENT_END_CHARS = "。！？.!?;；"


def _widen_span_to_sentence(source: str, span: str) -> str:
    """C10d: widen a high-risk number-anchored span (a ±30-char window) to its containing
    source sentence, so a scope-limiting clause ('降 44%（80 岁以上无效）') the judge would
    otherwise not see is included with the number. Best-effort: returns span unchanged if it
    can't be located in the normalised source."""
    if not span or span == "-":
        return span
    norm = _collapse_thousands(
        _GLUED_DIGIT_NAME_RE.sub(" ", _deglue_source(source))
    ).replace("\n", " ")
    probe = span.strip()[:24]
    idx = norm.find(probe) if probe else -1
    if idx < 0:
        return span
    left = max((norm.rfind(p, 0, idx) for p in _SENT_END_CHARS), default=-1)
    start = left + 1 if left >= 0 else max(0, idx - 120)
    ends = [e for e in (norm.find(p, idx + len(probe)) for p in _SENT_END_CHARS) if e >= 0]
    end = (min(ends) + 1) if ends else min(len(norm), idx + len(probe) + 120)
    widened = norm[start:end].strip()
    return widened or span


def _fallback_span(source: str) -> str:
    """C10 abstract_fallback: a real, source-grounded slice for a no-number claim that
    has no number-anchored or qualitative_extract span — so the entailment judge is NEVER
    handed an empty span (F8 root cause: empty span → judge 'not_entailed' → false suspect).
    Take the first substantive ~400 chars of the source, skipping a leading title/##Abstract
    heading line (the old ``source[:60]`` grabbed the title line on full-text sources). The
    span is intentionally coarse — a fact claim resting on it is suspect (§0.6.e 护栏), not
    faithful; it exists only to give the judge real text to rule against."""
    body = _LEADING_HEADING_RE.sub("", source, count=1).strip() or source.strip()
    return body[:_FALLBACK_SPAN_LEN].replace("\n", " ").strip()


# 主题实证谓词词表 (C-INF rev6/8): mechanism / finding / causal / comparison predicates
# whose presence means a clause TRANSCRIBES an empirical finding (must be grounded), as
# opposed to a process fact (检索/纳入/审稿 — 溯 log, §0.6.a). Deliberately excludes bare
# 与/and (a process count '检索 X 与 Y' must stay exempt) — that exclusion lives in
# _CORRELATION_RE already; this verb list carries no bare coordinator.
# NB (R2 fix): the bare generic magnitude verbs (升高/降低/增加/减少/提高/下降/增强/减弱) were
# REMOVED — they over-fire on process bookkeeping ('纳入数量增加至 23 篇' → false empirical). The
# THEMATIC nouns below still catch the real mechanism cases (免疫逃逸增强 → 逃逸; 削弱单抗结合 →
# 单抗结合; 中和能力下降 → 中和); a clinical effect (死亡率降低) is caught by _CLINICAL_OUTCOME_RE
# + _EFFECT_VERB_RE; a numeric finding is caught by _data_numbers. Regulatory verbs (抑制/激活/
# 上调/下调/介导/诱导) are kept — they are pathway-thematic, not count words.
_MECHANISM_VERB_RE = re.compile(
    r"受体结合|抗体结合|单抗结合|结合(?:能力|位点|模式|亲和)|中和(?:抗体|作用|能力)?|"
    r"逃逸|免疫逃逸|逃避|规避|构象|复制(?:能力|效率)?|转录|翻译|表达(?:量|水平)?|"
    r"传播(?:力|性|速度)?|传染性|感染性|致病性|毒力|亲和(?:力|性)|突变|变异|半衰期|清除(?:率)?|"
    r"抑制|激活|上调|下调|介导|诱导|"
    r"upregulat|downregulat|neutrali[sz]|evasion|escape|binding affinity|conformational|"
    r"replication|transmissibility|infectivity|virulence",
    re.IGNORECASE,
)

# A bare MAGNITUDE verb (升高/降低/增加…) is empirical ONLY when paired with a biological / clinical
# MEASURE noun ('病毒载量升高' / '抗体滴度下降' / 'T 细胞数量减少') — review round 2 restored these
# (deleting them outright regressed real mechanism detection) but GATED them so process bookkeeping
# ('研究数量增加至 23 篇' / '样本量增加到 1000 人' — 数量/样本量 are NOT measure nouns) stays exempt.
_MAGNITUDE_VERB_RE = re.compile(
    r"升高|降低|提高|下降|增加|减少|增多|增长|增强|减弱|削弱|上升|下跌|攀升|回落", re.IGNORECASE
)
_BIO_MEASURE_NOUN_RE = re.compile(
    r"载量|滴度|效价|浓度|水平|活性|表达|细胞|因子|抗体|抗原|血压|血糖|血脂|胆固醇|甘油三酯|"
    r"体重|心率|体温|发生率|患病率|感染率|死亡率|生存率|阳性率|检出率|风险|剂量|半衰期|"
    r"亲和力|病毒|免疫|炎症|蛋白|基因|受体|信号|代谢",
    re.IGNORECASE,
)

# A number bound to a study / methods COUNTER (纳入 23 篇 / 检索 1500 条 / 3 名审稿人) is process
# bookkeeping, NOT a transcribed data value. _data_numbers strips these so a process count stays
# grounding-exempt while a finding's data value (effect / rate / measurement) does not. Review round 2
# (the EIGHTH face): the generic accumulators (共/计/合计/累计) + bare event counters (人/例/名)
# over-stripped an OUTCOME count ('共 200 人死亡' / '12 例死亡') — a transcribed finding. A count is now
# process ONLY if (a) a SEARCH/SCREENING verb directly precedes it, OR (b) an UNAMBIGUOUS corpus unit
# (篇/项/条/批/轮) follows it, OR (c) an ambiguous counter is FOLLOWED BY a corpus/process noun
# (审稿人/研究/文献…). A bare event count never strips → an outcome count is checked like Phase 1.
_PROCESS_COUNT_RE = re.compile(
    r"(?:纳入|入组|招募|募集|随机分配|分配|脱落|失访|退出|检索|收录|获得|剔除|排除|筛选?出?|去重(?:后)?|保留|命中|获取|查得)"
    r"\s*\d[\d,，]*\s*(?:篇|项|条|批|轮|个|名|位|例|份|种)?"
    r"|\d[\d,，]*\s*(?:篇|项|条|批|轮)"
    r"|\d[\d,，]*\s*(?:个|名|位|份|例|册|本|种|人|篇|项|条)\s*"
    r"(?:研究|试验|文献|论文|RCT|队列|记录|审稿人?|综述|数据库|来源|样本)",
    re.IGNORECASE,
)

# An OUTCOME event word (死亡/复发/感染…) adjacent to a count means it is a transcribed RESULT
# ('200 例死亡' / '12 例研究对象死亡'), not bookkeeping — so it is NEVER stripped, even if it matched
# a process-count pattern (round-2 #1/#4: closes the last Phase-1/Phase-2 over-strip asymmetry).
_OUTCOME_EVENT_RE = re.compile(
    r"死亡|病死|致死|存活|生存|复发|转移|缓解|进展|恶化|痊愈|治愈|感染|发病|发作|事件|"
    r"卒中|中风|心梗|心肌梗死|住院|再入院|再住院|急诊|骨折|出血|血栓|栓塞|截肢|失明|致残|"
    r"阳转|阴转|转阴|转阳|确诊|患病|死于",
    re.IGNORECASE,
)


def _data_numbers(text: str) -> list[str]:
    """A clause's DATA numbers (effect / rate / ratio / measurement / OUTCOME count) — i.e. its
    extracted numbers minus study/methods COUNTS (纳入 23 篇 / 检索 1500 条 / 3 名审稿人). A data number
    in an exempt-label clause is a transcribed finding that must be grounded; a pure process count
    溯 to log/store and stays exempt (store_stat's count-vs-store check guards fabricated totals). A
    count ADJACENT to an outcome-event word is a transcribed RESULT, never stripped (round-2 #1/#4)."""
    if not _extract_numbers(text):
        return []

    def _strip(m: "re.Match[str]") -> str:
        # keep a process-count match that is really an OUTCOME count: an outcome word must sit in
        # the segment from this match's end to the NEXT clause boundary / next number (so '招募 800
        # 名后 30 人死亡' strips 800 but keeps 30 — the 死亡 belongs to the 30, not the 800).
        seg = re.split(r"[，,。.、；;:：\s]|\d", text[m.end():m.end() + 12], maxsplit=1)[0]
        return m.group(0) if _OUTCOME_EVENT_RE.search(seg) else " "

    return _extract_numbers(_PROCESS_COUNT_RE.sub(_strip, text))


def _carries_empirical_predicate(text: str) -> bool:
    """True if a clause asserts a THEMATIC empirical finding (mechanism / causal /
    comparison / clinical effect / correlation) — i.e. it transcribes some paper's
    empirical content and therefore must be grounded, NOT 溯 to a process log
    (§0.6.a). The trigger anchor for the C-INF2/3 strong per-clause entailment: it
    is label-, risk- and key-position-independent (rev6 收敛点). Bare process facts
    ('纳入 23 篇' / '检索 PubMed' / '审稿') carry none of these and stay exempt. A clause
    that carries a DATA number but no thematic predicate is caught separately via
    _data_numbers (the R1 number-gate fix)."""
    if _CLINICAL_OUTCOME_RE.search(text) and _EFFECT_VERB_RE.search(text):
        return True
    for rx in (_CAUSAL_RE, _COMPARISON_RE, _MECHANISM_VERB_RE):
        if rx.search(text):
            return True
    # a bare magnitude verb counts only with a biological/clinical MEASURE noun ('病毒载量升高'),
    # so process bookkeeping ('研究数量增加') stays exempt (review round 2 regression fix).
    if _MAGNITUDE_VERB_RE.search(text) and _BIO_MEASURE_NOUN_RE.search(text):
        return True
    # correlational finding ('X 与 Y 相关') — _CORRELATION_RE excludes bare 与/and so
    # a process count '检索 X 与 Y' does not match.
    return bool(_CORRELATION_RE.search(text))


_PARA_SPLIT_RE = re.compile(r"\n{2,}")


def _paragraph_of_claim_id(review_text: str) -> dict[str, int]:
    """Map each claim_id → the index of the blank-line-delimited paragraph it sits in
    (C-INF2: per-claim entailment is paragraph-LOCAL — an empirical-predicate exempt-label
    clause must be entailed by a grounded span in ITS OWN paragraph, not anywhere in the
    doc). Splitting on ``\\n{2,}`` matches §0.6.a's '同段' and the writer's prose blocks."""
    out: dict[str, int] = {}
    for idx, para in enumerate(_PARA_SPLIT_RE.split(review_text)):
        for cid in claimids.CLAIM_ID_RE.findall(para):
            out.setdefault(cid, idx)
    return out


def _load_evidence_table(topic_dir: "pathlib.Path | None") -> dict[str, dict[str, object]]:
    """Load meta/evidence_table.json (evidence_extract --validate output) if present.
    Used for C10b qualitative_extract spans; absent (unit tests / pre-extraction) → {}."""
    if topic_dir is None:
        return {}
    path = topic_dir / "meta" / "evidence_table.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _qualitative_span(
    table: dict[str, dict[str, object]], key: str, source: str
) -> str | None:
    """C10b: the evidence_extract qualitative span for this key's cited conclusion (a
    verbatim source sentence carrying the entity+direction+comparator of a no-number
    finding). Validated by same-language source existence (§0.6.e). None → fall to
    abstract_fallback. This is the ONLY clean span for a no-number fact claim — without it
    the 护栏 makes the claim suspect (forcing precise extraction or soften/delete)."""
    row = table.get(key)
    if not isinstance(row, dict):
        return None
    span = str(row.get("qualitative_span") or "").strip()
    if not span:
        return None
    return span if span[:20] in _norm_digits(source) else None


# ---------------------------------------------------------------------------
# Core data type and evaluate()
# ---------------------------------------------------------------------------

@dataclass
class ClaimVerdict:
    key: str
    sentence: str
    grounding: str
    verdict: str          # faithful | suspect | needs_review | insufficient
    reason: str
    numbers: list[str] = field(default_factory=list)
    evidence_spans: dict[str, str] = field(default_factory=dict)  # num -> span
    # F2/F5/F12 — full claim_evidence_map schema fields
    claim_id: str = ""
    key_type: str = "verified_entry"  # verified_entry | inference | …(F1 sidecar)
    risk: str = "low"                 # high | low (F12 risk grading)
    span: str = "-"                   # primary machine-checked evidence span (F5)
    span_section: str = ""            # section the span sits in (F5/F6)
    atomic_claim: str = ""            # F1 will populate per-clause; = sentence for now
    # C10 (v3.2): how the bound span was obtained. number_anchor = a claim number's
    # source window; qualitative_extract = evidence_extract's per-(key,claim) qualitative
    # span; abstract_fallback = a coarse whole-abstract slice (no precise span — a fact
    # claim resting on it is suspect, never faithful: §0.6.e); paragraph_entail = the
    # paragraph-local grounded spans an empirical-predicate exempt-label clause must be
    # entailed by (C-INF2/3); none = no span bound (insufficient / not applicable).
    span_source: str = "none"


def _claim_id(key: str, sentence: str) -> str:
    """Synthesised, deterministic claim id (F1 will override from the writer's
    `<!-- claim:CID -->` sidecar when present; until then derive from content)."""
    import hashlib

    digest = hashlib.sha1(f"{key}|{sentence}".encode("utf-8")).hexdigest()
    return f"c{digest[:8]}"


def evaluate(
    store: refs.Store,
    review_text: str,
    *,
    loader: "object | None" = None,  # unused; reserved for test injection
    topic_dir: "pathlib.Path | None" = None,  # enables F11① quarantine recheck
) -> list[ClaimVerdict]:
    """Return one ClaimVerdict per [@key] citation occurrence in review_text, plus
    an inference row per ``type:inference`` clause.

    No network. ``topic_dir`` (when given) lets the F11① negation recheck also scan
    meta/quarantine.jsonl (spec §0.6.f). The ``loader`` param is reserved.
    """
    quarantine_titles: list[str] = []
    if topic_dir is not None:
        from lib import quarantine as _quar
        quarantine_titles = [
            str(r.get("title") or "") for r in _quar.load(topic_dir) if r.get("title")
        ]
    evidence_table = _load_evidence_table(topic_dir)  # C10b qualitative_extract spans
    claims = _claim_sentences(review_text)
    occ_claim_ids = _claim_ids_by_occurrence(review_text)
    aligned = len(occ_claim_ids) == len(claims)  # counts match → index alignment safe
    results: list[ClaimVerdict] = []

    for claim_index, (key, sentence) in enumerate(claims):
        # Grade risk + bind claim_id EARLY so even an ``insufficient`` row (title_only /
        # unreadable source) is risk-graded: a quantitative/comparison claim is high-risk
        # regardless of grounding, so high-risk-grounding & evidence-uncertain ALSO flag
        # it (defense-in-depth — not only the insufficient→faithfulness FAIL).
        early_text = claimids.strip(_CITATION_RE.sub("", sentence))
        early_risk = _risk_level(early_text, _extract_numbers(early_text))
        _early_cid = occ_claim_ids[claim_index] if aligned else None
        if _early_cid is None:
            _m = claimids.CLAIM_ID_RE.search(sentence)
            _early_cid = _m.group(1) if _m else None
        early_cid = _early_cid or _claim_id(key, sentence)

        doi = refs.resolve_citation_key(store, key)
        if doi is None:
            results.append(ClaimVerdict(
                key=key, sentence=sentence,
                grounding="title_only",
                verdict="insufficient",
                reason=f"citation_key '{key}' not found in store",
                risk=early_risk, claim_id=early_cid,
            ))
            continue

        entry = store["entries"][doi]
        grounding_level = refs.grounding(entry)

        if grounding_level == "title_only":
            results.append(ClaimVerdict(
                key=key, sentence=sentence,
                grounding="title_only",
                verdict="insufficient",
                reason="title_only: no source text to verify against",
                risk=early_risk, claim_id=early_cid,
            ))
            continue

        source = _load_source_text(entry)
        if not source:
            results.append(ClaimVerdict(
                key=key, sentence=sentence,
                grounding=grounding_level,
                verdict="insufficient",
                reason="source file not readable or empty",
                risk=early_risk, claim_id=early_cid,
            ))
            continue

        # Read the writer's claim_id sidecar (F1 by-construction) for this [@key]
        # occurrence; fall back to a content hash for legacy reviews with no sidecar.
        sidecar_cid = occ_claim_ids[claim_index] if aligned else None
        if sidecar_cid is None:
            match = claimids.CLAIM_ID_RE.search(sentence)
            sidecar_cid = match.group(1) if match else None
        bound_claim_id = sidecar_cid or _claim_id(key, sentence)
        bound_key_type = "verified_entry"

        # Strip citation markers AND the claim_id sidecar from the sentence before
        # number extraction so that years in citation keys (jones2021) and the
        # claim id itself (claim:c1 → spurious "1") aren't extracted as claim numbers.
        claim_text = claimids.strip(_CITATION_RE.sub("", sentence))

        # Check numbers (C11)
        numbers = _extract_numbers(claim_text)
        # R14: the cited work's OWN publication year, mentioned in prose ('Suez 等 2022',
        # 'WHO 2023', '(Schiffman 2023)'), is not a hallucinated data value — a source abstract
        # rarely repeats its own pub year. Suppress it precisely via entry metadata (ground
        # truth), which replaces the fail-open-prone author-year heuristic. A real data value
        # is only ever suppressed in the negligible case it equals the cited work's exact year.
        entry_year = str(entry.get("year") or "").strip()
        evidence_spans: dict[str, str] = {}
        missing_numbers: list[str] = []
        for num in numbers:
            found, span = _number_in_source(num, source)
            evidence_spans[num] = span
            # R15: suppress only a BARE mention of the cited work's own pub year (a unit-suffixed
            # value equal to the pub year is a real measurement → still checked, no fail-open).
            if not found and not (
                entry_year and num == entry_year and _claim_has_bare_year(num, claim_text)
            ):
                missing_numbers.append(num)

        # Check study-type conflict (against claim text without citation markers)
        type_conflict = _study_type_conflict(claim_text, entry, source)

        # R29: the cited work's own BARE pub year is a citation mention, not a finding number — so
        # it must NOT bind the evidence span or feed the b2 (non-finding-section) gate. R28's
        # full-text source exposed this: a claim '…2017…' bound its span to '$26.3 billion for 2017'
        # in the Introduction → _span_section='introduction' → b2 fired → faithful→needs_review.
        # Mirror the missing-number suppression above onto the span-binding number set.
        bound_numbers = [
            n for n in numbers
            if not (entry_year and n == entry_year and _claim_has_bare_year(n, claim_text))
        ]

        # Risk grade (F12) + primary span + its section (F5). Prefer the co-location
        # window (M5: all numbers together) as the bound span when it exists.
        risk = _risk_level(claim_text, numbers)
        coloc = _colocation_span(bound_numbers, source)
        primary_span = coloc or next(
            (evidence_spans[n] for n in bound_numbers if evidence_spans.get(n) not in (None, "-")),
            "",
        )
        # C10 (§0.6.e): bind a NON-EMPTY same-language span for every claim so the
        # entailment judge is never handed '-' (the F8 root cause). A number-anchored
        # span is precise; a no-number claim prefers evidence_extract's qualitative span
        # (C10b), else a coarse abstract_fallback (grounding-agnostic, not abstract-only).
        span_source = "number_anchor" if primary_span else "none"
        if not primary_span:
            qual = _qualitative_span(evidence_table, key, source)
            if qual:
                primary_span, span_source = qual, "qualitative_extract"
            else:
                primary_span, span_source = _fallback_span(source), "abstract_fallback"
        span_section = _span_section(source, primary_span, grounding_level)
        # C10d: give the judge a high-risk number-anchored claim's WHOLE sentence (not just
        # the ±30-char number window), so a scope-limiting clause can't搭便车 past it.
        if span_source == "number_anchor" and risk == "high":
            primary_span = _widen_span_to_sentence(source, primary_span)

        # needs_review signals (deterministic): polarity, b3 negation, b2 type×section,
        # M5 number co-location, F4 scope-creep.
        nr: list[str] = []
        if _has_polarity(claim_text):
            nr.append("polarity word; direction not deterministically confirmable")
        if _span_negation_conflict(primary_span, claim_text):
            nr.append("negation/non-significance cue in source span but claim positive (b3)")
        # M5 (refocused, R2-F4/F5): only CI/range bound-pairs must be coherent — generic
        # ≥2-number co-location false-flagged multi-outcome decision sentences. _range_pair_issue
        # checks the claim's CI/range bounds co-locate in source AND keep order (catches
        # 0.27↔0.72 / CI flip without touching legitimate multi-stat synthesis sentences).
        rng = _range_pair_issue(claim_text, source)
        if rng:
            nr.append(rng)
        # R3-A4: restore a GUARDED scattered-swap flag the R2 refocus dropped. When ≥2
        # ratio-like numbers (0.x — RR/HR/OR/CI-ish) all exist in source but never co-locate
        # in one window, and they are NOT a multi-stat list (no 顿号/，/、/and between them),
        # the claim likely stitched them from different rows (the spec's 0.27↔0.72 example).
        # Gating to ratio-like + non-list avoids the multi-outcome-sentence false positive.
        elif not rng and not missing_numbers and _colocation_span(bound_numbers, source) is None:
            ratio_nums = [n for n in bound_numbers if _RATIO_NUM_RE.match(n)]
            # R4-A4: include Chinese coordinate markers (和/与/及/以及/分别) — without them
            # a legit two-outcome sentence ('风险比分别为 0.65 和 0.80') was false-killed.
            # Clausal connectors (并/而/同时) are NOT coordinate, so a genuine stitch
            # ('把风险比降到 0.27 并维持 0.72') still flags.
            is_list = bool(re.search(r"[、，,]|\band\b|[和与及]|以及|分别", claim_text))
            if len(ratio_nums) >= 2 and not is_list:
                nr.append("声明的多个比率型数字在源中从不共现于同一片段—疑似跨行拼接/抄错（b/M5）")
        # b2 (R2-F7 fix): flag a high-risk quantitative finding drawn from an EXPLICIT
        # non-finding section (intro/background/related-work/hypothesis/methods). An
        # undetectable 'body' is NOT flagged (extraction often drops fulltext headings —
        # flagging it was a false-positive regression; matches _span_section's docstring).
        if risk == "high" and bound_numbers and span_section in _NON_FINDING_SECTIONS:
            nr.append(f"finding cited from '{span_section}' section, not Results/table/abstract (b2)")
        if _scope_creep(claim_text, source):
            nr.append("causal claim but source shows only correlation — scope creep (F4)")
        nr.extend(_blind_spot_flags(
            claim_text, grounding_level, store, key, quarantine_titles,
            annotated_set_size=_annotated_set_size(topic_dir, store, key),
        ))

        common = dict(
            key=key, sentence=sentence, grounding=grounding_level,
            numbers=numbers, evidence_spans=evidence_spans,
            claim_id=bound_claim_id, key_type=bound_key_type, risk=risk,
            span=primary_span or "-", span_section=span_section,
            span_source=span_source, atomic_claim=claim_text.strip(),
        )

        # Determine verdict (precedence: suspect > needs_review > faithful)
        if missing_numbers or type_conflict:
            verdict, reason = "suspect", (
                f"number(s) not found in source: {missing_numbers}"
                if missing_numbers
                else "study_type conflict: prose label stronger than entry/source"
            )
        elif nr:
            verdict, reason = "needs_review", "; ".join(nr)
        else:
            verdict, reason = "faithful", (
                "grounded; all numbers found; no conflict/negation/scope-creep"
            )
        # 护栏 (C10 rev4/5, §0.6.e): a claim that TRANSCRIBES an empirical finding
        # (carries a thematic empirical predicate) but would be faithful only because a
        # coarse abstract_fallback span was synthesised for it has NO precise grounding →
        # suspect (HARD — not marker-clearable; write_gate requires suspect==0). The only
        # clean pass is a number_anchor or a C10b qualitative_extract span; absent that the
        # writer must soften / delete / §0.6.j-disclose. Anchored on the empirical predicate
        # (rev6 收敛点), not risk — closes the 'low-risk + 宽 span + 通用 judge entailed'
        # leak (rev4 新洞-B). A non-empirical framing clause ('开创了方向') is not
        # transcribing a finding → keeps its abstract_fallback span + judge, not forced suspect.
        if (verdict == "faithful" and span_source == "abstract_fallback"
                and _carries_empirical_predicate(claim_text)):
            verdict, reason = "suspect", (
                "转述实证发现但仅靠 abstract 兜底 span（无精确 number/qualitative span）—须精确 span 或软化/删/披露（§0.6.e/C10b）"
            )
        results.append(ClaimVerdict(verdict=verdict, reason=reason, **common))

    # §0.6.a: every declarative clause gets a claim_map row, not just cited ones.
    #  · inference / method / search / store sidecars (no [@key]) → grounding-exempt
    #    rows (they rest on grounded atoms or 过程日志). sentence/atomic_claim are
    #    filled from the annotated clause so the map row is never blank.
    #  · a `factual` sidecar with NO resolvable [@key] = an ungrounded factual
    #    claim → insufficient (can't dodge grounding by dropping the citation).
    sentence_map = _sentence_by_claim_id(review_text)
    para_of_cid = _paragraph_of_claim_id(review_text)  # C-INF2 paragraph-local entailment
    seen_ids = {cv.claim_id for cv in results}
    # C-INF2 (rev7): collect each paragraph's PRECISELY-grounded spans (number_anchor /
    # qualitative_extract, faithful or needs_review — R2-F2 双计). The strong per-clause
    # entailment requires an empirical-predicate exempt-label clause to be entailed by one
    # of ITS paragraph's grounded spans, NOT merely to co-exist with a grounded atom (rev7
    # 新洞-E paragraph-composition). A coarse abstract_fallback span is NOT precise grounding,
    # so it is excluded here (else c2「发表日期」could vacuously "support" c1「免疫逃逸增强」).
    grounded_spans_by_para: dict[int, list[str]] = {}
    for cv in results:
        if (cv.key_type == "verified_entry" and cv.verdict in ("faithful", "needs_review")
                and cv.span_source in ("number_anchor", "qualitative_extract")
                and cv.span and cv.span != "-"):
            p = para_of_cid.get(cv.claim_id)
            if p is not None:
                grounded_spans_by_para.setdefault(p, []).append(cv.span)
    # doc-global grounded atom (legacy lenient guard) — used ONLY for a NON-empirical
    # synthesis clause that transcribes no finding (§0.6.a "rests on grounded atoms").
    has_grounded_atom = any(
        cv.key_type == "verified_entry" and cv.verdict in ("faithful", "needs_review")
        for cv in results
    )
    n_verified_total = sum(
        1 for e in store.get("entries", {}).values()
        if e.get("verification_status") == "verified" and not e.get("excluded_reason")
    )
    for cid, ctype in claimids.claim_entries(review_text):
        if cid in seen_ids:
            continue
        sent = sentence_map.get(cid, "")
        nums = _extract_numbers(sent)
        data_nums = _data_numbers(sent)
        # R1 fix (the "seventh face"): an exempt-label clause is empirical if it carries a
        # thematic predicate OR a DATA number (effect/rate/measurement, not a process count) —
        # the number gate must NOT be label-skippable. Phase 1 (cited claims) already
        # number-checks; Phase 2 used to gate ONLY on the verb lexicon, so a fabricated
        # '生存率 78%' tagged type:inference slipped through faithful, its numbers never checked.
        if ctype in _LOG_KEY_TYPES and (_carries_empirical_predicate(sent) or data_nums):
            # C-INF3 (rev8) + R1: a clause carrying a THEMATIC empirical predicate OR a data
            # number transcribes a finding — grounding-exempt status is forfeit for ANY exempt
            # label (the whole _LOG_KEY_TYPES white-list). Strong per-clause entailment: it must
            # be positively entailed by a same-paragraph grounded span. No precise grounded span
            # → suspect (HARD); a DATA number absent from those spans → suspect (DETERMINISTIC,
            # mirrors Phase 1's number gate — not judge-dependent); else faithful pending an
            # 'entailed' judge verdict. Closes the seven-face arrangement family.
            pspans = grounded_spans_by_para.get(para_of_cid.get(cid)) or []
            joined = " ⋯ ".join(pspans)
            missing = [n for n in data_nums if not _number_in_source(n, joined)[0]]
            if not pspans:
                cv_inf = ClaimVerdict(
                    key=f"({ctype})", sentence=sent, grounding="n/a", verdict="suspect",
                    reason=f"{ctype} 子句承载实证内容、同段无 grounded span 可逐句蕴含—mislabel/无支撑（§0.6.a 强式）",
                    claim_id=cid, key_type=ctype, risk="high", span="-",
                    span_section="paragraph", span_source="none", atomic_claim=sent,
                )
            elif missing:
                cv_inf = ClaimVerdict(
                    key=f"({ctype})", sentence=sent, grounding="n/a", verdict="suspect",
                    reason=f"{ctype} 子句数字 {missing} 不在同段 grounded span—疑似编造/未支撑（§0.6.a 数字门，确定性）",
                    claim_id=cid, key_type=ctype, risk="high",
                    span=joined[:_FALLBACK_SPAN_LEN], span_section="paragraph",
                    span_source="paragraph_entail", atomic_claim=sent,
                )
            else:
                cv_inf = ClaimVerdict(
                    key=f"({ctype})", sentence=sent, grounding="n/a", verdict="faithful",
                    reason=f"{ctype} 子句承载实证内容—须被同段 grounded span 正面蕴含（judge，§0.6.a 强式）",
                    claim_id=cid, key_type=ctype, risk="high",
                    span=joined[:_FALLBACK_SPAN_LEN], span_section="paragraph",
                    span_source="paragraph_entail", atomic_claim=sent,
                )
            results.append(cv_inf)
        elif ctype in _LOG_KEY_TYPES:
            # Non-empirical exempt-label clause: legit synthesis (inference) or process fact
            # (检索/纳入/审稿 — 溯 log, §0.6.a). Grounding-exempt by default.
            verdict, risk, reason = "faithful", "low", (
                f"{ctype} clause — rests on grounded atoms / 过程日志, grounding-exempt (§0.6.a)"
            )
            if ctype == "inference":
                # R3-A2: an ABSOLUTE NEGATION or an UNAMBIGUOUS transcription of an external
                # conclusion (作者认为/该研究结论/conclude/…) marks a factual claim mislabelled
                # inference. (Empirical-predicate cases already handled above.)
                if _NEGATION_CLAIM_RE.search(sent) or _TRANSCRIBED_CONCL_RE.search(sent):
                    verdict, risk, reason = "needs_review", "high", (
                        "type:inference 子句含绝对否定/转述外部结论—疑似伪 inference，须核（§0.6.a/c）"
                    )
                elif not has_grounded_atom:
                    verdict, reason = "needs_review", (
                        "inference 子句但全文无任一 grounded 事实原子可依（§0.6.a 要求其所依据原子均已 grounded）"
                    )
            else:  # store_stat / search_log / research_log / reviewer_log — process facts
                claimed = [int(n) for n in nums if n.isdigit()]
                if ctype == "store_stat" and claimed and max(claimed) > n_verified_total:
                    verdict, risk, reason = "suspect", "high", (
                        f"store_stat 声称 {max(claimed)} 条 > 全库 verified {n_verified_total} 条—不可能（§0.6.a 溯 store）"
                    )
            results.append(ClaimVerdict(
                key=f"({ctype})", sentence=sent, grounding="n/a",
                verdict=verdict, reason=reason,
                claim_id=cid, key_type=ctype, risk=risk, atomic_claim=sent,
            ))
        else:  # 'factual' (or unknown) type with no bound [@key] → ungrounded
            results.append(ClaimVerdict(
                key="(unbound)", sentence=sent, grounding="title_only",
                verdict="insufficient",
                reason="factual claim_id with no resolvable [@key] — ungrounded (§0.6.a)",
                claim_id=cid, key_type="factual", risk="high",
                atomic_claim=sent,
            ))
        seen_ids.add(cid)

    return results


# ---------------------------------------------------------------------------
# Report writers
# ---------------------------------------------------------------------------

def judge_prompt(results: list[ClaimVerdict], *, risk: str = "high") -> str:
    """F8 (spec §0.6 (c)/(g) + 判级去相关): emit the claims a judge must rule on.
    ``risk='high'`` → high-risk claims for the DECORRELATED ensemble (Haiku 抽 span ·
    Sonnet 判极性/显著性 · Opus 判蕴含; different tiers / frames = real decorrelation).
    ``risk='low'`` → low-risk **factual** (cited, faithful) claims for the SINGLE
    judge (spec §0.6.g「低风险描述句单 judge」). apply_judgments() ingests the
    verdicts; inference / method-log rows are grounding-exempt and never emitted."""
    if risk == "low":
        sel = [cv for cv in results
               if cv.risk != "high" and cv.key_type == "verified_entry" and cv.verdict == "faithful"]
        if not sel:
            return "（无低风险事实断言需单 judge）"
        head = [
            "你是低风险事实断言的单 judge（spec §0.6.g）。对每条断言**仅依据其绑定 span**",
            "判 span 是否**正面蕴含**该断言（来源沉默 / 范围蔓延 = `not_entailed`；拿不准 = `unclear`）。",
            "返回 JSON `{claim_id: 'entailed'|'not_entailed'|'unclear'}`。",
            "",
        ]
    else:
        sel = [cv for cv in results if cv.risk == "high"]
        if not sel:
            return "（无高风险断言，无需 LLM 蕴含判定）"
        head = [
            "你们是去相关蕴含 judge 组（Haiku 抽 span / Sonnet 判极性·显著性 / Opus 判蕴含）。",
            "对每条断言**仅依据其绑定 span** 判断 span 是否**正面蕴含**该断言——",
            "来源沉默 / 范围蔓延（相关→因果、替代→结局、丢限定词、RR↔ARR）= `not_entailed`。",
            "英文 span ⊨ 中文断言可跨语言判（§0.6.e c）；**span 不正面蕴含断言每个实体/方向/对照即 `not_entailed`**。",
            "**span_source 标签从严（C10 护栏 #3）**：`paragraph_entail`=同段 grounded span，须逐句蕴含该实证子句，"
            "非'同段存在证据'即可；`abstract_fallback`=整段粗 abstract，除非某一句逐点支撑实体+方向+对照否则 `not_entailed`。",
            "返回 JSON `{claim_id: 'entailed'|'not_entailed'|'unclear'}`；三档分歧升级人工。",
            "",
        ]
    lines = head
    for cv in sel:
        lines.append(f"- claim_id={cv.claim_id} | 断言: {cv.atomic_claim or cv.sentence}")
        lines.append(f"  span[{cv.span_section or '-'}|{cv.span_source}]: {cv.span}")
    return "\n".join(lines)


_JUDGE_FRAMES = ("span", "polarity", "entail")


def claim_hash(cv: "ClaimVerdict") -> str:
    """A short stable hash of a claim's text — lets write_gate detect a STALE
    verdict (writer reused a claim_id but rewrote the clause: spec §0.6.g 改写复用
    旧 ID). Hash the atomic_claim (fallback: sentence)."""
    import hashlib

    text = (cv.atomic_claim or cv.sentence or "").strip()
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def _read_judge_file(meta: pathlib.Path, name: str) -> dict[str, str]:
    path = meta / name
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def combine_judges(topic_dir: pathlib.Path) -> tuple[dict[str, str], int]:
    """Deterministically merge the risk-tiered judge ensemble (spec §0.6.g) into
    meta/judge_verdicts.json, plus a freshness sidecar meta/judge_targets.json
    ({claim_id: claim_hash}) so write_gate can reject a verdict carried over a
    rewritten clause. No LLM here — the merge itself stays un-gameable.

      · HIGH-risk: 3 decorrelated frames judge_{span,polarity,entail}.json —
        any ``not_entailed`` → not_entailed; all three ``entailed`` → entailed;
        else (any unclear / a frame missing) → unclear.
      · LOW-risk factual: single judge judge_lowrisk.json passed straight through.

    Only claims still PRESENT and needing a verdict (current high-risk faithful, or
    low-risk factual faithful) are kept + hash-stamped; verdicts for dropped/rewritten
    claim_ids are discarded. Returns (verdicts, #claims)."""
    meta = topic_dir / "meta"
    frames = {f: _read_judge_file(meta, f"judge_{f}.json") for f in _JUDGE_FRAMES}
    low = _read_judge_file(meta, "judge_lowrisk.json")

    merged: dict[str, str] = {}
    for cid in {c for fr in frames.values() for c in fr}:
        votes = [frames[f].get(cid) for f in _JUDGE_FRAMES]
        if any(v == "not_entailed" for v in votes):
            merged[cid] = "not_entailed"
        elif all(v == "entailed" for v in votes):
            merged[cid] = "entailed"
        else:
            merged[cid] = "unclear"
    for cid, verdict in low.items():
        merged.setdefault(cid, verdict if verdict in ("entailed", "not_entailed", "unclear") else "unclear")

    # freshness + relevance filter: keep only verdicts for claims that currently
    # exist AND need a judge (so stale/rewritten ids drop out).
    targets: dict[str, str] = {}
    review_path = topic_dir / "review.md"
    if review_path.exists():
        store = refs.load(topic_dir)
        if store is not None:
            results = evaluate(store, review_path.read_text(encoding="utf-8"), topic_dir=topic_dir)
            need = {cv.claim_id: claim_hash(cv) for cv in results if cv.claim_id and _needs_judge(cv)}
            merged = {cid: v for cid, v in merged.items() if cid in need}
            targets = need

    meta.mkdir(parents=True, exist_ok=True)
    (meta / "judge_verdicts.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (meta / "judge_targets.json").write_text(
        json.dumps(targets, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return merged, len(merged)


def _needs_judge(cv: "ClaimVerdict") -> bool:
    """A claim that must carry a positive-entailment verdict (spec §4 '每事实断言
    ...正面蕴含 verdict'): a cited factual claim currently judged ``faithful``.
    inference / method-log rows are grounding-exempt; non-faithful already fail."""
    return cv.verdict == "faithful" and (cv.risk == "high" or cv.key_type == "verified_entry")


def apply_judgments(
    results: list[ClaimVerdict], judgments: dict[str, str]
) -> list[ClaimVerdict]:
    """Ingest the judge ensemble's verdicts (deterministic merge): ``not_entailed``
    → suspect; ``unclear`` on a high-risk OR abstract_fallback faithful claim →
    needs_review (护栏 #3: a coarse fallback span gets no leniency at low risk)."""
    for cv in results:
        verdict = judgments.get(cv.claim_id)
        if verdict == "not_entailed":
            cv.verdict = "suspect"
            cv.reason = (cv.reason + "; LLM judge: span 不正面蕴含断言").lstrip("; ")
        elif (verdict == "unclear" and cv.verdict == "faithful"
              and (cv.risk == "high" or cv.span_source == "abstract_fallback")):
            cv.verdict = "needs_review"
            cv.reason = (cv.reason + "; LLM judge: 蕴含不明，升人工").lstrip("; ")
    return results


def _write_faithfulness_report(
    meta_dir: pathlib.Path, results: list[ClaimVerdict]
) -> None:
    meta_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {
        "faithful": 0, "suspect": 0, "needs_review": 0, "insufficient": 0
    }
    lines = [
        "# Faithfulness Report",
        "",
        "| key | grounding | verdict | reason |",
        "| --- | --- | --- | --- |",
    ]
    for cv in results:
        counts[cv.verdict] = counts.get(cv.verdict, 0) + 1
        reason_short = cv.reason[:120].replace("|", "\\|")
        lines.append(
            f"| {cv.key} | {cv.grounding} | {cv.verdict} | {reason_short} |"
        )
    lines += [
        "",
        f"**Summary**: {len(results)} claims — "
        + " / ".join(f"{v}: {counts.get(v, 0)}" for v in counts),
        "",
    ]
    (meta_dir / "faithfulness_report.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )


def _write_claim_evidence_map(
    meta_dir: pathlib.Path, results: list[ClaimVerdict]
) -> None:
    meta_dir.mkdir(parents=True, exist_ok=True)
    # F2 full schema: claim_id / key / key_type / risk / grounding / span_section /
    # verdict / atomic_claim / span (machine-checked evidence span).
    lines = [
        "# Claim–Evidence Map",
        "",
        "| claim_id | key | key_type | risk | grounding | span_section | span_source | verdict | atomic_claim | span |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    def _cell(text: str, limit: int) -> str:
        return (text or "-")[:limit].replace("|", "\\|").replace("\n", " ")

    for cv in results:
        lines.append(
            f"| {cv.claim_id or '-'} | {cv.key} | {cv.key_type} | {cv.risk} | "
            f"{cv.grounding} | {cv.span_section or '-'} | {cv.span_source} | {cv.verdict} | "
            f"{_cell(cv.atomic_claim or cv.sentence, 90)} | {_cell(cv.span, 60)} |"
        )
    (meta_dir / "claim_evidence_map.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deterministic faithfulness gate for review.md citations."
    )
    parser.add_argument("topic_dir", help="Path to reviews/<topic>")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 if any verdict is 'suspect'.",
    )
    parser.add_argument(
        "--judge-prompt",
        action="store_true",
        help="Emit the F8 decorrelated entailment-judge prompt for high-risk claims "
        "(the write-loop ensemble fills meta/judge_{span,polarity,entail}.json).",
    )
    parser.add_argument(
        "--judge-prompt-low",
        action="store_true",
        help="Emit the single-judge prompt for low-risk factual claims (spec §0.6.g) "
        "→ meta/judge_lowrisk.json.",
    )
    parser.add_argument(
        "--combine-judges",
        action="store_true",
        help="Deterministically merge judge_{span,polarity,entail,lowrisk}.json → "
        "meta/judge_verdicts.json (+ judge_targets.json freshness stamps).",
    )
    args = parser.parse_args()

    topic_dir = pathlib.Path(args.topic_dir)

    if args.combine_judges:
        _verdicts, n = combine_judges(topic_dir)
        print(f"[faithfulness] combined judges → meta/judge_verdicts.json ({n} claim(s))")
        raise SystemExit(0)

    with testflight.timer("faithfulness", "main", topic_dir=topic_dir) as detail:
        store = refs.load(topic_dir)
        if store is None:
            print(f"[ERROR] missing references store: {topic_dir}")
            raise SystemExit(1)

        review_path = topic_dir / "review.md"
        if not review_path.exists():
            print(f"[ERROR] review.md not found: {review_path}")
            raise SystemExit(1)

        review_text = review_path.read_text(encoding="utf-8")
        results = evaluate(store, review_text, topic_dir=topic_dir)

        if args.judge_prompt:
            print(judge_prompt(results, risk="high"))
            raise SystemExit(0)
        if args.judge_prompt_low:
            print(judge_prompt(results, risk="low"))
            raise SystemExit(0)

        meta_dir = topic_dir / "meta"
        _write_faithfulness_report(meta_dir, results)
        _write_claim_evidence_map(meta_dir, results)

        suspect_count = sum(1 for cv in results if cv.verdict == "suspect")
        detail["claims"] = len(results)
        detail["suspect"] = suspect_count

        counts: dict[str, int] = {}
        for cv in results:
            counts[cv.verdict] = counts.get(cv.verdict, 0) + 1

        print(
            f"[faithfulness] {len(results)} claims checked — "
            + " / ".join(f"{v}: {counts.get(v, 0)}" for v in
                         ("faithful", "suspect", "needs_review", "insufficient"))
        )
        print(f"[faithfulness] report: {meta_dir / 'faithfulness_report.md'}")
        print(f"[faithfulness] map:    {meta_dir / 'claim_evidence_map.md'}")

        if args.strict and suspect_count:
            print(f"[ERROR] --strict: {suspect_count} suspect claim(s)")
            raise SystemExit(1)


if __name__ == "__main__":
    main()
