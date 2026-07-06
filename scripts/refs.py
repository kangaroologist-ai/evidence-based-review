from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import re
import tempfile
from datetime import date, datetime, timezone
from collections.abc import Callable, Iterator
from typing import Literal, TypedDict, cast

from lib import liveness, project

FetchStatus = Literal["pending", "succeeded", "failed", "skipped"]
VerificationStatus = Literal["pending", "verified", "failed"]
GapStatus = Literal["pending", "resolved", "insufficient"]
GapType = Literal[
    "decision",
    "descriptive",
    "mechanism",
    "comparison",
    "methodology",
    "safety",
    "diagnostic",
]
_VALID_GAP_TYPES = frozenset(
    {
        "decision",
        "descriptive",
        "mechanism",
        "comparison",
        "methodology",
        "safety",
        "diagnostic",
    }
)
StudyType = Literal[
    "rct",
    "meta",
    "cohort",
    "case_control",
    "review",
    "mechanism",
    "guideline",
    "other",
]

_META_FILENAME = "references_store.json"
_ENTRIES_DIRNAME = "references"
_LOCK_SUFFIX = ".lock"
_CITATION_KEY_INVALID = re.compile(r"[^a-z0-9]")


class Verification(TypedDict, total=False):
    provider: str
    partial: bool
    warnings: list[str]
    corrections: list[str]
    retraction_watch_checked: bool
    checked_at: str
    severity: str
    force_mismatch: dict[str, object]


class FetchState(TypedDict, total=False):
    abstract: FetchStatus
    fulltext_xml: FetchStatus
    figures: FetchStatus
    tables: FetchStatus
    pdf: FetchStatus
    pdf_text: FetchStatus
    # plan v3 §3.4 C19: OCR rung (MinerU) for scanned/image-only PDFs whose
    # born-digital text extraction (pdf_text via PyMuPDF) came back empty.
    ocr: FetchStatus


class Paths(TypedDict, total=False):
    abstract: str | None
    pdf: str | None
    pdf_text: str | None
    ocr: str | None


class JournalSignals(TypedDict, total=False):
    in_doaj: bool
    h_index: int
    works_count: int
    source_display_name: str


class Gap(TypedDict, total=False):
    description: str
    status: GapStatus
    created_round: int
    resolved_round: int | None
    # Classification + structured sub-fields (subagent-derived gap_type
    # taxonomy from 26-review survey). Optional for backwards compatibility;
    # new gaps declared via verify.py --declare-gap should fill these.
    gap_type: GapType | None
    secondary_type: GapType | None
    fields: dict[str, object]  # P-I-C-O / Phenomenon-Mechanism / etc. by type
    depends_on: list[str]      # gap ids this one depends on (topological order)
    subgap_of: str | None      # parent gap id (e.g. gap-3 for gap-3.1)
    query: str | None              # spec N2: English/optimized search query for the round
    relevance_terms: str | None    # spec N2: terms for the C2 relevance gate / genealogy


class Entry(TypedDict, total=False):
    doi: str
    title: str
    authors: list[str]
    year: int
    journal: str
    issn: list[str]
    source: str
    added_round: int
    overlap: int
    gap: str | None
    citation_key: str
    study_type: StudyType
    journal_signals: JournalSignals
    verification_status: VerificationStatus
    verification: Verification
    retracted: bool
    retraction_notes: list[object]
    oa_status: str
    oa_pdf_url: str | None
    has_fulltext_xml: bool
    superseded_by: str | None
    supersedes: list[str]
    excluded_reason: str | None
    # plan v3 §2 C6 (A1): on-topic, real evidence the analyst deliberately keeps
    # in the store but does not expect to cite (not the strongest support for any
    # proposition). Stays verified + citable, but is excluded from the cited-ratio
    # denominator so breadth doesn't trigger a FAIL that forces deleting read
    # literature. Distinct from excluded_reason (cross-domain noise → dropped).
    keep_uncited: bool
    fetch_state: FetchState
    paths: Paths
    asset_ids: list[str]


class StoreMeta(TypedDict, total=False):
    topic: str
    created: str
    rounds: int
    gaps: dict[str, Gap]
    # Domain selects which patches/<domain>.md overrides apply (term_check
    # evidence base, default search sources, lint thresholds). Defaults to
    # "health" — the implicit baseline declared in CLAUDE.md.
    domain: str
    # Rounds declared as targeted addenda to an already-saturated topic
    # (verify --declare-gap --addendum). Consumed by term_check to relax
    # round-bookkeeping. MUST be preserved by _store_meta across save().
    addendum_rounds: list[int]


class Store(StoreMeta, total=False):
    entries: dict[str, Entry]


class BlocklistRecord(TypedDict):
    reason: str
    notes: list[object]
    removed_at: str


class Blocklist(TypedDict):
    retracted: dict[str, BlocklistRecord]


def _default_fetch_state() -> FetchState:
    return {
        "abstract": "pending",
        "fulltext_xml": "pending",
        "figures": "pending",
        "tables": "pending",
        "pdf": "pending",
        "pdf_text": "pending",
    }


def _default_paths() -> Paths:
    return {"abstract": None, "pdf": None, "pdf_text": None, "ocr": None}


GroundingLevel = Literal["fulltext", "pdf_text", "ocr", "abstract", "title_only"]


