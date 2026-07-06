"""tools/evidence_extract.py — structured evidence extraction (plan v3.1 F9 / spec N6, §2).

The "LLM 抽取 + 确定性 span/数字校验的包装" node. Following the codebase pattern
(tools are deterministic; LLM judgment is done by spawned agents), this splits
into two modes:

  --prompt   : assemble the cited entries' source text + emit an extraction
               prompt for a Sonnet agent to fill {pico,n,intervention,outcome,
               effect,ci,p,limitations,coi,span,span_section} per entry.
  --validate : ingest the agent's extraction JSON and DETERMINISTICALLY verify
               each row — the bound span must really occur in the source, and
               every number in n/effect/ci/p must occur inside that span. A field
               that fails validation, or a high-risk field with no fulltext, is
               marked ``uncertain`` (write_gate fails high-risk ``uncertain``).
               Writes ``meta/evidence_table.{json,md}`` — the structured layer
               the writer composes from (spec N7: 据此写、不临场合成).

    python tools/evidence_extract.py reviews/<topic> --prompt
    python tools/evidence_extract.py reviews/<topic> --validate extraction.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import faithfulness
import refs
from lib import annotated, layout

_NUMERIC_FIELDS = ("n", "effect", "ci", "p")
_SCHEMA_FIELDS = (
    "pico", "n", "intervention", "outcome", "effect", "ci", "p",
    "limitations", "coi", "span", "span_section",
    # C10b (v3.2 §0.6.e): a verbatim source sentence supporting a NO-NUMBER cited
    # conclusion (entity + direction + comparator) — the only clean span for a no-number
    # fact claim; without it faithfulness's 护栏 makes such a claim suspect.
    "qualitative_span",
)


def _cited_keys(store: refs.Store, review_text: str) -> list[str]:
    keys = faithfulness._CITATION_RE.findall(review_text)
    seen: dict[str, None] = {}
    for key in keys:
        seen.setdefault(key, None)
    return list(seen)


def _cite_recommend_keys(topic_dir: pathlib.Path, store: refs.Store) -> list[str]:
    """spec N6: evidence_extract's input is the analysts' cite_recommend (the writer's MENU), not
    'whatever review.md happens to cite'. On the FIRST write review.md is a bootstrap scaffold with
    no [@key], so the old first-30-verified fallback pre-extracted the wrong entries — the writer's
    real cite_recommend (often added in later rounds, outside the first 30) then had no
    evidence_table row, so it couldn't ground their numbers → brief_insufficient (testflight: a
    saturated creatine topic stalled because wang2024/ferguson2006 — both cite_recommend, one
    fulltext — were missing from the table). Parse the persisted annotated.md cite rows
    (``| [@key] | type | number | cite … |``) and keep the verified ones, deduped across rounds×gaps."""
    verified = {
        e.get("citation_key")
        for e in store.get("entries", {}).values()
        if e.get("verification_status") == "verified"
    }
    keys: dict[str, None] = {}
    # C11: the shared tolerant parser accepts pipe-table AND section-list / inline annotated
    # shapes, so a non-table annotated no longer strands the whole gap's cite_recommend menu
    # (testflight F7 → brief_insufficient).
    for ann in sorted((topic_dir / "notes").glob("round-*/*.annotated.md")):
        for key, _study_type in annotated.parse(ann).get("cite", []):
            ck = key.lstrip("@")
            if ck in verified:
                keys.setdefault(ck, None)
    return list(keys)


def _annotated_numbers(topic_dir: pathlib.Path) -> dict[str, str]:
    """C15: the analyst already pulled each cite row's 关键数字 into annotated.md
    (``| [@key] | study_type | 关键数字 | cite … |``). Surface them to the extractor as
    hints so it LOCATES that number's span instead of cold re-extracting (and risking a
    different / invented value). Pipe-table rows only — the number cell has no analogue in
    the section-list shape."""
    out: dict[str, str] = {}
    for ann in sorted((topic_dir / "notes").glob("round-*/*.annotated.md")):
        for raw in ann.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 4:
                continue
            m = faithfulness._CITATION_RE.search(cells[0])
            num = cells[2]
            if m and num and num not in ("—", "-", "") and "见上" not in cells[0]:
                out.setdefault(m.group(1), num)
    return out


def validate_extraction(
    store: refs.Store, extraction: dict[str, dict[str, object]]
) -> dict[str, dict[str, object]]:
    """Deterministically validate an agent's extraction. Returns the table with
    a ``span_validated`` bool, ``grounding`` level, and any unverifiable numeric
    field downgraded to ``uncertain``."""
    table: dict[str, dict[str, object]] = {}
    for key, row in extraction.items():
        doi = refs.resolve_citation_key(store, key)
        entry = store.get("entries", {}).get(doi) if doi else None
        source = faithfulness._load_source_text(entry) if entry else ""
        grounding = refs.grounding(entry) if entry else "title_only"
        span = str(row.get("span") or "")
        norm_source = faithfulness._norm_digits(source)
        span_ok = bool(span.strip()) and span.strip()[:20] in norm_source

        out: dict[str, object] = {field: row.get(field, "") for field in _SCHEMA_FIELDS}
        out["grounding"] = grounding
        out["span_validated"] = span_ok
        # C10b: a qualitative_span is usable only if it occurs verbatim in source (same
        # existence check faithfulness re-applies); else blank it so a hallucinated span
        # can never become a no-number claim's grounding.
        qspan = str(row.get("qualitative_span") or "").strip()
        out["qualitative_span"] = qspan if qspan and qspan[:20] in norm_source else ""
        for field in _NUMERIC_FIELDS:
            value = str(row.get(field) or "")
            nums = faithfulness._extract_numbers(value)
            if nums and not all(
                faithfulness._number_in_source(num, span)[0] for num in nums
            ):
                out[field] = "uncertain"  # number not inside the validated span
        if not span_ok:
            # no validated span → the whole row is unanchored; high-risk fields
            # become uncertain so write_gate refuses to rest a claim on them.
            for field in _NUMERIC_FIELDS:
                if out[field]:
                    out[field] = "uncertain"
            out["span_section"] = out.get("span_section") or "unverified"
        table[key] = out
    return table


def write_table(topic_dir: pathlib.Path, table: dict[str, dict[str, object]]) -> None:
    meta = topic_dir / layout.META_DIRNAME
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "evidence_table.json").write_text(
        json.dumps(table, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# Evidence Table",
        "",
        "| key | grounding | n | effect | ci | p | span_section | span_validated |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for key, row in table.items():
        lines.append(
            f"| {key} | {row.get('grounding','-')} | {row.get('n','-') or '-'} | "
            f"{row.get('effect','-') or '-'} | {row.get('ci','-') or '-'} | "
            f"{row.get('p','-') or '-'} | {row.get('span_section','-') or '-'} | "
            f"{row.get('span_validated')} |"
        )
    (meta / "evidence_table.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_prompt(
    store: refs.Store, keys: list[str], number_hints: dict[str, str] | None = None
) -> str:
    number_hints = number_hints or {}
    blocks = []
    for key in keys:
        doi = refs.resolve_citation_key(store, key)
        entry = store.get("entries", {}).get(doi) if doi else None
        source = faithfulness._load_source_text(entry) if entry else ""
        hint = number_hints.get(key)
        hint_line = (
            f"\n（C15 analyst 已抽关键数字: {hint} — 请在下面原文中**定位其 span**，勿改值 / 勿另抽）"
            if hint else ""
        )
        blocks.append(f"### [@{key}]{hint_line}\n{source[:1500] or '(no source text)'}")
    fields = ", ".join(_SCHEMA_FIELDS)
    return (
        "你是证据抽取器（Sonnet）。对下列每条被引文献，从其原文抽取结构化字段并返回 "
        "JSON：{key: {" + fields + "}}。\n"
        "- 每个量化字段（n/effect/ci/p）必须附带 `span`：包含该数字的**逐字原文片段**。\n"
        "- `span_section` 标注该 span 所在章节（results/methods/abstract/…）。\n"
        "- `qualitative_span`：若该文献的核心被引结论是**无数字的定性发现**（机制/比较/方向，"
        "如『构象变化改变受体结合』『免疫逃逸增强』），抽**一句逐字原文**，须含断言的**实体+方向+对照**"
        "（供无数字断言 grounding；无则留空）。\n"
        "- 抽不到就留空字符串，不要编造。下游工具会确定性校验 span 与数字。\n\n"
        + "\n\n".join(blocks)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="evidence_extract — 证据抽取 + span 校验 (F9).")
    parser.add_argument("topic_dir")
    parser.add_argument("--prompt", action="store_true", help="Emit the extraction prompt.")
    parser.add_argument("--validate", default=None, help="Validate an extraction JSON file.")
    args = parser.parse_args()

    topic_dir = pathlib.Path(args.topic_dir)
    store = refs.load(topic_dir)
    if store is None:
        print(f"[ERROR] missing references store: {topic_dir}", file=sys.stderr)
        raise SystemExit(1)

    if args.validate:
        extraction = json.loads(pathlib.Path(args.validate).read_text(encoding="utf-8"))
        table = validate_extraction(store, extraction)
        write_table(topic_dir, table)
        uncertain = sum(
            1 for row in table.values()
            for field in _NUMERIC_FIELDS if row.get(field) == "uncertain"
        )
        print(f"[evidence_extract] {len(table)} rows → meta/evidence_table.* "
              f"({uncertain} uncertain numeric field(s))")
        raise SystemExit(0)

    review_path = topic_dir / "review.md"
    keys = _cited_keys(store, review_path.read_text(encoding="utf-8")) if review_path.exists() else []
    # R-testflight: a scaffold review.md has no [@key], so prefer the analysts' cite_recommend MENU
    # (the entries the writer will actually cite) over the first-30-verified last-resort fallback —
    # the latter silently dropped cite_recommend entries added after round 1 and stalled the writer.
    if not keys:
        keys = _cite_recommend_keys(topic_dir, store)
    if not keys:
        keys = [
            entry.get("citation_key", "")
            for entry in store.get("entries", {}).values()
            if entry.get("verification_status") == "verified"
        ][:30]
    number_hints = _annotated_numbers(topic_dir)  # C15
    print(build_prompt(store, [k for k in keys if k], number_hints))
    raise SystemExit(0)


if __name__ == "__main__":
    main()
