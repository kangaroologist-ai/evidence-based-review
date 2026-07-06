from __future__ import annotations

from typing import cast

from . import apis
import refs

OPENALEX_BASE = "https://api.openalex.org"


def _as_dict(value: object) -> dict[str, object] | None:
    return value if isinstance(value, dict) else None


def _as_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _openalex_work(doi: str) -> dict[str, object] | None:
    return apis.get_json(f"{OPENALEX_BASE}/works/doi:{doi}")


def _openalex_source(source_id: str) -> dict[str, object] | None:
    short_id = source_id.rsplit("/", 1)[-1]
    return apis.get_json(f"{OPENALEX_BASE}/sources/{short_id}")


def _has_signals_populated(signals: refs.JournalSignals | None) -> bool:
    if not signals:
        return False
    return (
        bool(signals.get("source_display_name"))
        or signals.get("h_index", 0) > 0
        or signals.get("works_count", 0) > 0
    )


def fetch_journal_signals(doi: str) -> refs.JournalSignals:
    oa_work = _openalex_work(doi)
    primary_location = _as_dict((oa_work or {}).get("primary_location"))
    source = _as_dict((primary_location or {}).get("source"))
    if source is None:
        return refs._default_journal_signals()

    in_doaj = bool(source.get("is_in_doaj", False))
    display_name = _as_str(source.get("display_name")) or ""
    h_index = _as_int(source.get("h_index")) or 0
    works_count = _as_int(source.get("works_count")) or 0
    source_id = _as_str(source.get("id"))
    if source_id and (h_index == 0 or works_count == 0):
        full = _openalex_source(source_id) or {}
        summary = _as_dict(full.get("summary_stats")) or {}
        h_index = _as_int(summary.get("h_index")) or h_index
        works_count = _as_int(full.get("works_count")) or works_count

    return {
        "in_doaj": in_doaj,
        "h_index": h_index,
        "works_count": works_count,
        "source_display_name": display_name,
    }


def ensure_journal_signals(entry: refs.Entry) -> refs.JournalSignals:
    existing = cast(refs.JournalSignals, entry.get("journal_signals") or {})
    if _has_signals_populated(existing):
        return existing
    signals = fetch_journal_signals(entry["doi"])
    entry["journal_signals"] = signals
    return signals
