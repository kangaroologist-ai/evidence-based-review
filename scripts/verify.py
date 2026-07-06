from __future__ import annotations

import argparse
import pathlib
import re
import sys
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, cast

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import fetch
from lib import apis, cli_runtime, config, project, testflight
from lib.crossref_parse import (
    CONCERN_TYPES,
    PUBTYPE_KEYWORDS,
    RETRACTION_TYPES,
    TYPE_MAP,
    as_dict as _as_dict,
    as_int as _as_int,
    as_list as _as_list,
    as_str as _as_str,
    as_strings as _strings,
    author_initials as _author_initials,
    correction_types as _correction_types,
    format_author as _format_author,
    parse_crossref_payload as _parse_crossref_payload,
    parse_semantic_scholar_payload as _parse_semantic_scholar_payload,
    study_type,
)
import refs

VerifyOutcome = Literal["OK", "WARN", "FAILED", "ERROR"]
MismatchSeverity = Literal["WARN", "ERROR"]
LookupStatus = Literal["ok", "missing", "transient"]
StudyType = refs.StudyType

EUPMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
RETRACTION_WATCH_BASE = "https://api.labs.crossref.org/data/retractionwatch"
SEMANTIC_SCHOLAR_BASE = "https://api.semanticscholar.org/graph/v1"
DOI_PATTERN = re.compile(r"^10\..+/.+$")


def _empty_verification() -> refs.Verification:
    return {}


@dataclass
class VerifyResult:
    doi: str
    outcome: VerifyOutcome
    title: str | None = None
    year: int | None = None
    authors: list[str] = field(default_factory=list)
    journal: str | None = None
    issn: list[str] = field(default_factory=list)
    study_type: refs.StudyType | None = None
    verification_status: refs.VerificationStatus = "pending"
    verification: refs.Verification = field(default_factory=_empty_verification)
    retracted: bool = False
    retraction_notes: list[object] = field(default_factory=list)
    blocklist_reason: str | None = None
    blocklist_notes: list[object] = field(default_factory=list)


def _normalize_doi(doi: str) -> str:
    return doi.strip().lower()


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower()))


def jaccard(left: str, right: str) -> float:
    left_tokens = _tokenize(left)
    right_tokens = _tokenize(right)
    if not left_tokens and not right_tokens:
        return 1.0
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _family_from_name(author: str) -> str:
    if "," in author:
        return author.split(",", 1)[0].strip().lower()
    parts = author.split()
    return parts[-1].lower() if parts else ""


# Dutch / Romance / Iberian surname particles. "Van Dijck" parsed from CrossRef
# may come back as "C. Van Dijck" (prefix retained) or just "Dijck" (prefix
# stripped) depending on the publisher's metadata; we want both to match the
# user-supplied "Van Dijck, C." Skip them when comparing surnames.
_NAME_PARTICLES = frozenset(
    {
        "van", "von", "de", "del", "du", "da", "di", "do",
        "la", "le", "der", "den", "ten", "ter",
    }
)


def _strip_name_particles(family: str) -> str:
    tokens = [token for token in family.split() if token]
    while tokens and tokens[0] in _NAME_PARTICLES:
        tokens = tokens[1:]
    return " ".join(tokens) or family


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _warning_code(warning: str) -> str:
    return warning.split("(", 1)[0]


def _classify_mismatch_severity(
    warnings: list[str],
    *,
    force: bool = False,
    force_reason: str | None = None,
) -> tuple[MismatchSeverity, dict[str, object] | None]:
    codes = {_warning_code(warning) for warning in warnings}
    severity: MismatchSeverity = "WARN"
    for configured_codes, configured_severity in config.MISMATCH_SEVERITY.items():
        if set(configured_codes).issubset(codes) and configured_severity == "ERROR":
            severity = "ERROR"
            break

    if severity == "ERROR" and force:
        return "WARN", {
            "original_severity": "ERROR",
            "original_warnings": list(warnings),
            "original_warning_codes": sorted(codes),
            "force_reason": force_reason or "",
            "forced_at": _checked_at(),
        }
    return severity, None


def _verification_payload(
    *,
    provider: str,
    partial: bool,
    warnings: list[str],
    corrections: list[str],
    retraction_watch_checked: bool,
    severity: MismatchSeverity | None = None,
    force_mismatch: dict[str, object] | None = None,
) -> refs.Verification:
    payload: refs.Verification = {
        "provider": provider,
        "partial": partial,
        "warnings": warnings,
        "corrections": corrections,
        "retraction_watch_checked": retraction_watch_checked,
        "checked_at": _checked_at(),
    }
    if severity is not None:
        payload["severity"] = severity
    if force_mismatch is not None:
        payload["force_mismatch"] = force_mismatch
    return payload


def crossref(doi: str) -> dict[str, object] | None:
    payload = apis.get_json(
        f"https://api.crossref.org/works/{doi}",
        params=apis.with_mailto(),
    )
    return _parse_crossref_payload(payload)


def crossref_with_status(doi: str) -> tuple[LookupStatus, dict[str, object] | None]:
    status, payload = apis.get_json_with_status(
        f"https://api.crossref.org/works/{doi}",
        params=apis.with_mailto(),
    )
    if status != "ok":
        return status, None
    meta = _parse_crossref_payload(payload)
    if meta is None:
        return "missing", None
    return "ok", meta


def semantic_scholar(doi: str) -> dict[str, object] | None:
    payload = apis.get_json(
        f"{SEMANTIC_SCHOLAR_BASE}/paper/DOI:{doi}",
        params={"fields": "title,year,authors,venue"},
    )
    return _parse_semantic_scholar_payload(payload)


def semantic_scholar_with_status(doi: str) -> tuple[LookupStatus, dict[str, object] | None]:
    status, payload = apis.get_json_with_status(
        f"{SEMANTIC_SCHOLAR_BASE}/paper/DOI:{doi}",
        params={"fields": "title,year,authors,venue"},
    )
    if status != "ok":
        return status, None
    meta = _parse_semantic_scholar_payload(payload)
    if meta is None:
        return "missing", None
    return "ok", meta


def retraction_watch(doi: str) -> tuple[list[dict[str, object]], bool]:
    try:
        payload = apis.get_json(RETRACTION_WATCH_BASE, params={"doi": doi})
    except Exception:
        return [], False
    if payload is None:
        return [], True
    items = _as_list(payload.get("items"))
    return [item for item in items if isinstance(item, dict)], True


def eupmc_meta(doi: str) -> dict[str, object] | None:
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


def _first_author_mismatch(claimed_authors: list[str], actual_authors: list[str]) -> str | None:
    if not claimed_authors or not actual_authors:
        return None
    claimed_families = {
        _strip_name_particles(_family_from_name(author))
        for author in claimed_authors
        if author
    }
    actual_family = _strip_name_particles(_family_from_name(actual_authors[0]))
    if actual_family and actual_family not in claimed_families:
        return f"first_author_mismatch(actual={actual_authors[0]})"
    return None


def _snapshot_for_verify(entry: refs.Entry) -> refs.Entry:
    snapshot: refs.Entry = {
        "doi": entry["doi"],
        "title": entry.get("title", ""),
        "authors": list(entry.get("authors", [])),
    }
    year = entry.get("year")
    if isinstance(year, int):
        snapshot["year"] = year
    return snapshot


