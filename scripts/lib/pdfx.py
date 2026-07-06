from __future__ import annotations

import re
from typing import Literal, TypedDict

import fitz  # type: ignore[import-untyped]

ExtractionStatus = Literal["ok", "partial", "failed"]


class SectionResult(TypedDict, total=False):
    abstract: str
    introduction: str
    conclusion: str
    extraction_status: ExtractionStatus
    error: str


class _Span(TypedDict):
    text: str
    size: float
    bold: bool


HEADER_MAP = {
    "abstract": {"abstract", "summary"},
    "introduction": {"introduction", "background"},
    "conclusion": {"conclusion", "conclusions", "discussion", "summary"},
}


def _as_dict(value: object) -> dict[str, object] | None:
    return value if isinstance(value, dict) else None


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_float(value: object) -> float | None:
    return value if isinstance(value, float) else None


def _iter_spans(pdf_path: str) -> list[_Span]:
    document = fitz.open(pdf_path)
    spans: list[_Span] = []
    try:
        for page in document:
            page_dict = _as_dict(page.get_text("dict"))
            for block_obj in _as_list((page_dict or {}).get("blocks")):
                block = _as_dict(block_obj)
                if block is None:
                    continue
                for line_obj in _as_list(block.get("lines")):
                    line = _as_dict(line_obj)
                    if line is None:
                        continue
                    for span_obj in _as_list(line.get("spans")):
                        span = _as_dict(span_obj)
                        if span is None:
                            continue
                        text = (_as_str(span.get("text")) or "").strip()
                        size = _as_float(span.get("size")) or 0.0
                        font = _as_str(span.get("font")) or ""
                        if text:
                            spans.append(
                                {
                                    "text": text,
                                    "size": size,
                                    "bold": "bold" in font.lower(),
                                }
                            )
    finally:
        document.close()
    return spans


def _body_size(spans: list[_Span]) -> float:
    sizes = sorted(span["size"] for span in spans if span["size"] > 0)
    if not sizes:
        return 10.0
    return sizes[len(sizes) // 2]


def _normalized_header(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def extract_sections(pdf_path: str) -> SectionResult:
    spans = _iter_spans(pdf_path)
    body_size = _body_size(spans)

    headers: list[tuple[int, str]] = []
    for index, span in enumerate(spans):
        text = span["text"]
        if len(text) > 80:
            continue
        if not span["bold"] and span["size"] < body_size + 1:
            continue
        normalized = _normalized_header(text)
        for section_name, aliases in HEADER_MAP.items():
            if normalized in aliases:
                headers.append((index, section_name))
                break

    sections: dict[str, str] = {"abstract": "", "introduction": "", "conclusion": ""}
    for position, (start_index, section_name) in enumerate(headers):
        if sections[section_name]:
            continue
        end_index = headers[position + 1][0] if position + 1 < len(headers) else len(spans)
        content = " ".join(span["text"] for span in spans[start_index + 1 : end_index]).strip()
        sections[section_name] = re.sub(r"\s+", " ", content)[:6000]

    if all(sections.values()):
        status: ExtractionStatus = "ok"
    elif any(sections.values()):
        status = "partial"
    else:
        status = "failed"

    return {
        "abstract": sections["abstract"],
        "introduction": sections["introduction"],
        "conclusion": sections["conclusion"],
        "extraction_status": status,
    }
