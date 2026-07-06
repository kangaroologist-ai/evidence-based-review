from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib import project
from lib.pdfx import extract_sections
import refs


def _extract_one(pdf_path_str: str) -> dict[str, object]:
    """Worker function — runs in a child process (pymupdf is CPU-bound and
    holds the GIL during parsing, so threads do not help; processes do)."""
    try:
        return dict(extract_sections(pdf_path_str))
    except Exception as exc:  # pragma: no cover — child-side guard
        return {
            "abstract": "",
            "introduction": "",
            "conclusion": "",
            "extraction_status": "failed",
            "error": str(exc),
        }


# Markdown cards written by fetch.py have this structure:
#   ## Abstract\n<text>\n\n## Introduction\n<text>\n\n## Conclusion\n<text>\n
# We patch only Introduction / Conclusion, leaving the EuPMC-sourced Abstract
# alone. If a section is currently empty *and* the PDF extraction yielded text,
# splice it in.
_SECTION_RE = re.compile(
    r"(##\s+(?P<name>Abstract|Introduction|Conclusion)\s*\n)(?P<body>.*?)(?=\n##\s+|\Z)",
    re.DOTALL,
)


def _merge_into_card(card_path: pathlib.Path, sections: dict[str, object]) -> bool:
    """Splice PDF-extracted Introduction / Conclusion into an existing abstract
    card. Returns True if the file was modified. Leaves Abstract section
    untouched (EuPMC abstract is canonical when present)."""
    if not card_path.exists():
        return False
    text = card_path.read_text(encoding="utf-8")
    new_text = text
    for slot in ("Introduction", "Conclusion"):
        pdf_value = sections.get(slot.lower(), "")
        if not isinstance(pdf_value, str) or not pdf_value.strip():
            continue
        match = _SECTION_RE.search(new_text) and re.search(
            rf"(##\s+{slot}\s*\n)(.*?)(?=\n##\s+|\Z)", new_text, re.DOTALL
        )
        if not match:
            continue
        existing_body = match.group(2).strip()
        if existing_body:
            # Don't overwrite EuPMC content with PDF guess.
            continue
        replacement = match.group(1) + pdf_value.strip() + "\n"
        new_text = new_text[: match.start()] + replacement + new_text[match.end():]
    if new_text != text:
        card_path.write_text(new_text, encoding="utf-8")
        return True
    return False


def _classify(entry: refs.Entry) -> tuple[str, pathlib.Path | None]:
    """Decide whether to extract; return (state, pdf_path) where state is one
    of 'skipped', 'missing', 'ready'."""
    paths = entry["paths"]
    fetch_state = entry["fetch_state"]
    if entry.get("retracted", False):
        return "skipped_retracted", None
    if entry.get("has_fulltext_xml", False):
        return "skipped_xml", None
    pdf_rel_path = paths.get("pdf")
    pdf_path = project.to_abs(pdf_rel_path)
    if pdf_path is None:
        return "no_pdf", None
    if not pdf_path.exists():
        return "missing", pdf_path
    return "ready", pdf_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("topic_dir", help="Path to a topic directory under reviews/")
    parser.add_argument("--doi")
    parser.add_argument(
        "--workers",
        type=int,
        default=max(1, (os.cpu_count() or 4) // 2),
        help="Parallel worker processes (default: half of CPU count).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-extract even if fetch_state.pdf_text is already succeeded.",
    )
    args = parser.parse_args()

    topic_dir = pathlib.Path(args.topic_dir)
    store = refs.load(topic_dir)
    if store is None:
        print(f"[ERROR] missing references store: {topic_dir}")
        raise SystemExit(1)

    target_doi = args.doi.lower() if args.doi else None
    matched_target = target_doi is None
    output_dir = project.topic_tmp(topic_dir.name) / "assets" / "pdfs_text"
    output_dir.mkdir(parents=True, exist_ok=True)
    abstracts_dir = project.topic_tmp(topic_dir.name) / "abstracts"

    # Partition entries into work / skip lists in a single pass.
    work: list[tuple[str, pathlib.Path]] = []
    for doi, raw_entry in store["entries"].items():
        entry = refs._ensure_defaults(raw_entry)
        store["entries"][doi] = entry
        if target_doi and doi != target_doi:
            continue
        matched_target = True
        state, pdf_path = _classify(entry)
        fetch_state = entry["fetch_state"]
        paths = entry["paths"]
        if state == "skipped_retracted" or state == "skipped_xml" or state == "no_pdf":
            fetch_state["pdf_text"] = "skipped"
            paths["pdf_text"] = None
            continue
        if state == "missing":
            fetch_state["pdf_text"] = "failed"
            paths["pdf_text"] = None
            continue
        if (
            fetch_state.get("pdf_text") in {"succeeded", "failed"}
            and not args.force
            and not target_doi
        ):
            continue
        assert pdf_path is not None
        work.append((doi, pdf_path))

    if not matched_target:
        print(f"[ERROR] DOI not found in references store: {target_doi}")
        raise SystemExit(1)

    if not work:
        refs.save(topic_dir, store)
        print("nothing to extract")
        return

    workers = max(1, min(args.workers, len(work)))
    processed = 0
    merged = 0

    # Submit all jobs upfront; collect results as they complete. ProcessPool
    # handles its own queueing, and pymupdf is CPU-bound so this scales near
    # linearly on multi-core hosts.
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_extract_one, str(pdf)): doi for doi, pdf in work}
        for future in as_completed(futures):
            doi = futures[future]
            entry = store["entries"][doi]
            fetch_state = entry["fetch_state"]
            paths = entry["paths"]
            sections = future.result()
            fetch_state["pdf_text"] = (
                "failed" if sections.get("extraction_status") == "failed" else "succeeded"
            )
            output_path = output_dir / f"{project.safe_doi(doi)}.json"
            output_path.write_text(
                json.dumps(sections, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            paths["pdf_text"] = project.to_rel(output_path)
            processed += 1

            # Splice intro/conclusion into the markdown card so prose authors
            # can read one file per paper instead of joining JSON + MD.
            card_path = abstracts_dir / f"{project.safe_doi(doi)}.md"
            if _merge_into_card(card_path, sections):
                merged += 1

    refs.save(topic_dir, store)
    print(f"extracted {processed} PDFs ({workers} workers); merged sections into {merged} cards")


if __name__ == "__main__":
    main()