def verify_worker(
    snapshot: refs.Entry,
    cross_check_rw: bool = False,
) -> VerifyResult:
    doi = _normalize_doi(snapshot["doi"])
    warnings: list[str] = []
    provider = "crossref"
    partial = False

    meta = crossref(doi)
    if meta is None:
        fallback = semantic_scholar(doi)
        if fallback is None:
            return VerifyResult(
                doi=doi,
                outcome="FAILED",
                verification_status="failed",
                verification={
                    "provider": "none",
                    "partial": False,
                    "warnings": ["not_found_in_crossref_or_semantic_scholar"],
                    "corrections": [],
                    "retraction_watch_checked": False,
                    "checked_at": _checked_at(),
                },
            )
        meta = fallback
        provider = "semantic_scholar"
        partial = True
        warnings.append("semantic_scholar_fallback")

    update_to = _as_list(meta.get("update_to"))
    notes, corrections, crossref_retracted = _correction_types(update_to)
    if corrections:
        warnings.extend(corrections)

    rw_hits: list[dict[str, object]] = []
    rw_checked = False
    if cross_check_rw:
        rw_hits, rw_checked = retraction_watch(doi)
        if not rw_checked:
            warnings.append("retraction_watch_unreachable")
    retracted = crossref_retracted or bool(rw_hits)
    if retracted:
        warnings.append("retracted")

    verified_title = _as_str(meta.get("title")) or ""
    claimed_title = snapshot.get("title", "")
    if claimed_title and verified_title and jaccard(verified_title, claimed_title) < 0.4:
        warnings.append("title_mismatch")

    verified_year = _as_int(meta.get("year"))
    claimed_year = snapshot.get("year")
    if claimed_year is not None and verified_year is not None and claimed_year != verified_year:
        warnings.append(f"year_mismatch(actual={verified_year})")

    verified_authors = _strings(meta.get("authors"))
    author_warning = _first_author_mismatch(snapshot.get("authors", []), verified_authors)
    if author_warning:
        warnings.append(author_warning)

    severity, force_mismatch = _classify_mismatch_severity(warnings)
    outcome: VerifyOutcome = "OK" if not warnings else severity
    eupmc_hit = eupmc_meta(doi) if provider == "crossref" else None
    retraction_notes = notes + rw_hits
    return VerifyResult(
        doi=doi,
        outcome=outcome,
        title=verified_title,
        year=verified_year,
        authors=verified_authors,
        journal=_as_str(meta.get("journal")) or "",
        issn=[s for s in (meta.get("issn") or []) if isinstance(s, str)],
        study_type=study_type(meta, eupmc_hit),
        verification_status="failed" if outcome == "ERROR" else "verified",
        verification=_verification_payload(
            provider=provider,
            partial=partial,
            warnings=warnings,
            corrections=corrections,
            retraction_watch_checked=rw_checked,
            severity=severity if warnings else None,
            force_mismatch=force_mismatch,
        ),
        retracted=retracted,
        retraction_notes=retraction_notes,
        blocklist_reason="retracted" if retracted else None,
        blocklist_notes=retraction_notes if retracted else [],
    )


_PREDATORY_ISSNS_CACHE: frozenset[str] | None = None


def _predatory_issns() -> frozenset[str]:
    """Load the (user-populated) predatory-journal ISSN denylist (§E.1). Ships
    EMPTY → no-op by default; populate ``tools/data/predatory_issn.txt`` from a
    vetted source (e.g. an archived Beall's list / Cabells / DOAJ-removed) to
    auto-exclude those journals at verify time. ISSNs normalized to digits +
    X, hyphen-insensitive."""
    global _PREDATORY_ISSNS_CACHE
    if _PREDATORY_ISSNS_CACHE is None:
        path = pathlib.Path(__file__).parent / "data" / "predatory_issn.txt"
        out: set[str] = set()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                token = line.split("#", 1)[0].strip().upper().replace("-", "")
                if token:
                    out.add(token)
        _PREDATORY_ISSNS_CACHE = frozenset(out)
    return _PREDATORY_ISSNS_CACHE


def _flag_predatory(entry: refs.Entry, result: VerifyResult) -> None:
    """Auto-exclude an entry whose ISSN is on the predatory denylist. No-op
    when the denylist is empty (the shipped default)."""
    deny = _predatory_issns()
    if not deny:
        return
    hit = next(
        (s for s in (result.issn or []) if s.replace("-", "").upper() in deny),
        None,
    )
    if hit and not entry.get("excluded_reason"):
        entry["excluded_reason"] = f"predatory journal (ISSN denylist: {hit})"
        print(f"[SKIP] predatory ISSN {hit}: {result.doi}", file=sys.stderr)


def apply_verify_result(entry: refs.Entry, result: VerifyResult) -> refs.Entry:
    entry["doi"] = result.doi
    if result.outcome in {"FAILED", "ERROR"}:
        entry["verification_status"] = "failed"
        entry["verification"] = result.verification
        if result.blocklist_reason:
            refs.add_to_blocklist(
                result.doi,
                result.blocklist_reason,
                result.blocklist_notes,
            )
        return refs._ensure_defaults(entry)

    if result.title is not None:
        entry["title"] = result.title
    if result.year is not None:
        entry["year"] = result.year
    entry["authors"] = result.authors
    # citation_key staleness: when verify rewrites authors[0] (e.g. SS gave
    # us Sul as first author, CrossRef corrects to Chase), the stored
    # citation_key may still embed the old family name. WARN rather than
    # auto-rewrite — silently changing the key would break already-cited
    # [@key] markers in review.md. See testflight_retro.md 工具 #8.
    stored_key = entry.get("citation_key")
    if (
        isinstance(stored_key, str)
        and stored_key
        and entry.get("authors")
    ):
        expected_key = refs._deterministic_citation_key(entry)
        if stored_key != expected_key:
            print(
                f"[WARN] {result.doi} citation_key staleness: stored "
                f"'{stored_key}' but current authors[0]="
                f"'{entry['authors'][0]}' / year={entry.get('year')} would "
                f"generate '{expected_key}'. Keep stored key (safe; won't "
                f"break already-cited [@{stored_key}]) OR manually update "
                f"the entry json + any review.md citations.",
                file=sys.stderr,
            )
    if result.journal is not None:
        entry["journal"] = result.journal
    if result.issn:
        entry["issn"] = result.issn
    _flag_predatory(entry, result)
    if result.study_type is not None:
        entry["study_type"] = result.study_type
    entry["verification_status"] = result.verification_status
    entry["verification"] = result.verification
    entry["retracted"] = result.retracted
    entry["retraction_notes"] = result.retraction_notes
    # M1 (spec §2 verify --add OUT): persist the normalized publication-integrity
    # STATUS FIELDS — not just derive booleans at read time. journal + verification
    # + retracted are all set above, so metadata_flags is now accurate.
    entry["metadata_status"] = refs.metadata_status_fields(entry)
    if result.blocklist_reason:
        refs.add_to_blocklist(
            result.doi,
            result.blocklist_reason,
            result.blocklist_notes,
        )
    return refs._ensure_defaults(entry)


