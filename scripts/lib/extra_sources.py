"""C8: extra REST search adapters — PubMed, bioRxiv/medRxiv, ClinicalTrials.gov.

All three functions return the same ``SearchHit`` shape used by ``search.py``
so results can be merged / deduped with the existing CrossRef + Semantic Scholar
pipeline.  Every function degrades gracefully: any HTTP error, timeout, non-200,
or JSON parse failure → one-line warning on stderr + empty list.

Public API
----------
search_pubmed(query, rows)         -> list[SearchHit]
search_biorxiv(query, rows)        -> list[SearchHit]
search_clinicaltrials(query, rows) -> list[SearchHit]
search_extra(query, rows, sources) -> list[SearchHit]  (dispatcher)

Endpoint choices
----------------
PubMed:
    NCBI E-utilities (public, no auth, polite-pool):
      esearch.fcgi → list of PMIDs
      esummary.fcgi → structured summaries including articleids
    DOI is extracted from the `articleids` array (idtype=="doi").
    Records with no DOI are skipped — the verify pipeline is DOI-based.
    source label: "pubmed"

bioRxiv / medRxiv:
    bioRxiv content API: https://api.biorxiv.org/details/{server}/{cursor}
    There is no keyword search endpoint in the public bioRxiv REST API
    (their fulltext search lives behind a non-public endpoint).  We use a
    cursor-based approach to retrieve recent preprints and filter by matching
    the query against title + abstract.  This is a best-effort heuristic —
    comprehensive bioRxiv discovery should go through PubMed (which now indexes
    many bioRxiv preprints) or the dedicated biorxiv MCP tool.
    DOI is taken directly from the preprint record.
    source label: "biorxiv"

ClinicalTrials.gov:
    API v2: https://clinicaltrials.gov/api/v2/studies
    Trials use NCT identifiers, not DOIs.  We emit hits with ``doi`` set to
    ``"nct:<NCTId>"`` (e.g. ``"nct:NCT01234567"``).
    Rationale for keeping NCT ids: the verify pipeline does reject non-DOI
    strings at the CrossRef lookup stage, but the caller (search.py auto-add)
    uses the doi field only as a dedup key before writing to the store, and the
    store itself stores whatever string is in the field.  Callers that need real
    DOIs can filter out "nct:" prefixes.  This is documented so the integration
    author can decide to skip instead if preferred.
    source label: "clinicaltrials"
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

# SearchHit lives in the search module (tools/search.py).  We import it here
# so callers can rely on this module for the type without importing search.
# Avoid circular imports: extra_sources is a library module, search is the CLI.
if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Re-export SearchHit so test files can import from one place.
# We use a lazy import because search.py also imports from lib/ (including
# this file when the integration patch is applied) — keep this import deferred.
# ---------------------------------------------------------------------------

def _hit_cls():  # type: ignore[return]  # pragma: no cover
    from search import SearchHit  # noqa: PLC0415
    return SearchHit


# ---------------------------------------------------------------------------
# HTTP helper — reuse apis.get_json so HostGate rate-limiting and caching
# apply automatically.  Fall back to a bare urllib GET only if apis is not
# importable (should never happen in this codebase).
# ---------------------------------------------------------------------------

def _get_json(url: str, params: dict | None = None) -> dict | None:
    try:
        from lib import apis  # noqa: PLC0415
        return apis.get_json(url, params=params)
    except Exception as exc:  # noqa: BLE001
        # Last-resort fallback so unit tests that only mock urllib work.
        import json
        import urllib.request
        import urllib.parse
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                return json.loads(resp.read())
        except Exception as exc2:  # noqa: BLE001
            print(f"[WARN] extra_sources HTTP fallback failed: {exc2}", file=sys.stderr)
            return None


# ---------------------------------------------------------------------------
# Shared SearchHit constructor — avoids importing search at module level.
# ---------------------------------------------------------------------------

def _make_hit(
    *,
    doi: str,
    title: str,
    year: int,
    authors: list[str],
    source: str,
    cited_by_count: int = 0,
    publication_type: str = "",
) -> object:
    """Return a SearchHit instance (imported lazily)."""
    from search import SearchHit  # noqa: PLC0415

    def _family(name: str) -> str:
        if "," in name:
            return name.split(",", 1)[0].strip()
        parts = name.split()
        return parts[-1] if parts else ""

    first_author_family = _family(authors[0]) if authors else ""
    # SearchHit.source is NOT a field — search.py's SearchHit has no `source`
    # field; source is used only in auto-add (stored to the refs entry).
    # We carry it as a plain attribute via a thin wrapper dict-union approach:
    # actually SearchHit is a frozen dataclass so we cannot add fields.
    # We return the hit as-is and store source separately in search_extra().
    # BUT — looking at SearchHit: it has no `source` field.  The `source`
    # label is written by _auto_add in search.py directly from the `--source`
    # CLI arg, not from the hit.  So extra_sources hits fed through the same
    # auto-add path would always get source="search".  For now we wrap hits in
    # a lightweight subclass-compatible namedtuple approach: add `source` as a
    # plain attribute post-construction via object.__setattr__ on a copy, BUT
    # frozen dataclasses disallow this.
    #
    # Pragmatic decision: define a local ExtraHit dataclass that EXTENDS
    # SearchHit by adding `source`.  The merge/dedup in search.py only reads
    # .doi and .cited_by_count — both present.  _print_hits reads doi/title/
    # year/cited_by_count/publication_type/first_author_family — all present.
    # _auto_add reads .doi/.title/.year/.authors — all present.  The `source`
    # field is read only from ExtraHit by search_extra() callers that want it.
    #
    # This is defined here (not at module level) to keep the lazy import pattern.
    import dataclasses  # noqa: PLC0415

    @dataclasses.dataclass(frozen=True)
    class ExtraHit(SearchHit):  # type: ignore[misc]
        source: str = ""

    return ExtraHit(
        doi=doi,
        title=title,
        year=year,
        authors=authors,
        first_author_family=first_author_family,
        cited_by_count=cited_by_count,
        publication_type=publication_type,
        source=source,
    )


# ---------------------------------------------------------------------------
# PubMed
# ---------------------------------------------------------------------------

_EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_NCBI_TOOL = "health-review"
_NCBI_EMAIL = "kangaroologician@gmail.com"


def search_pubmed(query: str, rows: int) -> list:
    """Search PubMed via NCBI E-utilities; return list[SearchHit].

    Steps:
      1. esearch → list of PMIDs (up to `rows`)
      2. esummary → per-PMID metadata including DOI
    Records without a DOI are skipped.
    """
    try:
        esearch_params = {
            "db": "pubmed",
            "retmode": "json",
            "term": query,
            "retmax": rows,
            "tool": _NCBI_TOOL,
            "email": _NCBI_EMAIL,
        }
        esearch = _get_json(f"{_EUTILS_BASE}/esearch.fcgi", esearch_params)
        if not isinstance(esearch, dict):
            return []
        result = esearch.get("esearchresult")
        if not isinstance(result, dict):
            return []
        id_list = result.get("idlist") or []
        if not id_list:
            return []

        esummary_params = {
            "db": "pubmed",
            "retmode": "json",
            "id": ",".join(str(pmid) for pmid in id_list),
            "tool": _NCBI_TOOL,
            "email": _NCBI_EMAIL,
        }
        esummary = _get_json(f"{_EUTILS_BASE}/esummary.fcgi", esummary_params)
        if not isinstance(esummary, dict):
            return []
        result_map = esummary.get("result")
        if not isinstance(result_map, dict):
            return []

        hits = []
        for pmid in id_list:
            entry = result_map.get(str(pmid))
            if not isinstance(entry, dict):
                continue

            # Extract DOI from articleids
            doi = ""
            for aid in entry.get("articleids") or []:
                if not isinstance(aid, dict):
                    continue
                if aid.get("idtype") == "doi":
                    doi = str(aid.get("value") or "").strip().lower()
                    break
            if not doi:
                continue  # DOI-less records are unusable downstream

            title = str(entry.get("title") or "").strip()
            if not title:
                continue

            # Year: sortpubdate "YYYY/MM/DD HH:MM" or pubdate "YYYY Mon DD"
            year = 0
            for date_field in ("sortpubdate", "pubdate", "epubdate"):
                raw_date = str(entry.get(date_field) or "")
                if raw_date:
                    first_token = raw_date.split("/")[0].split(" ")[0]
                    try:
                        year = int(first_token)
                        break
                    except ValueError:
                        continue

            # Authors: list of {name: "Family I"} objects
            authors: list[str] = []
            for auth in entry.get("authors") or []:
                if isinstance(auth, dict):
                    name = str(auth.get("name") or "").strip()
                    if name:
                        authors.append(name)

            pub_type_list = entry.get("pubtype") or []
            publication_type = (
                str(pub_type_list[0]).lower() if pub_type_list else ""
            )

            hits.append(
                _make_hit(
                    doi=doi,
                    title=title,
                    year=year,
                    authors=authors,
                    source="pubmed",
                    cited_by_count=0,
                    publication_type=publication_type,
                )
            )
        return hits
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] search_pubmed failed: {exc}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# bioRxiv / medRxiv
# ---------------------------------------------------------------------------

_BIORXIV_BASE = "https://api.biorxiv.org"
# The public API does not have a keyword search endpoint.  We fetch the most
# recent preprints (cursor=0 from today) and do in-process title filtering.
# This is a best-effort complement; for thorough bioRxiv retrieval use PubMed
# (which indexes bioRxiv preprints) or the dedicated biorxiv MCP.
_BIORXIV_DETAIL_ENDPOINT = "/details/{server}/2000-01-01/3000-01-01/{cursor}/json"
_BIORXIV_PAGE_SIZE = 100  # API always returns 100 per cursor page


def _biorxiv_search_server(query: str, rows: int, server: str) -> list:
    """Fetch recent preprints from one server and filter by query terms."""
    query_terms = [t.lower() for t in query.split() if len(t) >= 3]
    hits = []
    cursor = 0
    fetched = 0

    while fetched < rows * 10:  # scan up to 10 pages to find `rows` matches
        url = _BIORXIV_BASE + _BIORXIV_DETAIL_ENDPOINT.format(
            server=server, cursor=cursor
        )
        payload = _get_json(url)
        if not isinstance(payload, dict):
            break
        collection = payload.get("collection") or []
        if not isinstance(collection, list) or not collection:
            break

        for item in collection:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            abstract = str(item.get("abstract") or "").strip()
            doi = str(item.get("doi") or "").strip().lower()
            if not doi or not title:
                continue
            combined = (title + " " + abstract).lower()
            if query_terms and not any(term in combined for term in query_terms):
                continue
            # biorxiv returns "YYYY-MM-DD" in date field
            raw_date = str(item.get("date") or "")
            year = 0
            if raw_date:
                try:
                    year = int(raw_date.split("-")[0])
                except ValueError:
                    pass
            authors_raw = str(item.get("authors") or "")
            authors = [a.strip() for a in authors_raw.split(";") if a.strip()]
            category = str(item.get("category") or "").lower()
            hits.append(
                _make_hit(
                    doi=doi,
                    title=title,
                    year=year,
                    authors=authors,
                    source="biorxiv",
                    cited_by_count=0,
                    publication_type=category,
                )
            )
            if len(hits) >= rows:
                return hits

        fetched += len(collection)
        # Advance cursor for next page
        cursor += _BIORXIV_PAGE_SIZE
        # API message.total might limit us
        messages = payload.get("messages") or []
        if isinstance(messages, list) and messages:
            msg = messages[0] if isinstance(messages[0], dict) else {}
            total = msg.get("total") or 0
            if cursor >= int(total):
                break

    return hits[:rows]


def search_biorxiv(query: str, rows: int) -> list:
    """Search bioRxiv + medRxiv; return list[SearchHit].

    Uses the public bioRxiv content API (no keyword search endpoint exists).
    Fetches recent preprints and filters by query term occurrence in title/abstract.
    Returns [] gracefully on any error.
    """
    try:
        biorxiv_hits = _biorxiv_search_server(query, rows, "biorxiv")
        remaining = rows - len(biorxiv_hits)
        if remaining > 0:
            medrxiv_hits = _biorxiv_search_server(query, remaining, "medrxiv")
        else:
            medrxiv_hits = []
        # Dedup by doi
        seen: set[str] = set()
        out = []
        for hit in biorxiv_hits + medrxiv_hits:
            doi = getattr(hit, "doi", "")
            if doi and doi not in seen:
                seen.add(doi)
                out.append(hit)
        return out[:rows]
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] search_biorxiv failed: {exc}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# ClinicalTrials.gov
# ---------------------------------------------------------------------------

_CTGOV_BASE = "https://clinicaltrials.gov/api/v2"


def search_clinicaltrials(query: str, rows: int) -> list:
    """Search ClinicalTrials.gov v2 API; return list[SearchHit].

    DOI handling: trials have NCT identifiers, not DOIs.  We emit hits with
    ``doi = "nct:<NCTId>"`` (e.g. ``"nct:NCT01234567"``).  The verify pipeline
    will reject these as non-DOI strings at CrossRef lookup, but they can be
    used for deduplication and manual inspection.  Callers that need only real
    DOIs should filter on ``hit.doi.startswith("nct:")``.

    Returns [] gracefully on any error.
    """
    try:
        params = {
            "query.term": query,
            "pageSize": rows,
            "format": "json",
        }
        payload = _get_json(f"{_CTGOV_BASE}/studies", params)
        if not isinstance(payload, dict):
            return []
        studies = payload.get("studies") or []
        if not isinstance(studies, list):
            return []

        hits = []
        for study in studies:
            if not isinstance(study, dict):
                continue
            protocol = study.get("protocolSection") or {}
            if not isinstance(protocol, dict):
                continue

            id_module = protocol.get("identificationModule") or {}
            nct_id = str(id_module.get("nctId") or "").strip()
            if not nct_id:
                continue
            doi_field = f"nct:{nct_id}"

            brief_title = str(id_module.get("briefTitle") or "").strip()
            official_title = str(id_module.get("officialTitle") or "").strip()
            title = official_title or brief_title
            if not title:
                continue

            # Start date
            status_module = protocol.get("statusModule") or {}
            start_date_struct = status_module.get("startDateStruct") or {}
            raw_date = str(start_date_struct.get("date") or "")
            year = 0
            if raw_date:
                try:
                    year = int(raw_date.split("-")[0].split("/")[0])
                except ValueError:
                    pass

            # Sponsors / contacts as proxy for "authors"
            sponsor_module = protocol.get("sponsorCollaboratorsModule") or {}
            lead_sponsor = sponsor_module.get("leadSponsor") or {}
            sponsor_name = str(lead_sponsor.get("name") or "").strip()
            authors = [sponsor_name] if sponsor_name else []

            phase_list = (protocol.get("designModule") or {}).get("phases") or []
            publication_type = "; ".join(phase_list).lower() if phase_list else "trial"

            hits.append(
                _make_hit(
                    doi=doi_field,
                    title=title,
                    year=year,
                    authors=authors,
                    source="clinicaltrials",
                    cited_by_count=0,
                    publication_type=publication_type,
                )
            )
        return hits
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] search_clinicaltrials failed: {exc}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_ADAPTER_NAMES: frozenset[str] = frozenset({"pubmed", "biorxiv", "clinicaltrials"})


def _get_adapter(name: str):
    """Return the adapter function for `name`, looked up at call time.

    Late binding is intentional: monkeypatching the module-level names in tests
    must be reflected here without rebuilding a static dict.
    """
    import sys as _sys
    module = _sys.modules[__name__]
    return getattr(module, f"search_{name}", None)


def search_extra(query: str, rows: int, sources: list[str]) -> list:
    """Dispatch to the requested extra adapters and concatenate results.

    Deduplication by DOI is the caller's responsibility (same contract as
    _merge_dedup in search.py).  Unknown source labels are silently skipped.

    Parameters
    ----------
    query:   keyword query string
    rows:    max hits PER SOURCE (not total)
    sources: list of source labels, e.g. ["pubmed", "biorxiv", "clinicaltrials"]
    """
    out = []
    for src in sources:
        fn = _get_adapter(src)
        if fn is None or src not in _ADAPTER_NAMES:
            print(f"[WARN] search_extra: unknown source '{src}'", file=sys.stderr)
            continue
        out.extend(fn(query, rows))
    return out
