from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Literal, cast

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib import apis, project, testflight
import refs

FetchStatus = Literal["pending", "succeeded", "failed", "skipped"]
AssetRecord = dict[str, object]
EUPMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
UNPAYWALL_BASE = "https://api.unpaywall.org/v2"
CROSSREF_BASE = "https://api.crossref.org"
# When an EuPMC abstract is shorter than this, also probe CrossRef and pick
# whichever copy is longer. EuPMC sometimes truncates around 1400 chars on
# certain journals; CrossRef keeps the full JATS-marked abstract there.
_CROSSREF_ABSTRACT_FALLBACK_MIN = 1500
VALID_INCLUDE = {"abstract", "fulltext", "figures", "tables", "pdf"}
STATE_KEY_MAP = {"fulltext": "fulltext_xml"}
# FetchState keys that _get_fetch_status / _set_fetch_status may read or
# write. pdf_text intentionally excluded — it is set directly via
# apply_fetch_result, never via these helpers.
_FETCH_STATE_KEYS = frozenset({"abstract", "fulltext_xml", "figures", "tables", "pdf"})
SECTION_RE = re.compile(r"<sec\b[^>]*>\s*<title>([^<]+)</title>(.*?)</sec>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


@dataclass
class FetchResult:
    doi: str
    fetch_state_updates: dict[str, refs.FetchStatus] = field(default_factory=dict)
    paths_updates: dict[str, str | None] = field(default_factory=dict)
    oa_probe_succeeded: bool = False
    oa_status: str | None = None
    oa_pdf_url: str | None = None
    has_fulltext_xml: bool | None = None
    new_assets: list[AssetRecord] = field(default_factory=list)


def _as_dict(value: object) -> dict[str, object] | None:
    return value if isinstance(value, dict) else None


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _state_key(name: str) -> str:
    return STATE_KEY_MAP.get(name, name)


def _get_fetch_status(fetch_state: refs.FetchState, name: str) -> refs.FetchStatus:
    key = _state_key(name)
    if key not in _FETCH_STATE_KEYS:
        return "pending"
    return cast(refs.FetchStatus, cast(dict[str, object], fetch_state).get(key, "pending"))


def _set_fetch_status(
    fetch_state: refs.FetchState,
    name: str,
    status: refs.FetchStatus,
) -> None:
    key = _state_key(name)
    if key in _FETCH_STATE_KEYS:
        cast(dict[str, refs.FetchStatus], fetch_state)[key] = status


def _clean_text(value: str) -> str:
    without_tags = TAG_RE.sub(" ", value)
    return SPACE_RE.sub(" ", without_tags).strip()


def resolve_xml_status(has_xml_source: bool, xml_text: str) -> FetchStatus:
    if not has_xml_source:
        return "skipped"
    if xml_text.strip():
        return "succeeded"
    return "failed"


def resolve_text_status(text: str) -> FetchStatus:
    if text.strip():
        return "succeeded"
    return "failed"


def eupmc_search(doi: str) -> dict[str, object] | None:
    payload = apis.get_json(
        f"{EUPMC_BASE}/search",
        params={
            "query": f'DOI:"{doi}"',
            "format": "json",
            "resultType": "core",
        },
    )
    result_list = _as_dict((payload or {}).get("resultList"))
    results = _as_list((result_list or {}).get("result"))
    first = results[0] if results else None
    return first if isinstance(first, dict) else None


def unpaywall(doi: str) -> dict[str, object] | None:
    return apis.get_json(f"{UNPAYWALL_BASE}/{doi}", params={"email": apis.EMAIL})


def eupmc_xml(pmcid: str) -> str:
    return apis.get_text(f"{EUPMC_BASE}/{pmcid}/fullTextXML") or ""


def crossref_abstract(doi: str) -> str:
    """Return CrossRef's stored abstract (JATS markup stripped) or empty."""
    payload = apis.get_json(
        f"{CROSSREF_BASE}/works/{doi}",
        params=apis.with_mailto(),
    )
    message = _as_dict((payload or {}).get("message"))
    raw = _as_str((message or {}).get("abstract"))
    if not raw:
        return ""
    return _clean_text(raw)


def section(xml_text: str, names: tuple[str, ...]) -> str:
    for title, body in SECTION_RE.findall(xml_text):
        normalized_title = title.strip().lower()
        if any(name in normalized_title for name in names):
            return _clean_text(body)[:6000]
    return ""


def _suffix_from_href(href: str) -> str:
    suffix = pathlib.Path(href.split("?", 1)[0]).suffix.lower()
    if suffix:
        return suffix
    return ".bin"


def _best_oa_pdf_url(payload: dict[str, object] | None) -> str | None:
    best_location = _as_dict((payload or {}).get("best_oa_location"))
    if best_location is None:
        return None
    return _as_str(best_location.get("url_for_pdf"))


def figures(
    xml_text: str,
    pmcid: str,
    doi: str,
    assets_dir: pathlib.Path,
    max_figures: int,
) -> tuple[list[AssetRecord], bool]:
    figure_matches = list(re.finditer(r"<fig\b[^>]*>(.*?)</fig>", xml_text, re.I | re.S))
    if not figure_matches:
        return [], False

    records: list[AssetRecord] = []
    for index, match in enumerate(figure_matches[:max_figures], start=1):
        body = match.group(1)
        label_match = re.search(r"<label>(.*?)</label>", body, re.I | re.S)
        caption_match = re.search(r"<caption>(.*?)</caption>", body, re.I | re.S)
        href_match = re.search(r'xlink:href="([^"]+)"', body)
        label = _clean_text(label_match.group(1)) if label_match else f"Figure {index}"
        caption = _clean_text(caption_match.group(1)) if caption_match else ""

        source_url: str | None = None
        local_path: str | None = None
        if href_match:
            href = href_match.group(1)
            source_url = f"{EUPMC_BASE}/{pmcid}/supplementaryFiles?fname={href}"
            content = apis.get_bytes(source_url)
            if content:
                output_path = (
                    assets_dir
                    / "figures"
                    / f"{project.safe_doi(doi)}_fig{index}{_suffix_from_href(href)}"
                )
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(content)
                local_path = project.to_rel(output_path)

        records.append(
            {
                "id": f"fig:{project.safe_doi(doi)}_{index}",
                "type": "figure",
                "doi": doi,
                "label": label,
                "caption": caption,
                "source_url": source_url,
                "local_path": local_path,
                "selected": False,
            }
        )

    return records, True


def tables(
    xml_text: str,
    doi: str,
    assets_dir: pathlib.Path,
    max_tables: int,
) -> tuple[list[AssetRecord], bool]:
    table_matches = list(
        re.finditer(r"<table-wrap\b[^>]*>(.*?)</table-wrap>", xml_text, re.I | re.S)
    )
    if not table_matches:
        return [], False

    records: list[AssetRecord] = []
    for index, match in enumerate(table_matches[:max_tables], start=1):
        body = match.group(1)
        label_match = re.search(r"<label>(.*?)</label>", body, re.I | re.S)
        caption_match = re.search(r"<caption>(.*?)</caption>", body, re.I | re.S)
        label = _clean_text(label_match.group(1)) if label_match else f"Table {index}"
        caption = _clean_text(caption_match.group(1)) if caption_match else ""

        rows: list[list[str]] = []
        for row_text in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.I | re.S):
            cells: list[str] = []
            for cell_text in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row_text, re.I | re.S):
                cells.append(_clean_text(cell_text))
            if cells:
                rows.append(cells)

        table_path = assets_dir / "tables" / f"{project.safe_doi(doi)}_tbl{index}.md"
        table_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_lines = [f"# {label}", "", caption, ""]
        if rows:
            markdown_lines.append("| " + " | ".join(rows[0]) + " |")
            markdown_lines.append("| " + " | ".join(["---"] * len(rows[0])) + " |")
            for row in rows[1:]:
                markdown_lines.append("| " + " | ".join(row) + " |")
        table_path.write_text("\n".join(markdown_lines).rstrip() + "\n", encoding="utf-8")

        records.append(
            {
                "id": f"tbl:{project.safe_doi(doi)}_{index}",
                "type": "table",
                "doi": doi,
                "label": label,
                "caption": caption,
                "local_path": project.to_rel(table_path),
                "selected": False,
            }
        )

    return records, True