def verify_entry(
    entry: refs.Entry,
    cross_check_rw: bool = False,
) -> tuple[refs.Entry, VerifyOutcome]:
    snapshot = _snapshot_for_verify(entry)
    result = verify_worker(snapshot, cross_check_rw)
    updated = apply_verify_result(entry, result)
    return updated, result.outcome


def _parse_authors(raw_authors: str) -> list[str]:
    chunks = [part.strip() for part in raw_authors.split(";")]
    return [chunk for chunk in chunks if chunk]


def _summary_exit_code(stats: dict[str, int]) -> int:
    if stats["ERROR"]:
        return 2
    if stats["WARN"] or stats["FAILED"] or stats["RETRACTED_NEW"]:
        return 1
    return 0


def _add_exit_code(
    outcome: VerifyOutcome,
    in_blocklist: bool,
    retracted_new: bool,
    config_error: bool,
) -> int:
    del in_blocklist, retracted_new
    if config_error:
        return 1
    if outcome == "ERROR":
        return 2
    return 0


def _is_valid_doi(doi: str) -> bool:
    return DOI_PATTERN.match(doi) is not None


def _transient_verify_result(snapshot: refs.Entry, provider: str) -> VerifyResult:
    year = snapshot.get("year")
    return VerifyResult(
        doi=snapshot["doi"],
        outcome="WARN",
        title=snapshot.get("title", ""),
        year=year if isinstance(year, int) else None,
        authors=list(snapshot.get("authors", [])),
        journal=snapshot.get("journal", ""),
        study_type=cast(StudyType | None, snapshot.get("study_type")),
        verification_status="pending",
        verification={
            "provider": provider,
            "partial": False,
            "warnings": ["transient_error"],
            "corrections": [],
            "retraction_watch_checked": False,
            "checked_at": _checked_at(),
        },
    )


def verify_worker_for_add(
    snapshot: refs.Entry,
    cross_check_rw: bool = False,
    *,
    force: bool = False,
    force_reason: str | None = None,
) -> VerifyResult:
    doi = _normalize_doi(snapshot["doi"])
    warnings: list[str] = []
    provider = "crossref"
    partial = False

    crossref_status, meta = crossref_with_status(doi)
    if crossref_status == "transient":
        return _transient_verify_result(snapshot, provider)
    if meta is None:
        semantic_status, fallback = semantic_scholar_with_status(doi)
        if semantic_status == "transient":
            return _transient_verify_result(snapshot, "semantic_scholar")
        if fallback is None:
            return VerifyResult(
                doi=doi,
                outcome="FAILED",
                verification_status="failed",
                verification={
                    "provider": "none",
                    "partial": False,
                    "warnings": ["not_found_in_crossref_or_semantic_scholar"],
                    "corrections": [],
                    "retraction_watch_checked": False,
                    "checked_at": _checked_at(),
                },
            )
        meta = fallback
        provider = "semantic_scholar"
        partial = True
        warnings.append("semantic_scholar_fallback")

    update_to = _as_list(meta.get("update_to"))
    notes, corrections, crossref_retracted = _correction_types(update_to)
    if corrections:
        warnings.extend(corrections)

    rw_hits: list[dict[str, object]] = []
    rw_checked = False
    if cross_check_rw:
        rw_hits, rw_checked = retraction_watch(doi)
        if not rw_checked:
            warnings.append("retraction_watch_unreachable")
    retracted = crossref_retracted or bool(rw_hits)
    if retracted:
        warnings.append("retracted")

    verified_title = _as_str(meta.get("title")) or ""
    claimed_title = snapshot.get("title", "")
    if claimed_title and verified_title and jaccard(verified_title, claimed_title) < 0.4:
        warnings.append("title_mismatch")

    verified_year = _as_int(meta.get("year"))
    claimed_year = snapshot.get("year")
    if claimed_year is not None and verified_year is not None and claimed_year != verified_year:
        warnings.append(f"year_mismatch(actual={verified_year})")

    verified_authors = _strings(meta.get("authors"))
    author_warning = _first_author_mismatch(snapshot.get("authors", []), verified_authors)
    if author_warning:
        warnings.append(author_warning)

    severity, force_mismatch = _classify_mismatch_severity(
        warnings,
        force=force,
        force_reason=force_reason,
    )
    outcome: VerifyOutcome = "OK" if not warnings else severity
    eupmc_hit = eupmc_meta(doi) if provider == "crossref" else None
    retraction_notes = notes + rw_hits
    return VerifyResult(
        doi=doi,
        outcome=outcome,
        title=verified_title,
        year=verified_year,
        authors=verified_authors,
        journal=_as_str(meta.get("journal")) or "",
        issn=[s for s in (meta.get("issn") or []) if isinstance(s, str)],
        study_type=study_type(meta, eupmc_hit),
        verification_status="failed" if outcome == "ERROR" else "verified",
        verification=_verification_payload(
            provider=provider,
            partial=partial,
            warnings=warnings,
            corrections=corrections,
            retraction_watch_checked=rw_checked,
            severity=severity if warnings else None,
            force_mismatch=force_mismatch,
        ),
        retracted=retracted,
        retraction_notes=retraction_notes,
        blocklist_reason="retracted" if retracted else None,
        blocklist_notes=retraction_notes if retracted else [],
    )


def _verify_entry_for_add(
    entry: refs.Entry,
    cross_check_rw: bool = False,
    *,
    force: bool = False,
    force_reason: str | None = None,
) -> tuple[refs.Entry, VerifyOutcome, bool]:
    was_retracted = bool(entry.get("retracted", False))
    snapshot = _snapshot_for_verify(entry)
    result = verify_worker_for_add(
        snapshot,
        cross_check_rw,
        force=force,
        force_reason=force_reason,
    )
    updated = apply_verify_result(entry, result)
    retracted_new = bool(updated.get("retracted", False)) and not was_retracted
    return updated, result.outcome, retracted_new


def _load_gap_ids(topic_dir: pathlib.Path) -> set[str]:
    store = refs.load(topic_dir)
    if store is None:
        return set()
    return set(store.get("gaps", {}))


# ---- gap_type structured-field helpers ---------------------------------
# Required-field map lives in lib/patches.py so verify.py and
# lint_review.py share it. Missing required fields emit WARN here — not
# block — so users can iterate. lint_review will hard-fail/warn later.
from lib.patches import REQUIRED_FIELDS_BY_GAP_TYPE as _REQUIRED_FIELDS_BY_GAP_TYPE

_ALL_GAP_FIELDS: tuple[str, ...] = (
    "population",
    "intervention",
    "comparator",
    "outcome",
    "phenomenon",
    "candidate_mechanisms",
    "evidence_types",
    "item_a",
    "item_b",
    "dimensions",
    "comparison_level",
    "process",
    "audience",
    "decision_question",
    "reference_standard",
    "exposure",
    "at_risk_population",
    "adverse_outcomes",
    "threshold_ref",
    "differential_list",
    "discriminating_features",
    "measurement_method",
    "population_setting",
)


