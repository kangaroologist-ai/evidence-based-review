"""Shared tolerant parser for gap-X.annotated.md analyst four-bucket lists (v3.2 C11).

The analyst is asked to write per-gap rows as a Markdown pipe table
``| [@key] | study_type | 数字 | cite/keep/uncertain — 理由 |``. Under pressure it
sometimes emits a SECTION-LIST instead (a ``cite_recommend:`` heading followed by
``- [@key] 理由`` bullets) or a non-table inline line ``[@key] cohort 45% cite: …``.
The old pipe-table-only parsers silently dropped the WHOLE gap's cites for those shapes
(testflight F7: a non-table annotated → 0 evidence_table rows → brief_insufficient, and
research_log's four-bucket transcription went blank). This parser accepts all three.

Bias is INCLUSIVE on cite: a missed cite_recommend key strands the writer (high cost),
while an over-included key merely adds one evidence_table row (low cost). Both
evidence_extract (cite menu) and research_log (transcription) consume this.
"""
from __future__ import annotations

import pathlib
import re

VERDICTS = ("cite", "keep", "uncertain")
_KEY_RE = re.compile(r"\[@([^\]\s]+)\]")
_BULLET_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s")

# Verdict aliases incl. Chinese words the analyst may use (引用/保留/不确定/排除). cite-inclusive.
_VERDICT_RES = (
    ("cite", re.compile(r"\bcite\w*|引用|采用|入选", re.IGNORECASE)),
    ("keep", re.compile(r"\bkeep\w*|保留|留库|备查", re.IGNORECASE)),
    ("uncertain", re.compile(r"\buncertain\w*|不确定|待定|存疑|待裁", re.IGNORECASE)),
    ("exclude", re.compile(r"\bexclude\w*|排除|剔除|删除", re.IGNORECASE)),
)
_VERDICT_RE_BY_NAME = dict(_VERDICT_RES)


def _verdict_in(text: str) -> str | None:
    """Resolve a verdict from a verdict cell / free-form fragment. **cite-inclusive** bias: a
    missed cite strands the writer (the costliest error — brief_insufficient); an over-included
    key only adds one evidence_table row. So a `cite` token ANYWHERE wins — this is what fixes
    the reason-first mis-bucket ('这条不该 exclude，故 cite' → cite; 'keep in mind … cite' → cite).
    Else a LEADING verdict token (well-formed '判定 + 理由'), else presence (keep > uncertain >
    exclude). Handles Chinese verdict words. NB exclude is NOT an annotated.md row verdict (those
    are cite/keep/uncertain per the analyst schema) — so 'exclude' in a reason is non-authoritative."""
    s = text.strip().lstrip("*_ ")
    if not s:
        return None
    if _VERDICT_RE_BY_NAME["cite"].search(s):
        return "cite"
    for v in ("keep", "uncertain", "exclude"):
        if _VERDICT_RE_BY_NAME[v].match(s):  # leading token
            return v
    for v in ("keep", "uncertain", "exclude"):
        if _VERDICT_RE_BY_NAME[v].search(s):  # presence fallback
            return v
    return None


def _section_verdict(line: str) -> str | None:
    """If the line is a bucket HEADER (heading / bold / trailing-colon, names exactly one
    bucket, carries no [@key]), return its verdict word; else None."""
    s = line.strip()
    if "[@" in s:
        return None
    is_header = (
        s.startswith("#") or s.startswith("**") or s.startswith("- **")
        or s.endswith(":") or s.endswith("：")
    )
    if not is_header:
        return None
    low = s.lower()
    # NB order: 'keep_uncited' contains the substring 'cite', so test 'keep' before 'cite'.
    for needle, verdict in (("exclude", "exclude"), ("uncertain", "uncertain"),
                            ("keep", "keep"), ("cite", "cite"),
                            ("排除", "exclude"), ("不确定", "uncertain"),
                            ("保留", "keep"), ("引用", "cite")):
        if needle in low:
            return verdict
    return None


def parse_text(text: str) -> dict[str, list[tuple[str, str]]]:
    """Return {verdict: [(``@key``, study_type)]} for cite/keep/uncertain."""
    out: dict[str, list[tuple[str, str]]] = {v: [] for v in VERDICTS}
    section: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        sv = _section_verdict(line)
        if sv is not None:
            section = sv
            continue
        key_match = _KEY_RE.search(line)
        if not key_match or "见上" in line:
            continue
        key = f"@{key_match.group(1)}"
        verdict: str | None = None
        study_type = ""
        # 1. pipe-table row: | [@key] | study_type | num | verdict… | — cite-inclusive parse
        #    of the verdict cell (fixes the reason-first mis-bucket '不该 exclude，故 cite').
        if "|" in line:
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 4 and _KEY_RE.search(cells[0]):
                study_type = cells[1]
                verdict = _verdict_in(cells[3])
        # 2. section-list bullet under a bucket header → inherit the section verdict
        #    (the section is authoritative; a stray verdict word in the reason is ignored).
        if verdict is None and section is not None and _BULLET_RE.match(raw):
            verdict = section
        # 3. free-form inline line → cite-inclusive verdict from the post-key fragment.
        if verdict is None:
            verdict = _verdict_in(line[key_match.end():])
        if verdict in VERDICTS:
            out[verdict].append((key, study_type))
    return out


def parse(path: pathlib.Path) -> dict[str, list[tuple[str, str]]]:
    if not path.exists():
        return {v: [] for v in VERDICTS}
    return parse_text(path.read_text(encoding="utf-8"))
