"""claim_id sidecars — by-construction assertion mapping (plan v3.1 F1 / spec §0.6).

The writer tags each factual declarative clause with a ``<!-- claim:CID -->``
inline HTML comment next to its citation. The comment is invisible when rendered
(HTML/Markdown swallow comments), and ``strip()`` removes it outright for the
delivered artifact (spec §0.6: render 时剥离，交付正文不含 ID). ``lint_review``
uses ``has()`` to detect whether the infra is *active* for a review (opt-in: no
sidecars → the F3 coverage check stays silent so legacy reviews don't break).
"""
from __future__ import annotations

import re

# A sidecar is `<!-- claim:CID -->` (factual) or `<!-- claim:CID type:inference -->`
# (裁决/综合/过渡句, spec §0.6.a — grounding-exempt). CLAIM_ID_RE captures the CID
# only (one group, so findall stays string-valued); the optional type is parsed by
# claim_entries().
CLAIM_ID_RE = re.compile(r"<!--\s*claim:([\w\-]+)(?:\s+type:\w+)?\s*-->")
_CLAIM_FULL_RE = re.compile(r"<!--\s*claim:([\w\-]+)(?:\s+type:(\w+))?\s*-->")


def has(text: str) -> bool:
    """True if review.md carries any claim_id sidecar (F1 infra active)."""
    return bool(CLAIM_ID_RE.search(text))


def strip(text: str) -> str:
    """Remove every claim_id sidecar (render-time strip for delivery)."""
    return CLAIM_ID_RE.sub("", text)


def ids_in(text: str) -> list[str]:
    return CLAIM_ID_RE.findall(text)


def claim_entries(text: str) -> list[tuple[str, str]]:
    """(claim_id, type) for each sidecar; type defaults to 'factual'."""
    return [(m.group(1), m.group(2) or "factual") for m in _CLAIM_FULL_RE.finditer(text)]