def _build_gap_fields(
    args: argparse.Namespace,
) -> tuple[dict[str, object], list[str]]:
    """Read all gap structured sub-field args off ``args`` and return a
    ``(fields_dict, missing_required_field_names)`` pair.

    Missing-required list is computed only when ``args.gap_type`` is set;
    when no gap_type is given, returns an empty list (the outer caller
    emits a separate WARN about missing classification)."""
    fields: dict[str, object] = {}
    for field_name in _ALL_GAP_FIELDS:
        value = getattr(args, field_name, None)
        if isinstance(value, str) and value.strip():
            fields[field_name] = value.strip()
    if getattr(args, "prevalence", False):
        fields["prevalence_subtag"] = True

    if not args.gap_type:
        return fields, []
    required = _REQUIRED_FIELDS_BY_GAP_TYPE.get(args.gap_type, ())
    missing = [name for name in required if name not in fields]
    return fields, missing


def _render_gap_description(
    user_description: str,
    gap_type: str | None,
    prevalence_subtag: bool,
) -> str:
    """Prepend a one-token `[gap_type]` tag to the user description so the
    type is visible in any tool that reads gap.description directly
    (research_log, gaps_status). Idempotent — if the user already wrote
    a leading `[...]` tag we don't double-prefix."""
    if not gap_type:
        return user_description
    if user_description.startswith("["):
        return user_description
    tag_inner = gap_type
    if prevalence_subtag and gap_type == "descriptive":
        tag_inner = "descriptive:prevalence"
    return f"[{tag_inner}] {user_description}"


def _arg_error(parser: argparse.ArgumentParser, message: str) -> None:
    parser.print_usage(sys.stderr)
    print(f"{parser.prog}: error: {message}", file=sys.stderr)
    raise SystemExit(1)


def _normalize_force_mismatch_args(
    parser: argparse.ArgumentParser,
    values: list[str] | None,
    reason: str | None,
) -> set[str]:
    if not values:
        if reason is not None:
            _arg_error(parser, "--force-mismatch-reason requires --force-mismatch")
        return set()
    if not config.FORCE_MISMATCH_ALLOWED:
        _arg_error(parser, "--force-mismatch is disabled by config")
    normalized: set[str] = set()
    for value in values:
        doi = _normalize_doi(value)
        if doi == "all":
            _arg_error(parser, "--force-mismatch all is not allowed; pass explicit DOI values")
        if not _is_valid_doi(doi):
            _arg_error(parser, f"invalid --force-mismatch DOI: {value}")
        normalized.add(doi)
    if reason is None or not reason.strip():
        _arg_error(parser, "--force-mismatch requires --force-mismatch-reason")
    if "doi.org/" not in reason:
        _arg_error(
            parser,
            '--force-mismatch-reason must include a doi.org/ landing-page URL or substring',
        )
    return normalized


def _print_add_result(
    doi: str,
    outcome: VerifyOutcome,
    entry: refs.Entry | None,
    *,
    in_blocklist: bool,
    retracted_new: bool,
) -> None:
    if in_blocklist:
        print(f"[WARN] skip blocked DOI: {doi}")
        return
    if retracted_new:
        print(f"[RETRACTED_NEW] {doi}")
        return
    if outcome == "ERROR":
        warnings = entry.get("verification", {}).get("warnings", []) if entry is not None else []
        print(
            f"\x1b[31m[ERROR]\x1b[0m metadata mismatch: {doi} {warnings}. "
            f'If intentional, use --force-mismatch {doi} '
            '--force-mismatch-reason "doi.org/... actual_title=... reason=..."',
            file=sys.stderr,
        )
        return
    if outcome == "FAILED":
        print(f"[FAILED] both 404: {doi}")
        return
    if outcome == "WARN":
        warnings = entry.get("verification", {}).get("warnings", []) if entry is not None else []
        if warnings == ["transient_error"]:
            print(f"[WARN] verify transient error: {doi}")
            return
        print(f"[WARN] {doi} {warnings}")
        return
    print(f"[OK] {doi}")


def _collect_dois_to_process(
    store: refs.Store,
    target_doi: str | None,
    recheck: bool,
    limit: int,
) -> list[str]:
    dois_to_process: list[str] = []
    for doi, entry in list(store["entries"].items()):
        if target_doi is not None and doi != target_doi:
            continue
        if entry.get("verification_status") == "verified" and not recheck:
            continue
        # Bulk mode (no explicit --doi): skip sticky-excluded entries. A chained
        # verify (genealogy/search) on the bulk path used to re-verify every
        # excluded pending entry, flipping it to status=verified (excluded_reason
        # survived, so no resurrection — just wasted API + inflated counts and
        # polluted notes). An explicit --doi still processes the entry.
        if target_doi is None and entry.get("excluded_reason"):
            continue
        if limit and len(dois_to_process) >= limit:
            break
        dois_to_process.append(doi)
    return dois_to_process


def _run_parallel(
    store: refs.Store,
    topic_dir: pathlib.Path,
    dois_to_process: list[str],
    *,
    cross_check_rw: bool,
    parallel: int,
    chain_fetch: bool,
    fetch_include: set[str],
    topic_tmp: pathlib.Path,
    checkpoint_every: int = 10,
) -> dict[str, int]:
    """Verify all DOIs; if chain_fetch, dispatch each fetch into a SEPARATE
    fetch pool the moment its verify completes.

    Why two pools rather than one shared queue: when verify and fetch share
    a single ThreadPoolExecutor, all verifies are submitted upfront and
    fetches are appended to the back of the FIFO queue. Workers drain
    verifies first, fetches stack at the tail, and the result is
    effectively a two-phase batch (all verify, then all fetch) rather
    than the pipelined behavior the docstring promises. Worse, when
    verify is slow (CrossRef + RW round-trip ~3-5 s) and fetch is fast
    (cached EuPMC search ~0.3 s), fast fetches still wait behind every
    pending verify, hiding fetch's natural concurrency.

    Splitting verify_pool and fetch_pool gives each its own worker budget,
    lets fetches start as soon as their verify finishes, and removes the
    starvation-by-FIFO-position problem entirely. The two pools hit
    disjoint API surfaces (CrossRef / Retraction Watch vs EuPMC /
    Unpaywall), so there is no rate-limit contention; the only shared
    endpoint is EuPMC /search, which is cached by lib/apis.py."""
    stats = {"OK": 0, "WARN": 0, "FAILED": 0, "ERROR": 0, "RETRACTED_NEW": 0}
    assets_dir = topic_tmp / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    pending: dict[Future[object], tuple[str, str]] = {}
    completions = 0

    verify_pool = ThreadPoolExecutor(
        max_workers=parallel, thread_name_prefix="verify"
    )
    fetch_pool: ThreadPoolExecutor | None = (
        ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="fetch")
        if chain_fetch
        else None
    )
    try:
        for doi in dois_to_process:
            snapshot = _snapshot_for_verify(store["entries"][doi])
            future = cast(
                Future[object],
                verify_pool.submit(verify_worker, snapshot, cross_check_rw),
            )
            pending[future] = ("verify", doi)

        while pending:
            done, _ = wait(set(pending.keys()), return_when=FIRST_COMPLETED)
            for future in done:
                kind, doi = pending.pop(future)
                try:
                    payload = future.result()
                except Exception as exc:
                    print(f"[ERROR] {kind} {doi}: {exc}", file=sys.stderr)
                    if kind == "verify":
                        stats["ERROR"] += 1
                    completions += 1
                    continue

                if kind == "verify":
                    result = cast(VerifyResult, payload)
                    entry = store["entries"][doi]
                    was_retracted = bool(entry.get("retracted", False))
                    updated = apply_verify_result(entry, result)
                    store["entries"][doi] = updated

                    if result.outcome == "ERROR":
                        stats["ERROR"] += 1
                        warning_list = updated["verification"].get("warnings", [])
                        print(f"[ERROR] {doi} {warning_list}", file=sys.stderr)
                    elif result.outcome == "FAILED":
                        stats["FAILED"] += 1
                        print(f"[FAILED] {doi} not found in Crossref or Semantic Scholar")
                    elif updated.get("retracted", False) and not was_retracted:
                        stats["RETRACTED_NEW"] += 1
                        print(f"[RETRACTED] {doi}")
                    elif result.outcome == "WARN":
                        stats["WARN"] += 1
                        warning_list = updated["verification"].get("warnings", [])
                        print(f"[WARN] {doi} {warning_list}")
                    else:
                        stats["OK"] += 1
                        print(f"[OK] {doi}")

                    if (
                        chain_fetch
                        and fetch_pool is not None
                        and result.outcome not in ("FAILED", "ERROR")
                        and not updated.get("retracted", False)
                    ):
                        fetch_future = cast(
                            Future[object],
                            fetch_pool.submit(
                                fetch.fetch_compute,
                                fetch._snapshot_for_fetch(updated),
                                topic_tmp,
                                fetch_include,
                                5,
                                5,
                            ),
                        )
                        pending[fetch_future] = ("fetch", doi)
                else:
                    fetch_result = cast(fetch.FetchResult, payload)
                    fetch.apply_fetch_result(store["entries"][doi], fetch_result, assets_dir)

                completions += 1
                if completions % checkpoint_every == 0:
                    refs.save(topic_dir, store)
    finally:
        verify_pool.shutdown(wait=True)
        if fetch_pool is not None:
            fetch_pool.shutdown(wait=True)

    refs.save(topic_dir, store)
    return stats