def grounding(entry: "Entry") -> GroundingLevel:
    """Best readable-content level available for ``entry`` (plan v3 §3.4 C16).

    Derived from fetch_state/paths rather than stored, so it can never drift.
    Precedence by content richness: fulltext > pdf_text (born-digital) > ocr >
    abstract > title_only. ``title_only`` means we have only metadata (title /
    authors / year) — the analyst and faithfulness have no text to ground on.
    """
    fetch_state = entry.get("fetch_state") or {}
    paths = entry.get("paths") or {}
    # INVARIANT: a content-bearing level MUST be backed by readable text we actually
    # persisted, or grounding() and faithfulness._load_source_text() disagree — the
    # latter returns '' and every citing claim is marked `insufficient` (a hard
    # write_gate FAIL) while grounding() still advertises "fulltext". Fulltext XML is
    # never saved to its own path; its readable form is the enriched abstract card
    # (paths['abstract'], carrying intro+conclusion). So fulltext_xml=='succeeded' /
    # has_fulltext_xml only attest the XML was AVAILABLE at fetch time — when EuPMC's
    # abstractText and CrossRef's abstract are both empty the card is skipped and the
    # content dropped, leaving the flag set with no persisted text. Gate every content
    # level on a real readable path (pdf_text / ocr / abstract — the same paths
    # _load_source_text reads); the honest worst case is "title_only".
    readable = paths.get("pdf_text") or paths.get("ocr") or paths.get("abstract")
    if (fetch_state.get("fulltext_xml") == "succeeded" or entry.get("has_fulltext_xml")) and readable:
        return "fulltext"
    if paths.get("pdf_text"):
        return "pdf_text"
    if (fetch_state.get("ocr") == "succeeded" or paths.get("ocr")) and (
        paths.get("ocr") or paths.get("abstract")
    ):
        return "ocr"
    if paths.get("abstract"):
        return "abstract"
    return "title_only"


def is_grounded(entry: "Entry") -> bool:
    """True iff the entry has any readable content (not title-only)."""
    return grounding(entry) != "title_only"


_PREPRINT_JOURNAL_RE = re.compile(
    r"biorxiv|medrxiv|arxiv|research\s*square|preprint|ssrn|chemrxiv|psyarxiv|osf",
    re.IGNORECASE,
)


def metadata_flags(entry: "Entry") -> dict[str, bool]:
    """Normalized publication-integrity flags (plan v3.1 M1 / spec §0.6 元数据闸).

    Derived deterministically from already-stored fields — ``retracted`` plus
    ``verification.corrections`` (CrossRef update-to: correction / EoC, persisted
    by verify) plus the journal name (preprint servers). No network. These are the
    spec status fields retraction/erratum/expression_of_concern/preprint_status;
    duplicate_cluster_id is study-level dedup (spec §7, out of scope)."""
    verification = entry.get("verification") or {}
    corrections = [str(c).lower() for c in (verification.get("corrections") or [])]
    journal = str(entry.get("journal") or "")
    return {
        "retracted": bool(entry.get("retracted")),
        "expression_of_concern": any("concern" in c for c in corrections),
        "erratum": any(c in ("correction", "erratum", "corrigendum") for c in corrections),
        "preprint": bool(_PREPRINT_JOURNAL_RE.search(journal)),
    }


def _title_cluster_id(entry: "Entry") -> str | None:
    """A deterministic cluster id from the normalized title — entries with the same
    (case/space/punct-normalized) title share an id. A BASIC duplicate signal, not
    full study-level dedup (that is spec §7); but no longer a fixed None."""
    import hashlib

    title = str(entry.get("title") or "").lower()
    norm = re.sub(r"[^a-z0-9一-鿿]", "", title)
    if len(norm) < 6:
        return None
    return "t" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]


def metadata_status_fields(entry: "Entry") -> dict[str, object]:
    """The spec §2 verify --add OUT *persisted* status fields (plan v3.1 M1), built
    from metadata_flags. ``duplicate_cluster_id`` is a normalized-title cluster (a
    basic dup signal; full study-level dedup is spec §7)."""
    flags = metadata_flags(entry)
    return {
        "retraction_status": "retracted" if flags["retracted"] else "none",
        "erratum_status": "corrected" if flags["erratum"] else "none",
        "expression_of_concern": flags["expression_of_concern"],
        "preprint_status": "preprint" if flags["preprint"] else "published",
        "duplicate_cluster_id": _title_cluster_id(entry),
    }


# The grounding escalation ladder (plan v3 §3.4 C15): cheapest rung first.
_GROUNDING_LADDER: tuple[tuple[str, str], ...] = (
    ("abstract", "abstract"),       # fetch --include abstract (EuPMC→CrossRef)
    ("fulltext_xml", "fulltext"),   # fetch --include fulltext (JATS)
    ("pdf", "pdf"),                 # fetch --include pdf → extract_pdf (born-digital)
    ("ocr", "ocr"),                 # MinerU OCR rung (C19), for scanned PDFs
)


def grounding_ladder_next(entry: "Entry") -> str | None:
    """For an UNgrounded entry, return the next fetch rung to attempt
    (``abstract`` → ``fulltext`` → ``pdf`` → ``ocr``), or None when the entry is
    already grounded OR every rung has been tried (the irreducible title-only
    residual that must be accepted, not looped on — plan v3 §3.4 C15/C18).

    "Tried" = its fetch_state is succeeded/failed/skipped; a ``pending`` rung is
    the next thing to attempt. Callers escalate LAZILY (only for entries a gap
    actually needs — C10), never the whole store, to keep OCR/PDF cost bounded.
    """
    if is_grounded(entry):
        return None
    fetch_state = entry.get("fetch_state") or {}
    tried = {"succeeded", "failed", "skipped"}
    for state_key, include_name in _GROUNDING_LADDER:
        if fetch_state.get(state_key) not in tried:
            return include_name
    return None


