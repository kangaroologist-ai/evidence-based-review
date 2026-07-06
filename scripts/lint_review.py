"""Pre-submission lint for a review topic.

Read-only counterpart to render_refs.py: runs the same citation / gap checks
**without** writing anything. Also adds an "uncited verified entries" audit
that matches the 轮后取舍 principle in CLAUDE.md.

CLI:
    python scripts/lint_review.py reviews/<topic>

Exit codes:
    0  — all checks passed, no warnings
    1  — hard fail (missing key / not verified / retracted / excluded /
         phantom gap / gap under-supported)
    2  — warn-only (uncited verified entries above --warn-threshold)

Flags:
    --strict                       promote exit 2 to exit 1
    --min-verified-per-gap N       fail if a pending gap has fewer than N
                                   verified cited supports (default 1)
    --warn-threshold N             warn if >= N verified entries go uncited
                                   (default 1, i.e. any uncited triggers warn)
    --audit-all-gaps               also fail for resolved / insufficient gaps
                                   below --min-verified-per-gap
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib import config, patches, project, testflight
import refs
import term_check

# Citation + marker scanning lives in lib/citation_scan so lint_review,
# render_refs and gaps_status agree byte-for-byte on what counts as a cited
# key (otherwise lint PASS / render missing-key regressions creep back in).
from lib.citation_scan import (
    CITE_RE,
    PRISMA_MARK_RE,
    REFS_MARK_RE,
    strip_tool_managed_blocks as _strip_tool_managed_blocks,
)

# Anchor patterns — used only by _check_prose_metadata to decide whether
# prose actually attributes the [@key] to a specific author/year. See
# config.PROSE_ANCHOR_BEFORE_CHARS for the window size.
#
# Pattern A — "Family 等/et al." appears before [@key]; an optional year
# may live in the trailing connector text (e.g. "Hazari 等在 2007 年" or
# "Hazari et al. (2007)").
# Family must start uppercase followed by at least one lowercase letter
# (Hazari, McDonald, O'Connor) — skips all-caps abbreviations like EC,
# HEC, RNA, DNA which would otherwise be mistaken for author surnames.
_FAMILY_PATTERN = r"[A-Z][a-z][A-Za-z'’\-]*(?:[\s\-][A-Z][a-z][A-Za-z'’\-]*)?"
# English "et al." needs whitespace separator; Chinese "等" is often glued
# to the family name with no space ("Hazari等"). Match both forms so the
# Codex round 3 P2 漏检 case (Hazari等 in CJK prose) is caught.
ANCHOR_AUTHOR_MARKER_RE = re.compile(
    rf"({_FAMILY_PATTERN})(?:\s+et\s+al\.?|\s*等)"
)
# When the "等" marker is immediately followed by a quantity/list expression
# ("等 8 个属", "等多个品种", "等部分品种"), "等" is "etc" not "et al" —
# the marker isn't an author anchor at all.
_QUANTITY_AFTER_MARKER_RE = re.compile(
    r"^\s*(?:"
    r"(?:\d+\s*)?[个种属株类例项条只名点处]"
    r"|(?:部分|多个|多种|若干|一些|其他|少数|某些|各类)"
    r"(?:品种|物种|种|属|菌株|株|类|个体|样本)?"
    r")"
)
# Pattern B — closed-paren "(Family, YEAR)" tag right before [@key].
ANCHOR_PAREN_RE = re.compile(
    rf"[(（]\s*({_FAMILY_PATTERN})[\s,，]+(\d{{4}})\s*[)）][\s,，:;：、。]*$"
)
# Used to reject anchor matches whose "trail" between marker and [@key]
# contains another English family name — that suggests the prose is
# discussing multiple sources and the [@key] doesn't attribute to the
# earlier marker.
_TRAIL_FORBIDDEN_ENGLISH_NAME_RE = re.compile(r"[A-Z][a-z]")
_TRAIL_YEAR_RE = re.compile(r"(\d{4})")
# Pandoc multi-citation tokens like "[@bollen2015; @bollen2016]" can spill
# into the trail when the [@key] we're checking is the second one in the
# group. Citation-key embedded digits must not be mistaken for prose years.
_PANDOC_CITATION_TOKEN_RE = re.compile(r"\[@[^\]]*")

SUSPICIOUS_TITLE_RE = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in config.SUSPICIOUS_TITLE_PATTERNS
]

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_WARN = 2
CitationOccurrence = tuple[int, str]


def _load_review(topic_dir: pathlib.Path) -> str:
    review_path = project.review_path(topic_dir)
    if not review_path.exists():
        print(f"[ERROR] {review_path.name} not found under {topic_dir}")
        raise SystemExit(EXIT_FAIL)
    return review_path.read_text(encoding="utf-8")


def _index_by_key(store: refs.Store) -> dict[str, refs.Entry]:
    return {
        entry["citation_key"]: entry
        for entry in store["entries"].values()
        if isinstance(entry.get("citation_key"), str)
    }


def _collect_citation_occurrences(review_text: str) -> list[CitationOccurrence]:
    # Strip tool-managed blocks (PRISMA flow / References) so `@key`-shaped
    # strings the tool writes into them don't count as real citations.
    # Matches render_refs.py scanning behavior — render and lint must
    # agree on what counts as a citation, otherwise lint reports phantom
    # missing keys after a re-render.
    scannable = _strip_tool_managed_blocks(review_text)
    return [(match.start(), match.group(1)) for match in CITE_RE.finditer(scannable)]


def _is_multi_citation_occurrence(review_text: str, offset: int) -> bool:
    group_start = review_text.rfind("[", 0, offset)
    group_end = review_text.find("]", offset)
    if group_start == -1 or group_end == -1:
        return False
    group = review_text[group_start : group_end + 1]
    return group.count("@") > 1


def _is_citable_verified(entry: refs.Entry) -> bool:
    return (
        entry.get("verification_status") == "verified"
        and not entry.get("retracted")
        and not entry.get("excluded_reason")
    )


def _check_citations(
    used_keys: list[str],
    by_key: dict[str, refs.Entry],
) -> list[str]:
    """Return a list of FAIL messages for missing / non-verified / retracted /
    excluded citations. Matches render_refs.py semantics."""
    errors: list[str] = []

    missing = [key for key in used_keys if key not in by_key]
    if missing:
        errors.append(f"[FAIL] citation keys not in references store: {missing}")

    not_verified = [
        key
        for key in used_keys
        if key in by_key and by_key[key].get("verification_status") != "verified"
    ]
    if not_verified:
        errors.append(f"[FAIL] citations are not verified: {not_verified}")

    retracted = [
        key
        for key in used_keys
        if key in by_key and bool(by_key[key].get("retracted", False))
    ]
    if retracted:
        errors.append(f"[FAIL] retracted citations in review: {retracted}")

    excluded = [
        key
        for key in used_keys
        if key in by_key and by_key[key].get("excluded_reason")
    ]
    if excluded:
        errors.append(
            f"[FAIL] excluded entries cited in review: {excluded} "
            "(remove citation or run refs.include_entry)"
        )

    return errors


def _check_cited_ratio(
    used_keys: list[str],
    store: refs.Store,
    threshold: float,
    topic_dir: pathlib.Path,
) -> list[str]:
    if threshold <= 0:
        return []
    if (topic_dir / ".lint_legacy").exists():
        print("[INFO] .lint_legacy present; cited-ratio hard gate skipped")
        return []

    eligible: list[refs.Entry] = [
        entry
        for entry in store["entries"].values()
        if isinstance(entry.get("citation_key"), str) and _is_citable_verified(entry)
    ]
    if not eligible:
        return []

    used = set(used_keys)
    cited = [entry for entry in eligible if entry["citation_key"] in used]
    ratio = len(cited) / len(eligible)
    if ratio >= threshold:
        return []

    uncited = sorted(
        (
            entry["citation_key"],
            entry.get("gap") if isinstance(entry.get("gap"), str) else None,
            entry.get("added_round") if isinstance(entry.get("added_round"), int) else None,
        )
        for entry in eligible
        if entry["citation_key"] not in used
    )
    top = ", ".join(
        f"{key}(gap={gap or '-'}, r{round_number if round_number is not None else '?'})"
        for key, gap, round_number in uncited[: config.TOP_N_UNCITED_IN_FAIL]
    )
    message = (
        f"[FAIL] cited verified ratio {len(cited)}/{len(eligible)} = {ratio:.1%} "
        f"< threshold={threshold:.0%}"
    )
    if top:
        message += f"\n  top uncited: {top}"
    return [message]


def _check_gaps(
    used_keys: list[str],
    store: refs.Store,
    by_key: dict[str, refs.Entry],
    min_verified_per_gap: int,
    audit_all: bool,
) -> list[str]:
    """Return a list of FAIL messages for gap-level problems:
    - phantom gap (entry.gap refers to undeclared gap id)
    - declared gap with fewer than N verified cited supports
    """
    errors: list[str] = []
    gaps = store.get("gaps", {})

    phantom = [
        f"{entry.get('citation_key', entry.get('doi', '?'))}->{gap_id}"
        for entry in store["entries"].values()
        for gap_id in [entry.get("gap")]
        if isinstance(gap_id, str) and gap_id not in gaps
    ]
    if phantom:
        errors.append(f"[FAIL] entries reference undeclared gaps: {phantom}")

    if not gaps:
        return errors

    key_to_gap = {
        entry["citation_key"]: entry.get("gap")
        for entry in store["entries"].values()
        if isinstance(entry.get("citation_key"), str)
    }
    per_gap_cited: dict[str, list[str]] = collections.defaultdict(list)
    for key in used_keys:
        gap_id = key_to_gap.get(key)
        if isinstance(gap_id, str) and gap_id in gaps:
            # Cited key must be verified (caller also runs _check_citations,
            # but we guard here in case of partial failure).
            entry = by_key.get(key)
            if entry and entry.get("verification_status") == "verified" and not entry.get(
                "retracted"
            ):
                per_gap_cited[gap_id].append(key)

    weak: list[str] = []
    for gap_id, meta in sorted(gaps.items()):
        status = meta.get("status", "pending")
        if status != "pending" and not audit_all:
            continue
        cited_count = len(per_gap_cited.get(gap_id, []))
        if cited_count < min_verified_per_gap:
            weak.append(
                f"{gap_id} [{status}] cited_verified={cited_count} "
                f"< min={min_verified_per_gap}"
            )

    if weak:
        errors.append(
            "[FAIL] gaps below --min-verified-per-gap:\n  "
            + "\n  ".join(weak)
        )

    return errors


def _gap_citation_counts(
    used_keys: list[str],
    store: refs.Store,
) -> dict[str, int]:
    used = set(used_keys)
    counts: collections.Counter[str] = collections.Counter()
    for entry in store["entries"].values():
        key = entry.get("citation_key")
        gap_id = entry.get("gap")
        if not (isinstance(key, str) and isinstance(gap_id, str)):
            continue
        if key not in used or not _is_citable_verified(entry):
            continue
        counts[gap_id] += 1
    return dict(counts)


def _gap_verified_counts(store: refs.Store) -> dict[str, int]:
    counts: collections.Counter[str] = collections.Counter()
    for entry in store["entries"].values():
        gap_id = entry.get("gap")
        if isinstance(gap_id, str) and _is_citable_verified(entry):
            counts[gap_id] += 1
    return dict(counts)


def _check_gap_classification(store: refs.Store) -> tuple[list[str], list[str]]:
    """Per-gap classification audit.

    Returns ``(errors, warnings)`` — currently all gap_type / required-
    field problems are WARN level (back-compat for legacy stores). Lift
    to FAIL once playbook §2 is stable and all open reviews are migrated.

    Checks (per gap):
      - gap_type set (one of the 7 playbook types)
      - required sub-fields filled (per gap_type)
      - depends_on / subgap_of point to declared gaps (no phantom refs)
    """
    errors: list[str] = []
    warnings: list[str] = []
    gaps = store.get("gaps", {})
    declared = set(gaps)
    for gap_id, meta in sorted(gaps.items()):
        if not isinstance(meta, dict):
            continue
        gap_type = meta.get("gap_type")
        if not gap_type:
            warnings.append(
                f"[WARN] gap {gap_id} missing gap_type — declare with "
                f"`verify.py --declare-gap {gap_id} '...' --gap-type ...`"
            )
        else:
            fields = meta.get("fields") if isinstance(meta.get("fields"), dict) else {}
            required = patches.REQUIRED_FIELDS_BY_GAP_TYPE.get(gap_type, ())
            missing = [f for f in required if not fields.get(f)]
            if missing:
                flags = ", ".join(f"--{m.replace('_', '-')}" for m in missing)
                warnings.append(
                    f"[WARN] gap {gap_id} (type={gap_type}) missing required "
                    f"fields: {flags}"
                )
        for dep in meta.get("depends_on") or []:
            if isinstance(dep, str) and dep not in declared and dep != gap_id:
                errors.append(
                    f"[FAIL] gap {gap_id} depends_on undeclared gap: {dep}"
                )
        parent = meta.get("subgap_of")
        if (
            isinstance(parent, str)
            and parent
            and parent not in declared
            and parent != gap_id
        ):
            errors.append(
                f"[FAIL] gap {gap_id} subgap_of undeclared gap: {parent}"
            )
    return errors, warnings


def _check_gap_count_and_breadth(store: refs.Store) -> list[str]:
    """Warn if gap count > 8 (playbook §2.5 reverse exemplar #6) or if
    any gap description is > 200 chars (signals an over-broad gap packing
    multiple unrelated questions — reverse exemplar #2)."""
    warnings: list[str] = []
    gaps = store.get("gaps", {})
    if len(gaps) > 8:
        warnings.append(
            f"[WARN] {len(gaps)} declared gaps — playbook §2.5 反例 #6 警告 "
            "'≥8 太碎'，考虑合并近义 gap 或归到一个父 gap 的 subgap"
        )
    for gap_id, meta in sorted(gaps.items()):
        if not isinstance(meta, dict):
            continue
        desc = meta.get("description", "")
        if isinstance(desc, str) and len(desc) > 200:
            warnings.append(
                f"[WARN] gap {gap_id} description={len(desc)} chars > 200 — "
                "playbook 反例 #2 '一个 gap 包多个不相关命题'，考虑拆为 sub-gaps"
            )
    return warnings


def _check_protocol_filled(topic_dir: pathlib.Path) -> list[str]:
    """Warn if research_log.md protocol section still has unfilled
    '_user 填_' placeholders. Doesn't fail — user may write protocol
    inline in review.md instead."""
    log_path = topic_dir / "research_log.md"
    if not log_path.exists():
        return [
            f"[WARN] research_log.md not found under {topic_dir} — "
            "protocol section cannot be verified"
        ]
    text = log_path.read_text(encoding="utf-8")
    placeholder_count = text.count("_user 填_")
    if placeholder_count == 0:
        return []
    return [
        f"[WARN] research_log.md protocol section has {placeholder_count} "
        "unfilled '_user 填_' slots — fill before publishing"
    ]


_LIMIT_SECTION_RE = re.compile(r"^#+[^\n]*(?:限定|争议|局限)", re.M)
_METHOD_SECTION_RE = re.compile(r"^#+[^\n]*(?:方法|methods)", re.M | re.I)

# --- abbrev gloss check (CLAUDE.md Prose style 第 11 条 → lint 机械化) ---
#
# Catch all-caps abbreviations / Latin-name initialisms (>=2 leading
# uppercase letters, optional trailing digits so SPF15 / UV400 stay whole)
# whose FIRST occurrence isn't accompanied by a nearby Chinese gloss.
# The boundaries reject embedding inside a longer alnum run so we don't
# split "SPF15" into "SPF" + "1" the way a bare `[A-Z]{2,}[0-9]?` would.
ABBREV_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2,}[0-9]*(?![A-Za-z0-9])")
# CJK ideographs (covers the gloss text + the closing 中文 after a 括注).
_CJK_RE = re.compile(r"[一-鿿]")
# Fenced code block (```lang ... ```) — pseudo-math / commands inside don't
# render LaTeX and shouldn't be gloss-checked. DOTALL across the fence.
_FENCED_CODE_RE = re.compile(r"```.*?```", re.S)
# Inline code span (`tool.py`, `API_RATE_LIMIT_…`, `--source both`) — file
# names / identifiers / flags are full of uppercase tokens that are not prose
# abbreviations and never carry a Chinese gloss. Strip before scanning.
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
# Lines that are essentially a bare URL / DOI — skip wholesale (the path
# segments are full of uppercase tokens that aren't prose abbreviations).
_URL_DOI_LINE_RE = re.compile(r"https?://|doi\.org/|^\s*10\.\d{4,}/", re.I)
# Window (chars) AFTER the token's first occurrence to look for a Chinese
# gloss. The project gloss idiom writes "中文名（english, ABBR）" so the
# abbreviation sits *inside* the paren and CJK follows right after the
# closing ）— a forward window reliably catches it. 40 chars per spec §C.
ABBREV_GLOSS_WINDOW_CHARS = 40
_WHITELIST_PATH = pathlib.Path(__file__).parent / "data" / "abbrev_whitelist.txt"


def _load_abbrev_whitelist() -> set[str]:
    """Load the abbreviation gloss whitelist (one token per line, `#`
    comments stripped). Missing file → empty set (check still runs, just
    with no exemptions). Tokens are stored verbatim (case-sensitive)."""
    if not _WHITELIST_PATH.exists():
        return set()
    out: set[str] = set()
    for raw in _WHITELIST_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


def _abbrev_stem(token: str) -> str:
    """Strip trailing digits → alphabetic stem (SPF15 → SPF, UV400 → UV).
    Used so a whitelist line like `UV400` matches and so numeric variants
    of an already-seen abbreviation don't each re-trigger a WARN."""
    return token.rstrip("0123456789")


# Backward gloss idiom — the abbreviation sits inside a parenthetical that a
# Chinese term *immediately before the open paren* glosses:
#   "无可观察不良作用剂量（NOAEL）", "腹直肌分离（DRA）", "异噻唑啉酮（MIT/CMIT/BIT）",
#   "剥脱点阵激光（CO2、Er:YAG）", "技术误差(TEM)".
# Forward-only checking would false-positive every one of these (the CJK gloss
# is behind the token, not ahead). We accept the token as glossed when it is
# inside a `…CJK（ … ABBR … ）` group, i.e. its enclosing parenthetical is
# preceded by CJK. The token may be anywhere inside the paren (head, middle,
# or tail of a slash/、-separated abbreviation list).
_OPEN_PAREN = "（("
_CLOSE_PAREN = "）)"
# CJK appears within this many chars before the open paren to count as the
# gloss for the parenthetical (handles "维 A 酸（…" where a latin letter +
# space sit between the last han char and the paren).
_BACKWARD_GLOSS_LOOKBEHIND = 8


def _enclosing_paren_open(line: str, start: int, end: int) -> int:
    """Index of the open paren of the parenthetical enclosing the token at
    [start, end), or -1 if the token isn't inside a `（…）` / `(...)` group
    on this line. Requires a close paren at/after the token (scanning right)
    and an open paren before it (scanning left), with no intervening paren of
    the opposite kind. Bounded scans keep it line-local."""
    # Right: a close paren must follow (optionally past more list items /
    # separators), with no open paren opening a new group first.
    close_found = False
    for i in range(end, min(len(line), end + 60)):
        if line[i] in _OPEN_PAREN:
            break
        if line[i] in _CLOSE_PAREN:
            close_found = True
            break
    if not close_found:
        return -1
    # Left: the matching open paren, with no close paren of a prior group.
    for i in range(start - 1, max(-1, start - 60), -1):
        if line[i] in _CLOSE_PAREN:
            return -1
        if line[i] in _OPEN_PAREN:
            return i
    return -1


def _has_backward_paren_gloss(line: str, start: int, end: int) -> bool:
    """True for the `中文（…ABBR…）` gloss idiom: the token is inside a
    parenthetical whose open paren is immediately preceded by CJK."""
    open_idx = _enclosing_paren_open(line, start, end)
    if open_idx == -1:
        return False
    lookbehind = line[max(0, open_idx - _BACKWARD_GLOSS_LOOKBEHIND) : open_idx]
    return bool(_CJK_RE.search(lookbehind))


def _blank_out(match: re.Match[str]) -> str:
    """Replace a matched span with blank lines preserving its newline count,
    so downstream line numbering stays aligned with the original file."""
    return "\n" * match.group(0).count("\n")


def _scannable_prose(review_text: str) -> str:
    """Replace tool-managed blocks (PRISMA / References), fenced code blocks,
    and inline code spans with blanks so abbreviation scanning only sees
    author prose **and reported line numbers still match the source file**.
    Fenced blocks first (they may contain backticks), then inline spans.
    Tool-managed blocks are blanked here (rather than via
    _strip_tool_managed_blocks which deletes them) to keep line numbers
    honest for the WARN. URL/DOI line filtering happens later per-line."""
    text = PRISMA_MARK_RE.sub(_blank_out, review_text)
    text = REFS_MARK_RE.sub(_blank_out, text)
    text = _FENCED_CODE_RE.sub(_blank_out, text)
    text = _INLINE_CODE_RE.sub(_blank_out, text)
    return text


def _check_abbrev_gloss(
    review_text: str,
    whitelist: set[str],
) -> list[str]:
    """WARN (never FAIL) for all-caps abbreviations / Latin initialisms whose
    FIRST occurrence in prose lacks a Chinese gloss within
    ``ABBREV_GLOSS_WINDOW_CHARS`` chars after the token.

    Mechanizes CLAUDE.md Prose style 第 11 条 (缩写首现带中文) so the whole
    class is cleared before reviewers spend a round catching one missing
    gloss at a time. Deliberately WARN-only: the regex over-collects (table
    headers, stat abbreviations) and a hard FAIL would block legitimate
    drafts; the warning is enough to make the author look.

    Skips: tool-managed blocks, fenced + inline code, and lines that are
    essentially a bare URL/DOI. Recognises both gloss idioms —
    "中文（english, ABBR）" (CJK in the forward window) and "中文（ABBR）"
    (CJK before the enclosing paren). Tracks the digit-stripped stem so
    SPF / SPF15 / SPF30 are checked once (at the earliest occurrence)."""
    prose = _scannable_prose(review_text)
    seen_stems: set[str] = set()
    findings: list[tuple[str, int]] = []
    line_no = 0
    for line in prose.splitlines():
        line_no += 1
        if _URL_DOI_LINE_RE.search(line):
            continue
        for match in ABBREV_RE.finditer(line):
            token = match.group(0)
            stem = _abbrev_stem(token)
            if stem in seen_stems:
                continue
            seen_stems.add(stem)
            # Whitelist: exempt by exact token or by alphabetic stem
            # (so one `UV400` / `DOI` line covers its numeric siblings).
            if token in whitelist or stem in whitelist:
                continue
            # Glossed if CJK sits in the forward window (the dominant
            # "中文（english, ABBR）中文…" idiom) OR the token closes a
            # parenthetical whose gloss precedes it ("中文（ABBR）").
            window = line[match.end() : match.end() + ABBREV_GLOSS_WINDOW_CHARS]
            if _CJK_RE.search(window):
                continue
            if _has_backward_paren_gloss(line, match.start(), match.end()):
                continue
            findings.append((token, line_no))
    if not findings:
        return []
    listed = ", ".join(f"{tok}@line{ln}" for tok, ln in findings)
    return [
        f"[WARN] {len(findings)} 个缩写/学名首现缺中文译名（Prose style 第 11 条）: "
        f"{listed}\n  首次出现处紧跟括注中文，或加入 tools/data/abbrev_whitelist.txt"
    ]


def _check_review_structure(review_text: str) -> list[str]:
    """Warn if review.md is missing the structural sections required by the
    project review template: 限定与争议 + 方法 (PRISMA-flow + 检索表 lives
    in 方法 by convention).

    These are WARN (not FAIL) so legacy reviews don't break the lint pipeline,
    but new reviews should add them — playbook §7."""
    warnings: list[str] = []
    if not _LIMIT_SECTION_RE.search(review_text):
        warnings.append(
            "[WARN] review.md missing §限定与争议 / 局限 section — playbook §7 要求"
        )
    if not _METHOD_SECTION_RE.search(review_text):
        warnings.append(
            "[WARN] review.md missing §方法 section (PRISMA-flow + 检索表; "
            "should be at end before References) — playbook §7 要求"
        )
    return warnings


def _check_broad_gaps(used_keys: list[str], store: refs.Store) -> list[str]:
    warnings: list[str] = []
    cited_counts = _gap_citation_counts(used_keys, store)
    verified_counts = _gap_verified_counts(store)
    for gap_id, gap in sorted(store.get("gaps", {}).items()):
        description = gap.get("description", "")
        broad_terms = [term for term in config.BROAD_GAP_BLACKLIST if term in description]
        verified = verified_counts.get(gap_id, 0)
        cited = cited_counts.get(gap_id, 0)
        broad_by_volume = verified >= config.BROAD_GAP_VERIFIED_THRESHOLD
        if not (broad_terms or broad_by_volume):
            continue
        if cited >= config.BROAD_GAP_MIN_CITED:
            continue
        reasons: list[str] = []
        if broad_terms:
            reasons.append(f"broad wording={broad_terms}")
        if broad_by_volume:
            reasons.append(f"verified={verified}")
        warnings.append(
            f"[WARN] broad or under-used gap {gap_id}: cited={cited} "
            f"< min={config.BROAD_GAP_MIN_CITED}; {'; '.join(reasons)}"
        )
    return warnings


def _family_from_name(author: str) -> str:
    """Extract the most-likely "core family name" for fuzzy matching against
    prose.

    Handles "Al Dehaybes, M." → "dehaybes" (prose typically writes the
    surname without the Arabic article); "Van Der Berg, J." → "berg";
    "Hazari, Z." → "hazari"; "Smith" → "smith". Takes the last word of the
    pre-comma part so multi-word surnames with particles (Al, Van, De,
    Mc, etc.) reduce to the core family token — codex round 3 P2.
    """
    name_part = author.split(",", 1)[0].strip() if "," in author else author
    parts = name_part.split()
    return parts[-1].lower() if parts else ""


def _normalize_family(value: str) -> str:
    return re.sub(r"[^a-z]", "", value.lower())


def _levenshtein(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    prev = list(range(len(right) + 1))
    for i, left_char in enumerate(left, 1):
        row = [i]
        for j, right_char in enumerate(right, 1):
            cost = 0 if left_char == right_char else 1
            row.append(min(row[-1] + 1, prev[j] + 1, prev[j - 1] + cost))
        prev = row
    return prev[-1]


def _extract_prose_anchor(
    review_text: str,
    offset: int,
) -> tuple[str | None, int | None]:
    """Return (prose_family, prose_year) extracted from the small window
    immediately before [@key] at offset, IF an anchor pattern matches.

    Two accepted patterns:
      A. "Family 等" or "Family et al." sits in the window; the trail to
         [@key] must not contain another English family name (which would
         indicate multi-source prose). Year (4 digits) is harvested from
         the trail if present — covers both "Hazari 等 (2007)" and
         "Hazari 等在 2007 年" forms.
      B. "(Family, YEAR)" closed-paren tag right before [@key].

    Returns (None, None) if no anchor matches — caller skips the entry to
    avoid the wide-window false positives seen in the 木浆海绵 audit
    (background years / other-citation authors got mis-attributed).
    """
    # CITE_RE matches `@key`, so `offset` points at `@`. Trim a leading `[`
    # from the right edge so anchors that should sit immediately before
    # `[@key]` aren't blocked by the bracket.
    end = offset - 1 if offset > 0 and review_text[offset - 1] == "[" else offset
    before = review_text[max(0, end - config.PROSE_ANCHOR_BEFORE_CHARS):end]

    # Pattern A — pick the marker closest to [@key] (last match in window).
    marker_matches = list(ANCHOR_AUTHOR_MARKER_RE.finditer(before))
    if marker_matches:
        marker = marker_matches[-1]
        trail = before[marker.end():]
        # "等" + 数字 + 中文量词 → "等" = etc, not "et al"; skip anchor.
        if not _QUANTITY_AFTER_MARKER_RE.match(trail):
            # Strip any Pandoc citation tokens from the trail before scanning
            # for year/another-name — "[@bollen2015;" embeds digits and lower-
            # case letters that aren't prose. See test fixture for the
            # multi-citation pattern this protects against.
            trail_clean = _PANDOC_CITATION_TOKEN_RE.sub("", trail)
            if not _TRAIL_FORBIDDEN_ENGLISH_NAME_RE.search(trail_clean):
                year_match = _TRAIL_YEAR_RE.search(trail_clean)
                prose_year = int(year_match.group(1)) if year_match else None
                return marker.group(1), prose_year

    # Pattern B — parenthesised "(Family, YEAR)" tag.
    paren = ANCHOR_PAREN_RE.search(before)
    if paren is not None:
        return paren.group(1), int(paren.group(2))

    return None, None


def _check_prose_metadata(
    review_text: str,
    occurrences: list[CitationOccurrence],
    by_key: dict[str, refs.Entry],
) -> list[str]:
    """Flag prose-vs-metadata conflicts and suspicious entry titles.

    Anchor-based: only compares prose to entry metadata when an explicit
    "Author et al./等 (YEAR)?" or "(Author, YEAR)" tag sits immediately
    before [@key]. Bare year mentions or other-citation author names
    further out are intentionally skipped — see the 2026-05-22 audit
    where 80-char wide windows produced ~65 false positives on a 100%
    cited exemplar (木浆海绵). Hazari-class real errors (prose has
    "Hazari 等 (2007)" anchor but entry year=2008) still trip the check.
    """
    errors: list[str] = []
    checked_titles: set[str] = set()

    # Suspicious entry-title pattern check (independent of anchor logic).
    for _, key in occurrences:
        if key in checked_titles or key not in by_key:
            continue
        checked_titles.add(key)
        title = by_key[key].get("title", "")
        if isinstance(title, str) and any(pattern.search(title) for pattern in SUSPICIOUS_TITLE_RE):
            errors.append(
                f"[FAIL] citation [@{key}] has suspicious entry title: {title!r}"
            )

    # Anchor-based prose-vs-metadata check.
    for offset, key in occurrences:
        entry = by_key.get(key)
        if entry is None:
            continue
        if _is_multi_citation_occurrence(review_text, offset):
            continue
        prose_family, prose_year = _extract_prose_anchor(review_text, offset)
        if prose_family is None and prose_year is None:
            # No anchor — don't compare; prose may mention bare years or
            # other-citation authors that don't actually attribute to this [@key].
            continue

        if prose_family is not None:
            authors = entry.get("authors", [])
            if authors:
                entry_family_norm = _normalize_family(_family_from_name(authors[0]))
                prose_family_norm = _normalize_family(prose_family)
                if (
                    prose_family_norm
                    and entry_family_norm
                    and _levenshtein(prose_family_norm, entry_family_norm) > 1
                ):
                    errors.append(
                        f"[FAIL] citation [@{key}] @ offset {offset} nearby prose "
                        f"uses author '{prose_family}', "
                        f"entry first author='{authors[0]}'"
                    )

        if prose_year is not None:
            entry_year = entry.get("year")
            if isinstance(entry_year, int) and prose_year != entry_year:
                errors.append(
                    f"[FAIL] citation [@{key}] @ offset {offset} nearby prose "
                    f"uses year {prose_year}, entry year={entry_year}"
                )

    return errors


def _audit_uncited(
    used_keys: list[str],
    store: refs.Store,
) -> list[tuple[str, str | None, int | None]]:
    """Return (citation_key, gap_id, added_round) tuples for verified,
    non-excluded, non-retracted entries that are not cited in the review
    body.

    Mirrors CLAUDE.md's 轮后取舍 principle: verified ≠ 必须引用 — but a high
    count of uncited entries suggests the previous round pulled in too
    much. The added_round tag lets the caller see "round 2 gap-2 +22 went
    nowhere" at a glance, which round's expansion strategy needs revisiting.
    """
    used = set(used_keys)
    uncited: list[tuple[str, str | None, int | None]] = []
    for entry in store["entries"].values():
        citation_key = entry.get("citation_key")
        if not isinstance(citation_key, str):
            continue
        if citation_key in used:
            continue
        if entry.get("verification_status") != "verified":
            continue
        if entry.get("retracted") or entry.get("excluded_reason"):
            continue
        gap = entry.get("gap") if isinstance(entry.get("gap"), str) else None
        added_round = entry.get("added_round")
        if not isinstance(added_round, int):
            added_round = None
        uncited.append((citation_key, gap, added_round))
    uncited.sort()
    return uncited


def _print_header(topic_dir: pathlib.Path, store: refs.Store, used_keys: list[str]) -> None:
    total = len(store["entries"])
    verified = sum(
        1
        for entry in store["entries"].values()
        if entry.get("verification_status") == "verified"
    )
    gaps = len(store.get("gaps", {}))
    print(
        f"lint_review: {topic_dir.name} — "
        f"entries={total} verified={verified} gaps={gaps} "
        f"cited={len(set(used_keys))}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pre-submission lint for a review topic (read-only)."
    )
    parser.add_argument("topic_dir")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Promote uncited-entry warnings to failures.",
    )
    parser.add_argument(
        "--min-verified-per-gap",
        type=int,
        default=1,
        help="Fail if a pending gap has fewer verified cited supports (default 1).",
    )
    parser.add_argument(
        "--warn-threshold",
        type=int,
        default=1,
        help="Warn if >= N verified entries go uncited (default 1).",
    )
    parser.add_argument(
        "--audit-all-gaps",
        action="store_true",
        help="Also fail on resolved / insufficient gaps below threshold.",
    )
    parser.add_argument(
        "--cited-threshold",
        type=float,
        default=config.CITED_RATIO_THRESHOLD,
        help="Fail if cited verified ratio falls below this threshold (default 0.5).",
    )
    parser.add_argument(
        "--skip-prose-metadata",
        action="store_true",
        help="Skip citation-neighborhood prose vs entry metadata checks.",
    )
    parser.add_argument(
        "--skip-abbrev-gloss",
        action="store_true",
        help="Skip the abbreviation-first-occurrence Chinese-gloss WARN "
        "(Prose style 第 11 条 mechanization).",
    )
    parser.add_argument(
        "--require-term-check",
        action="store_true",
        help="Run term_check even before the automatic round>=3 trigger.",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print only gap-level uncited counts and skip the per-entry key "
        "list (which can be 400+ lines on a noisy store). Use this for a "
        "quick post-write health check; default keeps the full list as "
        "audit trail.",
    )
    args = parser.parse_args()

    topic_dir = pathlib.Path(args.topic_dir)
    with testflight.timer("lint_review", "main", topic_dir=topic_dir) as detail:
        store = refs.load(topic_dir)
        if store is None:
            print(f"[ERROR] no references store under {topic_dir}")
            raise SystemExit(EXIT_FAIL)

        review_text = _load_review(topic_dir)
        occurrences = _collect_citation_occurrences(review_text)
        used_keys = sorted({key for _, key in occurrences})
        by_key = _index_by_key(store)

        _print_header(topic_dir, store, used_keys)

        errors: list[str] = []
        errors.extend(_check_citations(used_keys, by_key))
        errors.extend(
            _check_cited_ratio(
                used_keys,
                store,
                args.cited_threshold,
                topic_dir,
            )
        )
        errors.extend(
            _check_gaps(
                used_keys,
                store,
                by_key,
                args.min_verified_per_gap,
                args.audit_all_gaps,
            )
        )
        if not args.skip_prose_metadata:
            errors.extend(_check_prose_metadata(review_text, occurrences, by_key))

        # gap classification (gap_type + required sub-fields + dep / subgap
        # phantom refs). Classification problems are mostly WARN for now —
        # phantom dep/subgap refs are FAIL (no excuse).
        # `.lint_legacy` marker silences the new structure/protocol/classification
        # warnings for pre-migration stores; phantom dep/subgap refs still FAIL.
        class_errors, class_warnings = _check_gap_classification(store)
        errors.extend(class_errors)

        warnings: list[str] = []
        if (topic_dir / ".lint_legacy").exists():
            print(
                "[INFO] .lint_legacy present; gap classification + protocol "
                "+ review structure warnings skipped"
            )
        else:
            warnings.extend(class_warnings)
            warnings.extend(_check_gap_count_and_breadth(store))
            warnings.extend(_check_protocol_filled(topic_dir))
            warnings.extend(_check_review_structure(review_text))
        warnings.extend(_check_broad_gaps(used_keys, store))
        # Abbreviation gloss WARN (Prose style 第 11 条). On by default; the
        # .lint_legacy marker silences it like the other structure warnings,
        # and --skip-abbrev-gloss is an explicit per-run escape.
        if not args.skip_abbrev_gloss and not (topic_dir / ".lint_legacy").exists():
            warnings.extend(
                _check_abbrev_gloss(review_text, _load_abbrev_whitelist())
            )

        # Always run term_check — the original round>=3 guard let early-stop
        # reviews slip past lint when their cited-ratio happened to be high
        # enough. Codex round 3 P1 finding: 1-2 round reviews shouldn't be
        # allowed through this gate just because no one passes --require-term-check.
        # .lint_legacy marker doubles as a term_check escape valve too — same
        # rationale as cited-ratio: lets stockpile reviews ship while new
        # reviews face the hard gate.
        latest_round = term_check.latest_round(store)
        term_result = term_check.evaluate_store(store)
        if not term_result.ok:
            if (topic_dir / ".lint_legacy").exists():
                print(
                    "[INFO] .lint_legacy present; term_check hard gate skipped "
                    f"(would have failed: {len(term_result.messages)} reasons)"
                )
            else:
                errors.append("[FAIL] term_check failed:\n  " + "\n  ".join(term_result.messages))

        uncited = _audit_uncited(used_keys, store)

        if errors:
            for err in errors:
                print(err)

        if warnings:
            for warning in warnings:
                print(warning)

        if uncited:
            # Group by gap, then within a gap show counts per round so the
            # caller can attribute waste to a specific round's expansion.
            by_gap: dict[str | None, list[tuple[str, int | None]]] = (
                collections.defaultdict(list)
            )
            for key, gap, added_round in uncited:
                by_gap[gap].append((key, added_round))
            print(f"[WARN] {len(uncited)} verified entries not cited in review:")
            for gap in sorted(by_gap, key=lambda g: (g is None, g or "")):
                label = gap if gap is not None else "<no gap>"
                round_counts: collections.Counter[int | None] = collections.Counter(
                    rnd for _, rnd in by_gap[gap]
                )
                round_summary = " ".join(
                    f"r{rnd if rnd is not None else '?'}={count}"
                    for rnd, count in sorted(
                        round_counts.items(),
                        key=lambda item: (item[0] is None, item[0] or 0),
                    )
                )
                if args.summary:
                    print(f"  {label} [{round_summary}]")
                else:
                    key_list = ", ".join(
                        f"{key}(r{rnd if rnd is not None else '?'})"
                        for key, rnd in by_gap[gap]
                    )
                    print(f"  {label} [{round_summary}]: {key_list}")
            print(
                "  考虑 prune 或放入 §争议 / §限定（对应 CLAUDE.md 轮后取舍原则）"
            )

        detail.update(
            {
                "errors": len(errors),
                "warnings": len(warnings),
                "uncited": len(uncited),
                "cited": len(used_keys),
                "latest_round": latest_round,
            }
        )

        if errors:
            raise SystemExit(EXIT_FAIL)

        if warnings or (uncited and len(uncited) >= args.warn_threshold):
            if args.strict:
                raise SystemExit(EXIT_FAIL)
            raise SystemExit(EXIT_WARN)

        print("OK")


if __name__ == "__main__":
    main()