def write_card(
    entry: refs.Entry,
    abstract_text: str,
    introduction: str,
    conclusion: str,
    output_dir: pathlib.Path,
) -> pathlib.Path:
    lines = [
        "---",
        f"doi: {entry['doi']}",
        f"title: {entry.get('title', '')}",
        f"year: {entry.get('year', '')}",
        f"authors: {', '.join(entry.get('authors', []))}",
        f"journal: {entry.get('journal', '')}",
        f"study_type: {entry.get('study_type', '')}",
        f"oa_pdf: {entry.get('oa_pdf_url') or ''}",
        "---",
        "",
        "## Abstract",
        abstract_text,
        "",
        "## Introduction",
        introduction,
        "",
        "## Conclusion",
        conclusion,
        "",
    ]
    output_path = output_dir / f"{project.safe_doi(entry['doi'])}.md"
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


def _load_manifest(path: pathlib.Path) -> dict[str, AssetRecord]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return {}
    manifest: dict[str, AssetRecord] = {}
    for key, value in payload.items():
        if isinstance(key, str) and isinstance(value, dict):
            manifest[key] = value
    return manifest


def _store_assets(
    entry: refs.Entry,
    assets_dir: pathlib.Path,
    assets: list[AssetRecord],
) -> None:
    if not assets:
        return
    manifest_path = assets_dir / "manifest.json"
    manifest = _load_manifest(manifest_path)
    for asset in assets:
        asset_id = asset.get("id")
        if isinstance(asset_id, str):
            manifest[asset_id] = asset
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    existing_ids = set(entry.get("asset_ids", []))
    new_ids = {
        asset_id
        for asset_id in (asset.get("id") for asset in assets)
        if isinstance(asset_id, str)
    }
    entry["asset_ids"] = sorted(existing_ids | new_ids)


