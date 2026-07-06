"""CrossRef / Semantic Scholar payload parsing and study-type classification.

Extracted from ``tools/verify.py`` (2026-04-16) to keep verify.py focused on
orchestration. The helpers here are pure functions with no I/O.

Consumers:
- ``tools/verify.py`` — imports the parsers + classifier + constants
"""
from __future__ import annotations

import re
from typing import Literal

# Public alias so downstream modules don't have to touch refs.py for the type.
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

RETRACTION_TYPES = {"retraction"}
CONCERN_TYPES = {"correction", "expression_of_concern"}

TYPE_MAP: dict[str, StudyType] = {
    "review-article": "review",
    "meta-analysis": "meta",
    "randomized-controlled-trial": "rct",
    "clinical-trial": "rct",
    "guideline": "guideline",
    "case-report": "case_control",
}

PUBTYPE_KEYWORDS: dict[StudyType, tuple[str, ...]] = {
    "rct": ("randomized controlled trial", "clinical trial"),
    "meta": ("meta-analysis", "systematic review"),
    "cohort": ("cohort studies", "prospective studies", "retrospective studies"),
    "case_control": ("case-control studies", "case report"),
    "review": ("review", "narrative review"),
    "guideline": ("practice guideline", "consensus development conference"),
    "mechanism": ("in vitro", "animal", "mice", "rats"),
    "other": (),
}


# ---------------------------------------------------------------------------
# JSON type-narrowing helpers
# ---------------------------------------------------------------------------


def as_dict(value: object) -> dict[str, object] | None:
    return value if isinstance(value, dict) else None


def as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def as_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def as_strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


# ---------------------------------------------------------------------------
# Author formatting
# ---------------------------------------------------------------------------


def author_initials(given_name: str) -> str:
    pieces = [part[0].upper() for part in re.findall(r"[A-Za-z]+", given_name)]
    return "".join(pieces)


def format_author(family: str, given: str) -> str:
    initials = author_initials(given)
    if family and initials:
        return f"{family}, {'. '.join(initials)}."
    if family:
        return family
    return given


# ---------------------------------------------------------------------------
# Payload parsers
# ---------------------------------------------------------------------------


def parse_crossref_payload(
    payload: dict[str, object] | None,
) -> dict[str, object] | None:
    """Normalize a CrossRef ``/works/{doi}`` response into the flat shape used
    by verify.py. Returns ``None`` if the payload has no ``message`` block."""
    message = as_dict((payload or {}).get("message"))
    if message is None:
        return None

    title_list = as_strings(message.get("title"))
    issued = as_dict(message.get("issued"))
    date_parts = as_list((issued or {}).get("date-parts"))
    first_part = date_parts[0] if date_parts else None
    year = None
    if isinstance(first_part, list) and first_part:
        year = as_int(first_part[0])

    authors: list[str] = []
    for author_obj in as_list(message.get("author")):
        author = as_dict(author_obj)
        if author is None:
            continue
        authors.append(
            format_author(
                as_str(author.get("family")) or "",
                as_str(author.get("given")) or "",
            )
        )

    update_to = [
        item for item in as_list(message.get("update-to")) if isinstance(item, dict)
    ]
    container_titles = as_strings(message.get("container-title"))
    publication_type = as_str(message.get("type")) or ""
    return {
        "title": title_list[0] if title_list else "",
        "year": year,
        "authors": authors,
        "journal": container_titles[0] if container_titles else "",
        "issn": as_strings(message.get("ISSN")),
        "type": publication_type,
        "update_to": update_to,
    }


def parse_semantic_scholar_payload(
    payload: dict[str, object] | None,
) -> dict[str, object] | None:
    """Normalize a Semantic Scholar ``/paper/DOI:{doi}`` response into the
    same shape as parse_crossref_payload. Returns ``None`` if payload is
    ``None``."""
    if payload is None:
        return None

    authors: list[str] = []
    for author_obj in as_list(payload.get("authors")):
        author = as_dict(author_obj)
        if author is None:
            continue
        name = as_str(author.get("name")) or ""
        parts = [part for part in name.split() if part]
        if not parts:
            continue
        family = parts[-1]
        given = " ".join(parts[:-1])
        authors.append(format_author(family, given))

    return {
        "title": as_str(payload.get("title")) or "",
        "year": as_int(payload.get("year")),
        "authors": authors,
        "journal": as_str(payload.get("venue")) or "",
        "type": "",
        "update_to": [],
    }


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def study_type(
    meta: dict[str, object],
    eupmc_hit: dict[str, object] | None,
) -> StudyType:
    """Map a parsed CrossRef meta (+ optional EuPMC hit) to a StudyType label.

    Precedence: CrossRef ``type`` field first (if it maps cleanly via
    TYPE_MAP), otherwise scan Europe PMC ``pubTypeList`` for keyword hits.
    """
    publication_type = as_str(meta.get("type"))
    if publication_type and publication_type in TYPE_MAP:
        return TYPE_MAP[publication_type]

    pub_type_list = as_dict((eupmc_hit or {}).get("pubTypeList"))
    pub_types = " ".join(as_strings((pub_type_list or {}).get("pubType"))).lower()
    for candidate, keywords in PUBTYPE_KEYWORDS.items():
        if any(keyword in pub_types for keyword in keywords):
            return candidate

    return "other"


def correction_types(
    update_to: list[object],
) -> tuple[list[object], list[str], bool]:
    """Parse CrossRef ``update-to`` array. Returns
    ``(retraction_notes, correction_type_strings, retracted_bool)``."""
    retraction_notes: list[object] = []
    corrections: list[str] = []
    retracted = False
    for item in update_to:
        update = as_dict(item)
        if update is None:
            continue
        update_type = as_str(update.get("type")) or ""
        if update_type in RETRACTION_TYPES:
            retracted = True
            retraction_notes.append(update)
        if update_type in CONCERN_TYPES:
            corrections.append(update_type)
    return retraction_notes, corrections, retracted