def _default_verification() -> Verification:
    return {
        "provider": "pending",
        "partial": False,
        "warnings": [],
        "corrections": [],
        "retraction_watch_checked": False,
    }


def _default_journal_signals() -> JournalSignals:
    return {
        "in_doaj": False,
        "h_index": 0,
        "works_count": 0,
        "source_display_name": "",
    }


def new_store(topic: str, domain: str = "health") -> Store:
    return {
        "topic": topic,
        "created": date.today().isoformat(),
        "rounds": 0,
        "gaps": {},
        "entries": {},
        "domain": domain,
    }


def _normalize_gap_status(value: object) -> GapStatus:
    if isinstance(value, str) and value in {"pending", "resolved", "insufficient"}:
        return cast(GapStatus, value)
    return "pending"


def _normalize_gap_type(value: object) -> GapType | None:
    if isinstance(value, str) and value in _VALID_GAP_TYPES:
        return cast(GapType, value)
    return None


def _normalize_gap(value: object) -> Gap:
    if not isinstance(value, dict):
        return {
            "description": "",
            "status": "pending",
            "created_round": 0,
            "resolved_round": None,
            "gap_type": None,
            "secondary_type": None,
            "fields": {},
            "depends_on": [],
            "subgap_of": None,
            "query": None,
            "relevance_terms": None,
        }

    description = value.get("description")
    created_round = value.get("created_round")
    resolved_round = value.get("resolved_round")
    fields_raw = value.get("fields")
    depends_on_raw = value.get("depends_on")
    subgap_of_raw = value.get("subgap_of")
    query_raw = value.get("query")
    relevance_raw = value.get("relevance_terms")
    return {
        "description": description if isinstance(description, str) else "",
        "status": _normalize_gap_status(value.get("status")),
        "created_round": created_round if isinstance(created_round, int) else 0,
        "resolved_round": resolved_round if isinstance(resolved_round, int) else None,
        "gap_type": _normalize_gap_type(value.get("gap_type")),
        "secondary_type": _normalize_gap_type(value.get("secondary_type")),
        "fields": fields_raw if isinstance(fields_raw, dict) else {},
        "depends_on": [g for g in (depends_on_raw or []) if isinstance(g, str)],
        "subgap_of": subgap_of_raw if isinstance(subgap_of_raw, str) else None,
        # spec N2: persisted per-gap search query + relevance terms (R-testflight)
        "query": query_raw if isinstance(query_raw, str) and query_raw.strip() else None,
        "relevance_terms": relevance_raw if isinstance(relevance_raw, str) and relevance_raw.strip() else None,
    }


def _normalize_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _normalize_object_list(value: object) -> list[object]:
    if not isinstance(value, list):
        return []
    return list(value)


def _normalize_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _normalize_int(value: object, default: int = 0) -> int:
    return value if isinstance(value, int) else default


def _normalize_verification_status(value: object) -> VerificationStatus:
    if isinstance(value, str) and value in {"pending", "verified", "failed"}:
        return cast(VerificationStatus, value)
    return "pending"


def _normalize_study_type(value: object) -> StudyType:
    if isinstance(value, str) and value in {
        "rct",
        "meta",
        "cohort",
        "case_control",
        "review",
        "mechanism",
        "guideline",
        "other",
    }:
        return cast(StudyType, value)
    return "other"


def _normalize_verification(entry: Entry) -> Verification:
    current = entry.get("verification")
    merged = _default_verification()
    if not isinstance(current, dict):
        return merged

    provider = current.get("provider")
    if isinstance(provider, str):
        merged["provider"] = provider

    partial = current.get("partial")
    if isinstance(partial, bool):
        merged["partial"] = partial

    warnings = current.get("warnings")
    if isinstance(warnings, list):
        merged["warnings"] = [item for item in warnings if isinstance(item, str)]

    corrections = current.get("corrections")
    if isinstance(corrections, list):
        merged["corrections"] = [item for item in corrections if isinstance(item, str)]

    retraction_watch_checked = current.get("retraction_watch_checked")
    if isinstance(retraction_watch_checked, bool):
        merged["retraction_watch_checked"] = retraction_watch_checked

    checked_at = current.get("checked_at")
    if isinstance(checked_at, str):
        merged["checked_at"] = checked_at

    severity = current.get("severity")
    if isinstance(severity, str):
        merged["severity"] = severity

    force_mismatch = current.get("force_mismatch")
    if isinstance(force_mismatch, dict):
        merged["force_mismatch"] = dict(force_mismatch)

    return merged


def _normalize_fetch_state(entry: Entry) -> FetchState:
    current = entry.get("fetch_state")
    merged = _default_fetch_state()
    if not isinstance(current, dict):
        return merged

    abstract = current.get("abstract")
    if isinstance(abstract, str) and abstract in {"pending", "succeeded", "failed", "skipped"}:
        merged["abstract"] = cast(FetchStatus, abstract)

    fulltext_xml = current.get("fulltext_xml")
    if isinstance(fulltext_xml, str) and fulltext_xml in {"pending", "succeeded", "failed", "skipped"}:
        merged["fulltext_xml"] = cast(FetchStatus, fulltext_xml)

    figures = current.get("figures")
    if isinstance(figures, str) and figures in {"pending", "succeeded", "failed", "skipped"}:
        merged["figures"] = cast(FetchStatus, figures)

    tables = current.get("tables")
    if isinstance(tables, str) and tables in {"pending", "succeeded", "failed", "skipped"}:
        merged["tables"] = cast(FetchStatus, tables)

    pdf = current.get("pdf")
    if isinstance(pdf, str) and pdf in {"pending", "succeeded", "failed", "skipped"}:
        merged["pdf"] = cast(FetchStatus, pdf)

    pdf_text = current.get("pdf_text")
    if isinstance(pdf_text, str) and pdf_text in {"pending", "succeeded", "failed", "skipped"}:
        merged["pdf_text"] = cast(FetchStatus, pdf_text)
    return merged