def _snapshot_for_fetch(entry: refs.Entry) -> refs.Entry:
    snapshot: refs.Entry = {
        "doi": entry["doi"],
        "title": entry.get("title", ""),
        "authors": list(entry.get("authors", [])),
        "journal": entry.get("journal", ""),
        "study_type": entry.get("study_type", "other"),
    }
    year = entry.get("year")
    if isinstance(year, int):
        snapshot["year"] = year
    snapshot["oa_pdf_url"] = entry.get("oa_pdf_url")
    return snapshot


def fetch_compute(
    snapshot: refs.Entry,
    base_tmp: pathlib.Path,
    include: set[str],
    max_figures: int,
    max_tables: int,
) -> FetchResult:
    doi = snapshot["doi"]
    result = FetchResult(doi=doi)
    abstracts_dir = base_tmp / "abstracts"
    assets_dir = base_tmp / "assets"
    pdfs_dir = assets_dir / "pdfs"
    abstracts_dir.mkdir(parents=True, exist_ok=True)
    pdfs_dir.mkdir(parents=True, exist_ok=True)

    need_eupmc = bool(include & {"abstract", "fulltext", "figures", "tables"})
    hit = eupmc_search(doi) if need_eupmc else None
    pmcid = _as_str((hit or {}).get("pmcid")) if hit else None
    is_open_access = (_as_str((hit or {}).get("isOpenAccess")) or "").upper() == "Y"
    has_xml_source = bool(pmcid and is_open_access)
    xml_text = eupmc_xml(pmcid) if pmcid and has_xml_source else ""
    xml_status = resolve_xml_status(has_xml_source, xml_text)
    result.has_fulltext_xml = xml_status == "succeeded"

    introduction = (
        section(xml_text, ("introduction", "background")) if xml_text else ""
    )
    conclusion = (
        section(xml_text, ("conclusion", "conclusions", "discussion", "summary"))
        if xml_text
        else ""
    )

    if "abstract" in include:
        abstract_text = _clean_text(_as_str((hit or {}).get("abstractText")) or "")
        if len(abstract_text) < _CROSSREF_ABSTRACT_FALLBACK_MIN:
            cr_abstract = crossref_abstract(doi)
            if len(cr_abstract) > len(abstract_text):
                abstract_text = cr_abstract
        if hit is None and not abstract_text:
            result.paths_updates["abstract"] = None
            result.fetch_state_updates["abstract"] = "skipped"
        elif abstract_text:
            card_path = write_card(
                snapshot,
                abstract_text,
                introduction,
                conclusion,
                abstracts_dir,
            )
            result.paths_updates["abstract"] = project.to_rel(card_path)
            result.fetch_state_updates["abstract"] = resolve_text_status(abstract_text)
        else:
            result.paths_updates["abstract"] = None
            result.fetch_state_updates["abstract"] = "failed"

    if "fulltext" in include:
        result.fetch_state_updates["fulltext_xml"] = xml_status

    if "figures" in include:
        if xml_status == "skipped":
            result.fetch_state_updates["figures"] = "skipped"
        elif xml_status == "failed":
            result.fetch_state_updates["figures"] = "failed"
        else:
            figure_assets, saw_figure_nodes = figures(
                xml_text,
                pmcid or "",
                doi,
                assets_dir,
                max_figures,
            )
            if not saw_figure_nodes:
                result.fetch_state_updates["figures"] = "skipped"
            elif figure_assets:
                result.fetch_state_updates["figures"] = "succeeded"
                result.new_assets.extend(figure_assets)
            else:
                result.fetch_state_updates["figures"] = "failed"

    if "tables" in include:
        if xml_status == "skipped":
            result.fetch_state_updates["tables"] = "skipped"
        elif xml_status == "failed":
            result.fetch_state_updates["tables"] = "failed"
        else:
            table_assets, saw_table_nodes = tables(
                xml_text,
                doi,
                assets_dir,
                max_tables,
            )
            if not saw_table_nodes:
                result.fetch_state_updates["tables"] = "skipped"
            elif table_assets:
                result.fetch_state_updates["tables"] = "succeeded"
                result.new_assets.extend(table_assets)
            else:
                result.fetch_state_updates["tables"] = "failed"

    if "pdf" in include:
        payload = unpaywall(doi)
        legacy_pdf_url = _as_str(snapshot.get("oa_pdf_url"))
        if payload is None:
            pdf_url = legacy_pdf_url
        else:
            pdf_url = _best_oa_pdf_url(payload)
            result.oa_probe_succeeded = True
            result.oa_status = "open" if bool(payload.get("is_oa")) else "closed"
            result.oa_pdf_url = pdf_url
        if not pdf_url:
            result.paths_updates["pdf"] = None
            result.fetch_state_updates["pdf"] = "skipped"
        else:
            content = apis.get_bytes(pdf_url)
            if content:
                pdf_path = pdfs_dir / f"{project.safe_doi(doi)}.pdf"
                pdf_path.write_bytes(content)
                result.paths_updates["pdf"] = project.to_rel(pdf_path)
                result.fetch_state_updates["pdf"] = "succeeded"
            else:
                result.paths_updates["pdf"] = None
                result.fetch_state_updates["pdf"] = "failed"

    return result