def _run_serial(
    store: refs.Store,
    dois_to_process: list[str],
    *,
    cross_check_rw: bool,
) -> dict[str, int]:
    stats = {"OK": 0, "WARN": 0, "FAILED": 0, "ERROR": 0, "RETRACTED_NEW": 0}
    for doi in dois_to_process:
        entry = store["entries"][doi]
        was_retracted = bool(entry.get("retracted", False))
        updated_entry, outcome = verify_entry(entry, cross_check_rw=cross_check_rw)
        store["entries"][doi] = updated_entry

        if outcome == "ERROR":
            stats["ERROR"] += 1
            warning_list = updated_entry["verification"].get("warnings", [])
            print(f"[ERROR] {doi} {warning_list}", file=sys.stderr)
            continue

        if outcome == "FAILED":
            stats["FAILED"] += 1
            print(f"[FAILED] {doi} not found in Crossref or Semantic Scholar")
            continue

        if updated_entry.get("retracted", False) and not was_retracted:
            stats["RETRACTED_NEW"] += 1
            print(f"[RETRACTED] {doi}")
            continue

        if outcome == "WARN":
            stats["WARN"] += 1
            warning_list = updated_entry["verification"].get("warnings", [])
            print(f"[WARN] {doi} {warning_list}")
            continue

        stats["OK"] += 1
        print(f"[OK] {doi}")

    return stats


def _excluded_reason_for(topic_dir: pathlib.Path, doi: str) -> str | None:
    """Read the existing exclusion reason for `doi`, or None if the DOI is
    not in the store or not excluded. Reads the single entry file (cheap)
    rather than loading the whole store."""
    entry = refs.get_entry(topic_dir, doi)
    if entry is None:
        return None
    reason = entry.get("excluded_reason")
    return reason if isinstance(reason, str) and reason else None


def _append_readd_log(
    topic_dir: pathlib.Path,
    doi: str,
    prior_reason: str,
    readd_reason: str,
) -> None:
    """Audit the deliberate resurrection of a previously-excluded DOI in
    research_log.md so the un-exclusion is never silent."""
    log_path = topic_dir / "research_log.md"
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = (
        f"- [{timestamp}] readd `{doi}` (was excluded: {prior_reason})"
        f" — {readd_reason}"
    )
    if log_path.exists():
        existing = log_path.read_text(encoding="utf-8").rstrip()
        log_path.write_text(existing + "\n" + line + "\n", encoding="utf-8")
        return
    log_path.write_text(line + "\n", encoding="utf-8")