def _normalize_paths(entry: Entry) -> Paths:
    current = entry.get("paths")
    merged = _default_paths()
    if not isinstance(current, dict):
        return merged

    abstract = current.get("abstract")
    if abstract is None or isinstance(abstract, str):
        merged["abstract"] = abstract

    pdf = current.get("pdf")
    if pdf is None or isinstance(pdf, str):
        merged["pdf"] = pdf

    pdf_text = current.get("pdf_text")
    if pdf_text is None or isinstance(pdf_text, str):
        merged["pdf_text"] = pdf_text
    return merged


def _normalize_journal_signals(entry: Entry) -> JournalSignals:
    current = entry.get("journal_signals")
    merged = _default_journal_signals()
    if not isinstance(current, dict):
        return merged

    in_doaj = current.get("in_doaj")
    if isinstance(in_doaj, bool):
        merged["in_doaj"] = in_doaj

    h_index = current.get("h_index")
    if isinstance(h_index, int):
        merged["h_index"] = h_index

    works_count = current.get("works_count")
    if isinstance(works_count, int):
        merged["works_count"] = works_count

    source_display_name = current.get("source_display_name")
    if isinstance(source_display_name, str):
        merged["source_display_name"] = source_display_name

    return merged


def _citation_key(authors: list[str], year: int | None, taken: set[str]) -> str:
    raw_family = authors[0].split(",")[0].strip().lower() if authors else "anon"
    family = _CITATION_KEY_INVALID.sub("", raw_family) or "anon"
    if not family[:1].isalpha():
        family = "a" + family

    base = f"{family}{year if year is not None else 'na'}"
    if base not in taken:
        return base
    for suffix in "abcdefghij":
        candidate = f"{base}{suffix}"
        if candidate not in taken:
            return candidate
    return f"{base}x"


def _deterministic_citation_key(entry: Entry) -> str:
    authors = entry.get("authors", [])
    raw_family = authors[0].split(",")[0].strip().lower() if authors else "anon"
    family = _CITATION_KEY_INVALID.sub("", raw_family) or "anon"
    if not family[:1].isalpha():
        family = "a" + family

    year = entry.get("year")
    base = f"{family}{year if isinstance(year, int) else 'na'}"
    doi = _normalize_str(entry.get("doi")).lower()
    if not doi:
        return base
    digest = base64.b32encode(hashlib.sha1(doi.encode("utf-8")).digest()).decode("ascii")
    return f"{base}{digest[:4].lower()}"


def _ensure_defaults(entry: Entry) -> Entry:
    entry["doi"] = _normalize_str(entry.get("doi")).lower()
    entry["title"] = _normalize_str(entry.get("title"))
    entry["authors"] = _normalize_str_list(entry.get("authors"))
    year = entry.get("year")
    if isinstance(year, int):
        entry["year"] = year
    elif "year" in entry:
        del entry["year"]
    entry["journal"] = _normalize_str(entry.get("journal"))
    entry["issn"] = _normalize_str_list(entry.get("issn"))
    entry["source"] = _normalize_str(entry.get("source"))
    added_round = entry.get("added_round")
    if isinstance(added_round, int):
        entry["added_round"] = added_round
    elif "added_round" in entry:
        del entry["added_round"]
    overlap = entry.get("overlap")
    if isinstance(overlap, int):
        entry["overlap"] = overlap
    elif "overlap" in entry:
        del entry["overlap"]
    gap_value = entry.get("gap")
    entry["gap"] = gap_value if isinstance(gap_value, str) and gap_value else None
    citation_key = entry.get("citation_key")
    if isinstance(citation_key, str) and citation_key:
        entry["citation_key"] = citation_key
    elif "citation_key" in entry:
        del entry["citation_key"]
    entry["study_type"] = _normalize_study_type(entry.get("study_type"))
    entry["journal_signals"] = _normalize_journal_signals(entry)
    entry["verification_status"] = _normalize_verification_status(entry.get("verification_status"))
    entry["verification"] = _normalize_verification(entry)
    entry["retracted"] = bool(entry.get("retracted", False))
    entry["retraction_notes"] = _normalize_object_list(entry.get("retraction_notes"))
    entry["oa_status"] = _normalize_str(entry.get("oa_status"), "unknown")
    oa_pdf_url = entry.get("oa_pdf_url")
    entry["oa_pdf_url"] = oa_pdf_url if isinstance(oa_pdf_url, str) else None
    entry["has_fulltext_xml"] = bool(entry.get("has_fulltext_xml", False))
    superseded_by = entry.get("superseded_by")
    entry["superseded_by"] = superseded_by if isinstance(superseded_by, str) else None
    entry["supersedes"] = _normalize_str_list(entry.get("supersedes"))
    excluded_reason = entry.get("excluded_reason")
    entry["excluded_reason"] = (
        excluded_reason if isinstance(excluded_reason, str) and excluded_reason else None
    )
    entry["fetch_state"] = _normalize_fetch_state(entry)
    entry["paths"] = _normalize_paths(entry)
    entry["asset_ids"] = _normalize_str_list(entry.get("asset_ids"))
    return entry