def apply_fetch_result(
    entry: refs.Entry,
    result: FetchResult,
    assets_dir: pathlib.Path,
) -> None:
    fetch_state = entry["fetch_state"]
    for name, status in result.fetch_state_updates.items():
        _set_fetch_status(fetch_state, name, status)

    paths = entry["paths"]
    for name, path_value in result.paths_updates.items():
        if name == "abstract":
            paths["abstract"] = path_value
        elif name == "pdf":
            paths["pdf"] = path_value
        elif name == "pdf_text":
            paths["pdf_text"] = path_value

    if result.oa_probe_succeeded:
        if result.oa_status is not None:
            entry["oa_status"] = result.oa_status
        entry["oa_pdf_url"] = result.oa_pdf_url
    if result.has_fulltext_xml is not None:
        entry["has_fulltext_xml"] = result.has_fulltext_xml
    if result.new_assets:
        _store_assets(entry, assets_dir, result.new_assets)


def process(
    entry: refs.Entry,
    base_tmp: pathlib.Path,
    include: set[str],
    max_figures: int,
    max_tables: int,
) -> None:
    snapshot = _snapshot_for_fetch(entry)
    result = fetch_compute(snapshot, base_tmp, include, max_figures, max_tables)
    apply_fetch_result(entry, result, base_tmp / "assets")


def _parse_include(raw: str) -> set[str]:
    include = {part.strip() for part in raw.split(",") if part.strip()}
    invalid = sorted(include - VALID_INCLUDE)
    if invalid:
        joined = ", ".join(invalid)
        raise ValueError(f"invalid --include values: {joined}")
    return include