def _suggest_dois_by_title(title: str, rows: int = 4) -> None:
    """On a single --add `title_mismatch`, the supplied DOI almost always points
    to a *different* paper (mis-typed / copy-pasted / publisher swapped the DOI),
    NOT a CrossRef metadata glitch. The bare ERROR line only mentions
    --force-mismatch, which wrongly assumes the DOI is right — so query CrossRef
    by the claimed title and print candidate correct DOIs. The caller verifies one
    and re-adds it, instead of guessing DOI formats or force-mismatching.
    Best-effort: any network/parse failure prints nothing (ERROR line still stands).
    """
    title = (title or "").strip()
    if not title:
        return
    try:
        payload = apis.get_json(
            "https://api.crossref.org/works",
            params=apis.with_mailto(
                {
                    "query.bibliographic": title,
                    "rows": rows,
                    "select": "DOI,title,author,issued",
                }
            ),
        )
    except Exception:
        return
    message = payload.get("message") if isinstance(payload, dict) else None
    items = (message or {}).get("items") if isinstance(message, dict) else None
    if not isinstance(items, list) or not items:
        return
    ranked: list[tuple[float, str, str, str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        cand_doi = str(item.get("DOI") or "").lower()
        titles = item.get("title") or []
        cand_title = titles[0] if isinstance(titles, list) and titles else ""
        if not cand_doi or not cand_title:
            continue
        authors = item.get("author") or []
        first = ""
        if isinstance(authors, list) and authors and isinstance(authors[0], dict):
            first = str(authors[0].get("family") or authors[0].get("name") or "")
        year = ""
        issued = item.get("issued")
        if isinstance(issued, dict):
            parts = issued.get("date-parts") or []
            if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
                year = str(parts[0][0])
        ranked.append((jaccard(cand_title, title), cand_doi, cand_title, first, year))
    if not ranked:
        return
    ranked.sort(reverse=True)
    print(
        "       ↳ 该 DOI 多半指向了另一篇文献。按标题检索到的候选正确 DOI"
        "（先核对再 --add，别急着 --force-mismatch）：",
        file=sys.stderr,
    )
    for score, cand_doi, cand_title, first, year in ranked[:3]:
        mark = "  ← best title match" if score >= 0.6 else ""
        meta = " ".join(p for p in (first, year) if p)
        print(f"         {cand_doi}  ({meta}) {cand_title[:64]}{mark}", file=sys.stderr)


def _handle_add(
    topic_dir: pathlib.Path,
    *,
    add_args: tuple[str, str, str, str],
    gap_id: str | None,
    source: str,
    round_number: int,
    cross_check_rw: bool,
    force_mismatch: set[str] | None = None,
    force_mismatch_reason: str | None = None,
    study_type_override: str | None = None,
    readd: bool = False,
    readd_reason: str | None = None,
) -> int:
    blocklist = refs.load_blocklist()
    raw_doi, title, year_text, authors_text = add_args
    doi = _normalize_doi(raw_doi)
    if not _is_valid_doi(doi):
        print(f"[ERROR] invalid DOI format: {raw_doi}")
        return _add_exit_code("FAILED", False, False, True)
    try:
        year = int(year_text)
    except ValueError:
        print(f"[ERROR] invalid year: {year_text}")
        return _add_exit_code("FAILED", False, False, True)

    if gap_id is not None and gap_id not in _load_gap_ids(topic_dir):
        print(f"[ERROR] gap not declared: {gap_id}")
        return _add_exit_code("FAILED", False, False, True)

    if refs.is_blocked(blocklist, doi):
        _print_add_result(doi, "WARN", None, in_blocklist=True, retracted_new=False)
        return _add_exit_code("WARN", True, False, False)

    # §B sticky exclusion: a DOI deliberately excluded in an earlier round is
    # denied by default. This stops a re-search / genealogy hand-off from
    # quietly resurrecting noise (and clearing the excluded flag via upsert).
    # `--readd` (with `--readd-reason`) is the explicit, audited override.
    prior_exclusion = _excluded_reason_for(topic_dir, doi)
    if prior_exclusion is not None and not readd:
        print(
            f"[WARN] skip previously-excluded DOI: {doi} "
            f"(excluded: {prior_exclusion}). If you intend to resurrect it, "
            'pass --readd --readd-reason "why this is no longer noise".',
            file=sys.stderr,
        )
        return _add_exit_code("WARN", True, False, False)
    if prior_exclusion is not None and readd:
        if not readd_reason or not readd_reason.strip():
            print(
                f"[ERROR] --readd for {doi} requires --readd-reason "
                "(audit trail for un-excluding deliberate noise).",
                file=sys.stderr,
            )
            return _add_exit_code("FAILED", False, False, True)
        # Clear the exclusion on disk first (upsert would otherwise preserve
        # it), then audit the resurrection in research_log.md.
        refs.clear_exclusion_on_disk(topic_dir, doi)
        _append_readd_log(topic_dir, doi, prior_exclusion, readd_reason.strip())
        print(f"[READD] cleared exclusion: {doi} — {readd_reason.strip()}")

    current_entry: refs.Entry = {
        "doi": doi,
        "title": title,
        "year": year,
        "authors": _parse_authors(authors_text),
        "source": source,
        "added_round": round_number,
        "gap": gap_id,
        "verification_status": "pending",
    }
    force_hit = doi in (force_mismatch or set())
    if force_hit:
        updated_entry, outcome, retracted_new = _verify_entry_for_add(
            current_entry,
            cross_check_rw=cross_check_rw,
            force=True,
            force_reason=force_mismatch_reason,
        )
    else:
        updated_entry, outcome, retracted_new = _verify_entry_for_add(
            current_entry,
            cross_check_rw=cross_check_rw,
        )
    if outcome == "ERROR":
        _print_add_result(
            doi,
            outcome,
            updated_entry,
            in_blocklist=False,
            retracted_new=False,
        )
        err_warnings = updated_entry.get("verification", {}).get("warnings", [])
        if "title_mismatch" in err_warnings and not force_hit:
            _suggest_dois_by_title(add_args[1])
        return _add_exit_code(outcome, False, False, False)

    # Manual study_type override wins over auto-classification. Applied after
    # verification so it isn't clobbered by study_type() derived from coarse
    # CrossRef/EuPMC metadata (see --study-type help). Note: a later --recheck
    # re-derives study_type, so re-run --study-type if you recheck the entry.
    if study_type_override is not None:
        updated_entry["study_type"] = cast(refs.StudyType, study_type_override)

    written_entry = refs.put_entry(topic_dir, updated_entry)
    _print_add_result(
        doi,
        outcome,
        written_entry,
        in_blocklist=False,
        retracted_new=retracted_new,
    )
    return _add_exit_code(outcome, False, retracted_new, False)


def _handle_add_batch(
    topic_dir: pathlib.Path,
    batch_path: pathlib.Path,
    *,
    default_gap: str | None,
    source: str,
    round_number: int,
    cross_check_rw: bool,
    force_mismatch: set[str] | None = None,
    force_mismatch_reason: str | None = None,
    study_type_override: str | None = None,
    readd: bool = False,
    readd_reason: str | None = None,
) -> int:
    """Add many entries from a TSV (DOI<TAB>TITLE<TAB>YEAR<TAB>AUTHORS[<TAB>GAP])
    in one process, reusing _handle_add per row — so blocklist / sticky-exclusion
    / force-mismatch / study_type / title_mismatch handling are byte-identical to
    a single --add, just without the per-row interpreter cold-start. Blank and
    '#'-prefixed lines are skipped; a malformed or failing row is reported and
    skipped (the batch never aborts). Per-row GAP overrides --gap (empty GAP →
    falls back to --gap). Returns the worst per-row exit code
    (2 verify-error > 1 config-error > 0 ok/warn)."""
    try:
        text = batch_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"[ERROR] cannot read --add-batch file {batch_path}: {exc}",
            file=sys.stderr,
        )
        return 1
    worst = 0
    processed = non_ok = 0
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) < 4:
            print(
                f"[ERROR] line {lineno}: expected "
                "DOI<TAB>TITLE<TAB>YEAR<TAB>AUTHORS[<TAB>GAP], got "
                f"{len(parts)} field(s) — skipped",
                file=sys.stderr,
            )
            non_ok += 1
            worst = max(worst, 2)
            continue
        doi, title, year_text, authors_text = (p.strip() for p in parts[:4])
        row_gap = (
            parts[4].strip()
            if len(parts) >= 5 and parts[4].strip()
            else default_gap
        )
        processed += 1
        print(f"--- line {lineno}: {doi} ---")
        rc = _handle_add(
            topic_dir,
            add_args=(doi, title, year_text, authors_text),
            gap_id=row_gap,
            source=source,
            round_number=round_number,
            cross_check_rw=cross_check_rw,
            force_mismatch=force_mismatch,
            force_mismatch_reason=force_mismatch_reason,
            study_type_override=study_type_override,
            readd=readd,
            readd_reason=readd_reason,
        )
        if rc != 0:
            non_ok += 1
        worst = max(worst, rc)
    print(f"=== add-batch: {processed} row(s) processed, {non_ok} non-OK ===")
    return worst


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("topic_dir", help="Path to a topic directory under reviews/")
    parser.add_argument("--add", nargs=4, metavar=("DOI", "TITLE", "YEAR", "AUTHORS"))
    parser.add_argument(
        "--add-batch",
        metavar="FILE",
        help="Batch-add entries from a TSV (DOI<TAB>TITLE<TAB>YEAR<TAB>AUTHORS"
        "[<TAB>GAP]) in ONE process — no per-row interpreter cold-start. Blank "
        "and '#'-prefixed lines are skipped; a malformed or failing row is "
        "reported and skipped (batch never aborts). Per-row GAP overrides "
        "--gap. Pairs with `search.py --emit-batch`. Mutually exclusive with "
        "--add.",
    )
    parser.add_argument(
        "--gap",
        metavar="GAP_ID",
        help="Attach this entry to a declared gap.",
    )
    parser.add_argument(
        "--study-type",
        choices=(
            "rct",
            "meta",
            "cohort",
            "case_control",
            "review",
            "mechanism",
            "guideline",
            "other",
        ),
        help=(
            "Manually set study_type on the added entry, overriding the "
            "auto-classification. Use when the analyst has identified the design "
            "but CrossRef/EuPMC metadata is too coarse (e.g. a cohort or RCT that "
            "auto-classifies to 'other'). Only applies with --add."
        ),
    )
    parser.add_argument(
        "--declare-gap",
        nargs=2,
        metavar=("GAP_ID", "DESCRIPTION"),
        help="Declare a new gap before adding entries. Idempotent.",
    )
    # ---- gap classification & structured sub-fields --------------------
    # Used with --declare-gap. gap_type taxonomy = decision / descriptive /
    # mechanism / comparison / methodology / safety / diagnostic (see
    # docs/methodology_playbook.md §2 — derived from a 26-review subagent
    # survey). Each gap_type has its own required sub-field set; missing
    # fields warn but don't block the declare (so callers can iterate).
    _GAP_TYPE_CHOICES = (
        "decision",
        "descriptive",
        "mechanism",
        "comparison",
        "methodology",
        "safety",
        "diagnostic",
    )
    parser.add_argument(
        "--gap-type",
        choices=_GAP_TYPE_CHOICES,
        help="Primary gap type for --declare-gap (see methodology_playbook §2).",
    )
    parser.add_argument(
        "--secondary-type",
        choices=_GAP_TYPE_CHOICES,
        help="Optional secondary type for cross-class gaps (e.g. decision+mechanism).",
    )
    parser.add_argument(
        "--query",
        help="(--declare-gap) spec-N2 per-gap search query — English/optimized terms used by the "
             "round instead of the CJK description (which returns wrong-language noise). Persisted.",
    )
    parser.add_argument(
        "--relevance-terms",
        help="(--declare-gap) spec-N2 per-gap relevance terms for the C2 gate / genealogy. Persisted.",
    )
    parser.add_argument(
        "--depends-on",
        action="append",
        default=[],
        metavar="GAP_ID",
        help=(
            "Mark this gap as depending on another gap (resolve in topological "
            "order). Repeat for multiple deps."
        ),
    )
    parser.add_argument(
        "--subgap-of",
        metavar="GAP_ID",
        help=(
            "Mark this gap as a sub-gap of another (e.g. gap-3.1 subgap-of gap-3). "
            "Subgaps share their parent's evidence base for term_check."
        ),
    )
    parser.add_argument(
        "--addendum",
        action="store_true",
        help=(
            "Mark this gap as a targeted addendum to an ALREADY-saturated topic "
            "(e.g. a follow-up question on a finished review). term_check then "
            "does not force an extra consolidation round for it and does not "
            "count this round against the lifetime hard cap — but every per-gap "
            "evidence floor (≥3 verified, ≥1 RCT/meta where required) still "
            "applies. Use only when continuing a review that already reached "
            "saturated/hard_stop."
        ),
    )
    parser.add_argument(
        "--description-override",
        help=(
            "Skip the auto gap_type-prefix on description and use this string "
            "verbatim."
        ),
    )
    # decision (PICO)
    parser.add_argument("--population", help="decision/safety: target population.")
    parser.add_argument("--intervention", help="decision: intervention or choice.")
    parser.add_argument("--comparator", help="decision: comparison arm.")
    parser.add_argument("--outcome", help="decision: outcome / endpoint.")
    # mechanism
    parser.add_argument("--phenomenon", help="mechanism/descriptive: phenomenon under study.")
    parser.add_argument(
        "--candidate-mechanisms",
        help="mechanism: ';'-separated candidate mechanisms (≥2).",
    )
    parser.add_argument(
        "--evidence-types",
        help="mechanism: required evidence types e.g. 'mechanism;animal;human'.",
    )
    # comparison
    parser.add_argument("--item-a", help="comparison: first item.")
    parser.add_argument("--item-b", help="comparison: second item.")
    parser.add_argument(
        "--dimensions",
        help="comparison: ';'-separated comparison dimensions (≥3).",
    )
    parser.add_argument(
        "--comparison-level",
        choices=("product", "class", "same_dose", "same_setting"),
        help="comparison: granularity of the comparison.",
    )
    # methodology
    parser.add_argument("--process", help="methodology: which process is being evaluated.")
    parser.add_argument("--audience", help="methodology/decision: who runs this process.")
    parser.add_argument("--decision-question", help="methodology: what decision the result informs.")
    parser.add_argument(
        "--reference-standard",
        help="methodology/diagnostic: gold-standard reference.",
    )
    # safety
    parser.add_argument("--exposure", help="safety: exposure source + dose + duration.")
    parser.add_argument("--at-risk-population", help="safety: sensitive sub-populations.")
    parser.add_argument("--adverse-outcomes", help="safety: ';'-separated negative endpoints.")
    parser.add_argument(
        "--threshold-ref",
        help="safety: regulatory threshold reference (AOEL/ADI/RfD/etc).",
    )
    # diagnostic
    parser.add_argument(
        "--differential-list",
        help="diagnostic: ';'-separated differential dx list (≥3).",
    )
    parser.add_argument(
        "--discriminating-features",
        help="diagnostic: imaging/site/timing features used to discriminate.",
    )
    # descriptive
    parser.add_argument("--measurement-method", help="descriptive: how the phenomenon is measured.")
    parser.add_argument("--population-setting", help="descriptive: population / sample frame / setting.")
    parser.add_argument(
        "--prevalence",
        action="store_true",
        help="descriptive: tag as a prevalence sub-type (Sample frame required).",
    )

    parser.add_argument("--source", default="seed")
    parser.add_argument("--round", dest="round_number", type=int, default=1)
    parser.add_argument("--recheck", action="store_true")
    parser.add_argument(
        "--readd",
        action="store_true",
        help=(
            "With --add: deliberately resurrect a DOI that was excluded in an "
            "earlier round (sticky exclusion denylist, §B). Requires "
            "--readd-reason. Without it, an excluded DOI is skipped to prevent "
            "accidental un-exclusion via re-search / genealogy."
        ),
    )
    parser.add_argument(
        "--readd-reason",
        metavar="TEXT",
        help="Audit reason for --readd; logged to research_log.md.",
    )
    parser.add_argument(
        "--doi",
        metavar="DOI",
        help="Verify only this single DOI; skip the rest of the store.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Cap the number of entries processed in this pass (0 = no cap).",
    )
    parser.add_argument(
        "--cross-check-rw",
        action="store_true",
        help=(
            "Opt into the experimental Crossref Labs Retraction Watch endpoint "
            "as a redundant check. Default off — Crossref `update-to` is the "
            "authoritative source."
        ),
    )
    parser.add_argument(
        "--populate-signals",
        action="store_true",
        help=(
            "After verify, eagerly populate journal_signals via OpenAlex. "
            "Default off."
        ),
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="Parallel workers for verify. 1 = serial.",
    )
    parser.add_argument(
        "--chain-fetch",
        action="store_true",
        help="Pipeline fetch after each verify succeeds.",
    )
    parser.add_argument(
        "--fetch-include",
        default="abstract",
        help="When --chain-fetch, fetch include modes (comma-separated).",
    )
    parser.add_argument(
        "--force-mismatch",
        action="append",
        metavar="DOI",
        help=(
            "Allow a title mismatch for this explicit DOI after manual "
            "doi.org landing-page verification. Requires --force-mismatch-reason."
        ),
    )
    parser.add_argument(
        "--force-mismatch-reason",
        metavar="TEXT",
        help=(
            "Audit reason for --force-mismatch. Must be non-empty and include "
            "a doi.org/ landing-page URL or substring."
        ),
    )
    args = parser.parse_args()
    force_mismatch = _normalize_force_mismatch_args(
        parser,
        cast(list[str] | None, args.force_mismatch),
        cast(str | None, args.force_mismatch_reason),
    )

    try:
        topic_dir = pathlib.Path(args.topic_dir)
        if args.add and args.add_batch:
            print(
                "[ERROR] --add and --add-batch are mutually exclusive.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        op = "add" if args.add else ("add_batch" if args.add_batch else "bulk")
        with testflight.timer(
            "verify",
            op,
            topic_dir=topic_dir,
            parallel=args.parallel,
            chain_fetch=args.chain_fetch,
        ) as detail:
            if args.declare_gap:
                gap_id, raw_description = args.declare_gap
                fields, missing_required = _build_gap_fields(args)
                description = (
                    args.description_override
                    if args.description_override
                    else _render_gap_description(
                        raw_description,
                        args.gap_type,
                        bool(getattr(args, "prevalence", False)),
                    )
                )

                # Validate subgap-of / depends-on point to existing gaps.
                warnings: list[str] = []
                if args.subgap_of or args.depends_on:
                    existing_gaps = _load_gap_ids(topic_dir)
                    if (
                        args.subgap_of
                        and args.subgap_of not in existing_gaps
                        and args.subgap_of != gap_id
                    ):
                        warnings.append(
                            f"[WARN] --subgap-of references undeclared gap: {args.subgap_of}"
                        )
                    for dep in args.depends_on or []:
                        if dep not in existing_gaps and dep != gap_id:
                            warnings.append(
                                f"[WARN] --depends-on references undeclared gap: {dep}"
                            )
                if not args.gap_type:
                    warnings.append(
                        "[WARN] --gap-type not given; lint_review will flag this gap"
                    )

                def _declare(meta: refs.StoreMeta) -> None:
                    refs.declare_gap(
                        meta,
                        gap_id,
                        description,
                        args.round_number,
                        gap_type=cast("refs.GapType | None", args.gap_type),
                        secondary_type=cast(
                            "refs.GapType | None", args.secondary_type
                        ),
                        fields=fields,
                        depends_on=args.depends_on or None,
                        subgap_of=args.subgap_of,
                        query=args.query,
                        relevance_terms=args.relevance_terms,
                        addendum=args.addendum,
                    )

                refs.update_meta(topic_dir, _declare)
                tag = " [addendum]" if args.addendum else ""
                print(f"[OK] declared {gap_id}:{tag} {description}")
                for warning in warnings:
                    print(warning)
                for required_field in missing_required:
                    flag = f"--{required_field.replace('_', '-')}"
                    print(
                        f"[WARN] gap_type={args.gap_type} missing required "
                        f"field: {flag}"
                    )

                # If the caller only wanted to declare the gap (no --add and
                # no explicit --doi/--recheck/--limit), exit cleanly here.
                # Previously this fell through to the bulk-verify path, which
                # would re-verify every pending entry in the store — a
                # surprise 100+ second run when the user just wanted to
                # register a gap label.
                bulk_verify_requested = (
                    bool(args.add)
                    or bool(args.add_batch)
                    or args.doi is not None
                    or args.recheck
                    or args.limit
                )
                if not bulk_verify_requested:
                    detail.update({"declared_gap": gap_id})
                    raise SystemExit(0)

            if args.add:
                raise SystemExit(
                    _handle_add(
                        topic_dir,
                        add_args=cast(tuple[str, str, str, str], tuple(args.add)),
                        gap_id=args.gap,
                        source=args.source,
                        round_number=args.round_number,
                        cross_check_rw=args.cross_check_rw,
                        force_mismatch=force_mismatch,
                        force_mismatch_reason=args.force_mismatch_reason,
                        study_type_override=args.study_type,
                        readd=args.readd,
                        readd_reason=args.readd_reason,
                    )
                )

            if args.add_batch:
                raise SystemExit(
                    _handle_add_batch(
                        topic_dir,
                        pathlib.Path(args.add_batch),
                        default_gap=args.gap,
                        source=args.source,
                        round_number=args.round_number,
                        cross_check_rw=args.cross_check_rw,
                        force_mismatch=force_mismatch,
                        force_mismatch_reason=args.force_mismatch_reason,
                        study_type_override=args.study_type,
                        readd=args.readd,
                        readd_reason=args.readd_reason,
                    )
                )

            store = refs.load(topic_dir) or refs.new_store(topic_dir.name)
            target_doi = _normalize_doi(args.doi) if args.doi else None
            dois_to_process = _collect_dois_to_process(
                store,
                target_doi,
                args.recheck,
                args.limit,
            )

            if args.parallel > 1 or args.chain_fetch:
                fetch_include = (
                    fetch._parse_include(args.fetch_include) if args.chain_fetch else set()
                )
                topic_tmp = project.topic_tmp(topic_dir.name)
                topic_tmp.mkdir(parents=True, exist_ok=True)
                stats = _run_parallel(
                    store,
                    topic_dir,
                    dois_to_process,
                    cross_check_rw=args.cross_check_rw,
                    parallel=max(args.parallel, 2 if args.chain_fetch else 1),
                    chain_fetch=args.chain_fetch,
                    fetch_include=fetch_include,
                    topic_tmp=topic_tmp,
                )
            else:
                stats = _run_serial(
                    store,
                    dois_to_process,
                    cross_check_rw=args.cross_check_rw,
                )
                refs.save(topic_dir, store)

            if args.populate_signals:
                from lib import enrich

                for entry in store["entries"].values():
                    if entry.get("verification_status") != "verified":
                        continue
                    enrich.ensure_journal_signals(entry)
                refs.save(topic_dir, store)

            detail.update(
                {
                    "ok": stats["OK"],
                    "warn": stats["WARN"],
                    "failed": stats["FAILED"],
                    "error": stats["ERROR"],
                    "retracted_new": stats["RETRACTED_NEW"],
                }
            )
            print(f"summary: {stats}")
            raise SystemExit(_summary_exit_code(stats))
    except SystemExit:
        raise
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(2)


def run(argv: list[str], cwd: str | None = None, env: dict[str, str] | None = None) -> int:
    """In-process entry for the daemon (E3) / tests; wraps main() via
    cli_runtime so the exit-code contract matches the standalone CLI."""
    return cli_runtime.invoke(main, argv, prog="verify.py", cwd=cwd, env=env)


if __name__ == "__main__":
    from lib import daemon

    raise SystemExit(daemon.cli_entry("verify", main))