def _ensure_meta_defaults(meta: StoreMeta, topic_name: str = "") -> StoreMeta:
    topic = meta.get("topic")
    meta["topic"] = topic if isinstance(topic, str) and topic else topic_name

    created = meta.get("created")
    meta["created"] = created if isinstance(created, str) else date.today().isoformat()

    rounds = meta.get("rounds")
    meta["rounds"] = rounds if isinstance(rounds, int) else 0

    raw_gaps = meta.get("gaps")
    gaps: dict[str, Gap] = {}
    if isinstance(raw_gaps, dict):
        for gap_id, gap_value in raw_gaps.items():
            if isinstance(gap_id, str):
                gaps[gap_id] = _normalize_gap(gap_value)
    meta["gaps"] = gaps

    # Domain field — defaults to "health" (CLAUDE.md baseline). Existing
    # stores without this field will silently inherit the default; new
    # stores get it set by bootstrap_topic.py.
    domain = meta.get("domain")
    meta["domain"] = domain if isinstance(domain, str) and domain else "health"
    return meta


def _ensure_store_defaults(store: Store, topic_name: str = "") -> Store:
    meta = _ensure_meta_defaults(cast(StoreMeta, store), topic_name)
    store["topic"] = meta["topic"]
    store["created"] = meta["created"]
    store["rounds"] = meta["rounds"]
    store["gaps"] = meta["gaps"]
    store["domain"] = meta.get("domain", "health")

    raw_entries = store.get("entries")
    entries: dict[str, Entry] = {}
    if isinstance(raw_entries, dict):
        for doi, entry_value in raw_entries.items():
            if isinstance(doi, str) and isinstance(entry_value, dict):
                entry = cast(Entry, dict(entry_value))
                entry["doi"] = doi
                normalized = _ensure_defaults(entry)
                entries[normalized["doi"]] = normalized
    store["entries"] = entries
    return store


def _meta_path(topic_dir: pathlib.Path) -> pathlib.Path:
    return topic_dir / _META_FILENAME


def _entries_dir(topic_dir: pathlib.Path) -> pathlib.Path:
    return topic_dir / _ENTRIES_DIRNAME


def _entry_path(topic_dir: pathlib.Path, doi: str) -> pathlib.Path:
    return _entries_dir(topic_dir) / f"{project.safe_doi(doi)}.json"


def _lock_path(file_path: pathlib.Path) -> pathlib.Path:
    return file_path.parent / f"{file_path.name}{_LOCK_SUFFIX}"


def _store_meta(store: Store) -> StoreMeta:
    ensured = _ensure_store_defaults(cast(Store, dict(store)))
    meta: StoreMeta = {
        "topic": ensured["topic"],
        "created": ensured["created"],
        "rounds": ensured["rounds"],
        "gaps": ensured["gaps"],
        "domain": ensured.get("domain", "health"),
    }
    # addendum_rounds is outside the base whitelist above; without this
    # passthrough any save() silently drops it and breaks term_check's
    # addendum handling (declare_gap writes it, term_check.py reads it).
    addendum_rounds = ensured.get("addendum_rounds")
    if addendum_rounds:
        meta["addendum_rounds"] = list(addendum_rounds)
    # round_meta (F6, testflight #2): per-round genealogy cap-hit flags so
    # term_check can reject cap-throttled rounds from the saturation window — a
    # tight --max-add must not fake saturation. Same passthrough rationale as
    # addendum_rounds: outside the base whitelist above, silently dropped without it.
    round_meta = ensured.get("round_meta")
    if round_meta:
        meta["round_meta"] = dict(round_meta)
    return meta


def _read_json_file(file_path: pathlib.Path) -> object:
    return json.loads(file_path.read_text(encoding="utf-8"))


def _write_json_file(file_path: pathlib.Path, payload: object) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        dir=file_path.parent,
        delete=False,
        encoding="utf-8",
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        temp_path = pathlib.Path(handle.name)
    os.replace(temp_path, file_path)