def _select_entries(
    store: refs.Store,
    target_doi: str | None,
    include: set[str],
    retry_failed: bool,
) -> tuple[list[tuple[str, refs.Entry]], bool]:
    """Pick entries that need fetching. Returns (eligible, matched_target).

    Mutates each eligible entry's fetch_state to 'pending' for retry / target
    paths so that fetch_compute will re-fetch them. Skips verified=False
    and retracted entries.
    """
    eligible: list[tuple[str, refs.Entry]] = []
    matched_target = target_doi is None
    for doi, raw_entry in store["entries"].items():
        entry = refs._ensure_defaults(raw_entry)
        store["entries"][doi] = entry

        if target_doi and doi != target_doi:
            continue
        matched_target = True

        if entry.get("verification_status") != "verified" or entry.get(
            "retracted", False
        ):
            continue

        fetch_state = entry["fetch_state"]
        if retry_failed:
            for name in include:
                if _get_fetch_status(fetch_state, name) == "failed":
                    _set_fetch_status(fetch_state, name, "pending")

        if target_doi:
            for name in include:
                _set_fetch_status(fetch_state, name, "pending")
        else:
            pending = [
                name
                for name in include
                if _get_fetch_status(fetch_state, name) == "pending"
            ]
            if not pending:
                continue

        eligible.append((doi, entry))
    return eligible, matched_target


def _fetch_serial(
    eligible: list[tuple[str, refs.Entry]],
    base_tmp: pathlib.Path,
    include: set[str],
    max_figures: int,
    max_tables: int,
) -> int:
    for _, entry in eligible:
        process(entry, base_tmp, include, max_figures, max_tables)
    return len(eligible)


def _fetch_parallel(
    eligible: list[tuple[str, refs.Entry]],
    base_tmp: pathlib.Path,
    include: set[str],
    max_figures: int,
    max_tables: int,
    parallel: int,
) -> int:
    """Run fetch_compute() across a thread pool; apply results serially in the
    main thread (manifest writes are not thread-safe — see
    test_manifest_write_serialized_by_main_thread)."""
    assets_dir = base_tmp / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    processed = 0
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures: dict[object, tuple[str, refs.Entry]] = {}
        for doi, entry in eligible:
            snapshot = _snapshot_for_fetch(entry)
            future = pool.submit(
                fetch_compute,
                snapshot,
                base_tmp,
                include,
                max_figures,
                max_tables,
            )
            futures[future] = (doi, entry)

        for future in as_completed(list(futures)):
            doi, entry = futures[future]
            try:
                result = cast(FetchResult, future.result())
            except Exception as exc:
                print(f"[ERROR] fetch {doi}: {exc}")
                continue
            apply_fetch_result(entry, result, assets_dir)
            processed += 1
    return processed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("topic_dir", help="Path to a topic directory under reviews/")
    parser.add_argument("--doi")
    parser.add_argument("--include", default="abstract,fulltext,figures,tables,pdf")
    parser.add_argument("--max-figures", type=int, default=5)
    parser.add_argument("--max-tables", type=int, default=5)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        help=(
            "Parallel workers for fetch (default 1 = serial). 3 aligns with "
            "CrossRef polite-pool concurrency; higher risks rate-limit hits."
        ),
    )
    args = parser.parse_args()

    topic_dir = pathlib.Path(args.topic_dir)
    store = refs.load(topic_dir)
    if store is None:
        print(f"[ERROR] missing references store: {topic_dir}")
        raise SystemExit(1)

    try:
        include = _parse_include(args.include)
    except ValueError as exc:
        print(f"[ERROR] {exc}")
        raise SystemExit(2)

    target_doi = args.doi.lower() if args.doi else None
    topic_tmp = project.topic_tmp(topic_dir.name)
    topic_tmp.mkdir(parents=True, exist_ok=True)

    eligible, matched_target = _select_entries(
        store, target_doi, include, args.retry_failed
    )
    if not matched_target:
        print(f"[ERROR] DOI not found in references store: {target_doi}")
        raise SystemExit(1)

    with testflight.timer(
        "fetch",
        "main",
        topic_dir=topic_dir,
        parallel=args.parallel,
        eligible=len(eligible),
        include=",".join(sorted(include)),
    ) as detail:
        if args.parallel > 1 and len(eligible) > 1:
            processed = _fetch_parallel(
                eligible,
                topic_tmp,
                include,
                args.max_figures,
                args.max_tables,
                args.parallel,
            )
        else:
            processed = _fetch_serial(
                eligible, topic_tmp, include, args.max_figures, args.max_tables
            )
        detail["processed"] = processed

    refs.save(topic_dir, store)
    print(f"fetched {processed} entries")


if __name__ == "__main__":
    main()