@contextlib.contextmanager
def _exclusive_lock(lock_path: pathlib.Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_meta(topic_dir: pathlib.Path) -> StoreMeta | None:
    meta_file = _meta_path(topic_dir)
    if not meta_file.exists():
        return None
    return _ensure_meta_defaults(cast(StoreMeta, _read_json_file(meta_file)), topic_dir.name)


def _write_meta(topic_dir: pathlib.Path, meta: StoreMeta) -> None:
    _write_json_file(_meta_path(topic_dir), _ensure_meta_defaults(meta, topic_dir.name))


def _load_entry_file(file_path: pathlib.Path) -> Entry | None:
    if not file_path.exists():
        return None
    raw = _read_json_file(file_path)
    if not isinstance(raw, dict):
        return None
    return _ensure_defaults(cast(Entry, dict(raw)))


def load(topic_dir: str | pathlib.Path) -> Store | None:
    topic_path = pathlib.Path(topic_dir)
    meta_file = _meta_path(topic_path)
    entries_directory = _entries_dir(topic_path)
    if not (meta_file.exists() or entries_directory.is_dir()):
        return None

    meta = _read_meta(topic_path)
    if meta is None:
        meta = _ensure_meta_defaults({"topic": topic_path.name}, topic_path.name)
    store = cast(Store, dict(meta))
    entries: dict[str, Entry] = {}
    if entries_directory.exists():
        for entry_file in sorted(entries_directory.glob("*.json")):
            entry = _load_entry_file(entry_file)
            if entry is None:
                continue
            entries[entry["doi"]] = entry
    store["entries"] = entries
    return _ensure_store_defaults(store, topic_path.name)


def latest_round(store: Store) -> int:
    """The highest round number the store has reached.

    Derived as the max of three signals so that no single stale counter
    can under-report progress:

    - ``store['rounds']`` — a hint maintained only by genealogy.py; it lags
      whenever a round added evidence via verify/search but not genealogy.
    - the max ``added_round`` across all entries.
    - the max ``created_round`` across all declared gaps.

    This is the single source of truth for "which round are we on". Both
    term_check.py and the Stop hook (scripts/review_workflow_check.sh) must
    use it; reading raw ``store['rounds']`` instead silently under-counts
    rounds and lets per-round gates (e.g. genealogy-per-round) skip rounds.
    """
    return max(
        store.get("rounds", 0),
        max(
            (
                entry.get("added_round", 0)
                for entry in store["entries"].values()
                if isinstance(entry.get("added_round", 0), int)
            ),
            default=0,
        ),
        max(
            (
                gap.get("created_round", 0)
                for gap in store.get("gaps", {}).values()
                if isinstance(gap.get("created_round", 0), int)
            ),
            default=0,
        ),
    )


def save(topic_dir: str | pathlib.Path, data: Store) -> None:
    # C12: a daemon-routed tool whose client vanished mid-run must NOT commit a
    # half-applied store. ensure_alive() raises ClientGone (caught by the daemon
    # dispatcher → exit 2, no write) before any file is touched. Outside the daemon
    # no probe is registered → this is a no-op.
    liveness.ensure_alive()
    topic_path = pathlib.Path(topic_dir)
    store = _ensure_store_defaults(cast(Store, dict(data)), topic_path.name)
    data["topic"] = store["topic"]
    data["created"] = store["created"]
    data["rounds"] = store["rounds"]
    data["gaps"] = store["gaps"]
    data["entries"] = store["entries"]

    _write_meta(topic_path, _store_meta(store))
    _entries_dir(topic_path).mkdir(parents=True, exist_ok=True)
    for doi, raw_entry in store["entries"].items():
        entry = _ensure_defaults(cast(Entry, dict(raw_entry)))
        entry["doi"] = doi.lower()
        if "citation_key" not in entry:
            taken = {
                value["citation_key"]
                for value in store["entries"].values()
                if isinstance(value.get("citation_key"), str) and value["doi"] != entry["doi"]
            }
            entry["citation_key"] = _citation_key(entry.get("authors", []), entry.get("year"), taken)
        store["entries"][entry["doi"]] = entry
        _write_json_file(_entry_path(topic_path, entry["doi"]), entry)


def get_entry(topic_dir: str | pathlib.Path, doi: str) -> Entry | None:
    normalized = doi.lower()
    topic_path = pathlib.Path(topic_dir)
    file_path = _entry_path(topic_path, normalized)
    entry = _load_entry_file(file_path)
    if entry is not None:
        return entry
    if _meta_path(topic_path).exists() or _entries_dir(topic_path).exists():
        return None
    store = load(topic_path)
    if store is None:
        return None
    current = store["entries"].get(normalized)
    return cast(Entry, dict(current)) if current is not None else None


def list_dois(topic_dir: str | pathlib.Path) -> list[str]:
    topic_path = pathlib.Path(topic_dir)
    if _meta_path(topic_path).exists() or _entries_dir(topic_path).exists():
        dois: list[str] = []
        for entry_file in sorted(_entries_dir(topic_path).glob("*.json")):
            entry = _load_entry_file(entry_file)
            if entry is not None:
                dois.append(entry["doi"])
        return dois
    store = load(topic_path)
    if store is None:
        return []
    return sorted(store["entries"])


def put_entry(topic_dir: str | pathlib.Path, entry: Entry) -> Entry:
    topic_path = pathlib.Path(topic_dir)
    incoming = _ensure_defaults(cast(Entry, dict(entry)))
    lock_path = _lock_path(_entry_path(topic_path, incoming["doi"]))
    with _exclusive_lock(lock_path):
        current = get_entry(topic_path, incoming["doi"])
        if current is None and "citation_key" not in incoming:
            incoming["citation_key"] = _deterministic_citation_key(incoming)
        temp_store = new_store(topic_path.name)
        if current is not None:
            temp_store["entries"][incoming["doi"]] = current
        upsert(temp_store, incoming)
        merged = temp_store["entries"][incoming["doi"]]
        _write_json_file(_entry_path(topic_path, incoming["doi"]), merged)
    return merged


def update_meta(topic_dir: str | pathlib.Path, mutator: Callable[[StoreMeta], None]) -> None:
    topic_path = pathlib.Path(topic_dir)
    meta_file = _meta_path(topic_path)
    with _exclusive_lock(_lock_path(meta_file)):
        meta = _read_meta(topic_path)
        if meta is None:
            store = load(topic_path)
            if store is not None:
                meta = _store_meta(store)
            else:
                meta = _ensure_meta_defaults({"topic": topic_path.name}, topic_path.name)
        ensured = _ensure_meta_defaults(meta, topic_path.name)
        mutator(ensured)
        _write_meta(topic_path, ensured)


def delete_entry(topic_dir: str | pathlib.Path, doi: str) -> None:
    file_path = _entry_path(pathlib.Path(topic_dir), doi.lower())
    if file_path.exists():
        file_path.unlink()


def upsert(data: Store, entry: Entry) -> bool:
    doi = entry["doi"].lower()
    entry["doi"] = doi
    current = data["entries"].get(doi)
    if current is not None:
        current = _ensure_defaults(current)
        incoming_status = _normalize_verification_status(entry.get("verification_status"))
        if current["verification_status"] == "verified" and incoming_status == "pending":
            entry = cast(Entry, dict(entry))
            if "verification_status" in entry:
                del entry["verification_status"]
            if "verification" in entry:
                del entry["verification"]
        if "gap" in entry and current.get("gap") is not None:
            # Preserve the gap a DOI was first attached to. Genealogy expansion
            # for gap-N pulls in candidates that may already exist under
            # gap-M (M != N); without this guard, the second wave overwrites
            # the original gap label and we lose the audit trail of which
            # gap actually triggered the seed. Use refs.set_gap() (or the
            # regap.py CLI) for deliberate re-tagging.
            del entry["gap"]
        if (
            "excluded_reason" in entry
            and entry["excluded_reason"] is None
            and current.get("excluded_reason") is not None
        ):
            del entry["excluded_reason"]
        if "added_round" in entry and isinstance(current.get("added_round"), int):
            # Preserve the round a DOI was FIRST added in. Re-verification via
            # genealogy/search chains (a later round expands gap-N and re-emits
            # an already-stored DOI as an ancestor/descendant) carries the
            # CURRENT round; without this guard current.update() below bumps
            # added_round forward, which silently inflates term_check's
            # latest-round saturation ratio with entries that are not actually
            # new this round (a saturation round can then read as not_ready).
            del entry["added_round"]
        if "source" in entry and current.get("source"):
            # Same rationale: keep the original provenance (seed / search /
            # genealogy_*) of the first time the DOI entered the store, rather
            # than overwriting it with the re-emitting chain's source.
            del entry["source"]
        current.update(entry)
        data["entries"][doi] = _ensure_defaults(current)
        return False

    normalized = _ensure_defaults(cast(Entry, dict(entry)))
    taken = {
        value["citation_key"]
        for value in data["entries"].values()
        if isinstance(value.get("citation_key"), str)
    }
    normalized["citation_key"] = normalized.get("citation_key") or _citation_key(
        normalized.get("authors", []),
        normalized.get("year"),
        taken,
    )
    data["entries"][doi] = normalized
    return True


def delete(data: Store, doi: str) -> None:
    data["entries"].pop(doi.lower(), None)


def declare_gap(
    store: StoreMeta,
    gap_id: str,
    description: str,
    round_number: int,
    *,
    gap_type: GapType | None = None,
    secondary_type: GapType | None = None,
    fields: dict[str, object] | None = None,
    depends_on: list[str] | None = None,
    subgap_of: str | None = None,
    query: str | None = None,
    relevance_terms: str | None = None,
    addendum: bool = False,
) -> bool:
    """Idempotent declare. If the gap already exists, update mutable
    fields (description / type / fields / deps / subgap) in place but
    keep the original created_round and status. Returns True iff the
    gap was newly created.

    ``addendum=True`` marks a gap that is a *targeted addition to an already
    saturated topic* (e.g. a follow-up safety question on a finished review):
    its round is recorded in ``store["addendum_rounds"]`` so term_check can
    (a) not force an extra consolidation round and (b) not count this round
    against the lifetime hard cap. The per-gap evidence floors still apply —
    addendum only relaxes the round-bookkeeping, never the evidence bar."""
    gaps = store.setdefault("gaps", {})
    existing = gaps.get(gap_id)
    normalized_deps = [g for g in (depends_on or []) if isinstance(g, str) and g != gap_id]
    normalized_fields = dict(fields) if isinstance(fields, dict) else {}
    if existing is not None:
        existing["description"] = description
        if gap_type is not None:
            existing["gap_type"] = gap_type
        if secondary_type is not None:
            existing["secondary_type"] = secondary_type
        if fields is not None:
            existing["fields"] = normalized_fields
        if depends_on is not None:
            existing["depends_on"] = normalized_deps
        if subgap_of is not None:
            existing["subgap_of"] = subgap_of if subgap_of else None
        # spec N2 schema: a per-gap search `query` (English/optimized) + `relevance_terms` for the
        # C2 gate / genealogy. Persisted (not just transient workflow args) so a resume / fallback
        # runner doesn't lose them and fall back to the CJK description as the search query.
        if query is not None:
            existing["query"] = query.strip() or None
        if relevance_terms is not None:
            existing["relevance_terms"] = relevance_terms.strip() or None
        if addendum:
            existing["addendum"] = True
            rounds = store.setdefault("addendum_rounds", [])
            if round_number not in rounds:
                rounds.append(round_number)
        return False
    gaps[gap_id] = {
        "description": description,
        "status": "pending",
        "created_round": round_number,
        "resolved_round": None,
        "gap_type": gap_type,
        "secondary_type": secondary_type,
        "fields": normalized_fields,
        "depends_on": normalized_deps,
        "subgap_of": subgap_of if subgap_of else None,
        "query": (query.strip() or None) if query else None,
        "relevance_terms": (relevance_terms.strip() or None) if relevance_terms else None,
    }
    if addendum:
        gaps[gap_id]["addendum"] = True
        rounds = store.setdefault("addendum_rounds", [])
        if round_number not in rounds:
            rounds.append(round_number)
    return True


def resolve_gap(store: StoreMeta, gap_id: str, round_number: int) -> None:
    gap = store.get("gaps", {}).get(gap_id)
    if gap is None:
        raise KeyError(f"gap not declared: {gap_id}")
    gap["status"] = "resolved"
    gap["resolved_round"] = round_number


def mark_gap_insufficient(store: StoreMeta, gap_id: str, round_number: int) -> None:
    gap = store.get("gaps", {}).get(gap_id)
    if gap is None:
        raise KeyError(f"gap not declared: {gap_id}")
    gap["status"] = "insufficient"
    gap["resolved_round"] = round_number


def exclude_entry(store: Store, doi: str, reason: str) -> None:
    entry = store["entries"].get(doi.lower())
    if entry is None:
        raise KeyError(f"doi not in store: {doi}")
    entry["excluded_reason"] = reason


def set_keep_uncited(store: Store, doi: str, value: bool = True) -> None:
    """Mark/unmark an entry as keep_uncited (plan v3 C6). Raises if absent."""
    entry = store["entries"].get(doi.lower())
    if entry is None:
        raise KeyError(f"doi not in store: {doi}")
    if value:
        entry["keep_uncited"] = True
    else:
        entry.pop("keep_uncited", None)


def resolve_citation_key(store: Store, key: str) -> str | None:
    """Return the DOI of the entry whose citation_key == ``key``, or None."""
    for doi, entry in store["entries"].items():
        if entry.get("citation_key") == key:
            return doi
    return None


def include_entry(store: Store, doi: str) -> None:
    entry = store["entries"].get(doi.lower())
    if entry is not None:
        entry["excluded_reason"] = None


def is_excluded(store: Store, doi: str) -> bool:
    """True iff `doi` is present in the store AND carries an exclusion reason.

    The exclusion denylist is derived directly from the per-entry
    ``excluded_reason`` field rather than a separate ``excluded_dois`` set —
    one source of truth, no redundant field to keep in sync. Lookup is O(1)
    (a dict get + truthiness check) and DOI is normalized the same way the
    store keys are (``strip().lower()``), so callers can pass a raw DOI
    straight off a search hit or OpenAlex work.

    Add-paths (search.py --auto-add, genealogy.py candidate apply, verify.py
    --add) consult this so a DOI the author deliberately excluded in an
    earlier round stays excluded — it is no longer silently re-added (which
    used to clear the excluded flag via upsert and force a re-prune loop).
    A DOI that is not in the store yet returns False (nothing to exclude),
    which is the intended behavior: exclusion is sticky for *known* noise,
    not a global blocklist (that role belongs to ``is_blocked`` / retraction).
    """
    entry = store["entries"].get(doi.strip().lower())
    if entry is None:
        return False
    return bool(entry.get("excluded_reason"))


def set_gap(store: Store, doi: str, new_gap: str | None) -> None:
    """Explicitly set the gap label on an existing entry, bypassing upsert's
    first-seen protection. For deliberate re-tagging only."""
    entry = store["entries"].get(doi.lower())
    if entry is None:
        raise KeyError(f"doi not in store: {doi}")
    entry["gap"] = new_gap if new_gap else None


def set_gap_on_disk(
    topic_dir: str | pathlib.Path,
    doi: str,
    new_gap: str | None,
) -> Entry:
    """Re-tag an entry's gap label directly on disk (per-entry file write).
    Returns the updated entry."""
    topic_path = pathlib.Path(topic_dir)
    file_path = _entry_path(topic_path, doi.lower())
    with _exclusive_lock(_lock_path(file_path)):
        entry = _load_entry_file(file_path)
        if entry is None:
            raise KeyError(f"doi not in store: {doi}")
        entry["gap"] = new_gap if new_gap else None
        _write_json_file(file_path, entry)
    return entry


def clear_exclusion_on_disk(topic_dir: str | pathlib.Path, doi: str) -> Entry:
    """Clear an entry's ``excluded_reason`` directly on disk (per-entry file
    write). Used by verify.py --readd to resurrect a deliberately-excluded
    DOI. Done as a targeted disk write *before* the re-add because upsert's
    merge preserves an existing excluded_reason (it drops an incoming None),
    so clearing has to bypass the merge path. Returns the updated entry."""
    topic_path = pathlib.Path(topic_dir)
    file_path = _entry_path(topic_path, doi.lower())
    with _exclusive_lock(_lock_path(file_path)):
        entry = _load_entry_file(file_path)
        if entry is None:
            raise KeyError(f"doi not in store: {doi}")
        entry["excluded_reason"] = None
        _write_json_file(file_path, entry)
    return entry


def load_blocklist() -> Blocklist:
    file_path = project.blocklist_path()
    if not file_path.exists():
        return {"retracted": {}}
    return cast(Blocklist, json.loads(file_path.read_text(encoding="utf-8")))


def add_to_blocklist(doi: str, reason: str, notes: list[object] | None = None) -> None:
    blocklist = load_blocklist()
    blocklist["retracted"][doi.lower()] = {
        "reason": reason,
        "notes": notes or [],
        "removed_at": datetime.now(timezone.utc).isoformat(),
    }
    file_path = project.blocklist_path()
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        json.dumps(blocklist, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def is_blocked(blocklist: Blocklist, doi: str) -> bool:
    return doi.lower() in blocklist.get("retracted", {})
