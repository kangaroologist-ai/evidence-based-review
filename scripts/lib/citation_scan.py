"""Shared citation + tool-managed-marker scanning.

lint_review, render_refs and gaps_status MUST agree byte-for-byte on (a) what
an in-text `[@key]` citation looks like and (b) which marker blocks (PRISMA
flow + References) to strip before scanning — otherwise lint and render
disagree on "which keys are cited" and you get the classic regression:
lint PASS but render reports a missing key after a re-render (and vice versa).

These regexes used to be copy-pasted (with subtly different names) across six
tools. Centralizing them here removes the drift surface. workflow_status.py
keeps its own *capturing* variants on purpose (it extracts block contents for
the status panel, not just presence/removal).
"""
from __future__ import annotations

import re

# Matches a single `[@key]` or one key inside a group like `[@k1; @k2]`.
CITE_RE = re.compile(r"@([A-Za-z][A-Za-z0-9]*)")

# Tool-managed marker blocks written by render_refs.
REFS_MARK_RE = re.compile(r"<!-- refs:start -->.*?<!-- refs:end -->", re.S)
PRISMA_MARK_RE = re.compile(
    r"<!-- prisma-flow:start -->.*?<!-- prisma-flow:end -->", re.S
)


def strip_tool_managed_blocks(text: str) -> str:
    """Remove the PRISMA-flow and References marker blocks before scanning.

    Without this, any `@key`-shaped string the tool itself writes into those
    blocks (instructional text, future template additions) leaks into the
    citation set and trips lint/render with a false missing-key error.
    """
    text = PRISMA_MARK_RE.sub("", text)
    text = REFS_MARK_RE.sub("", text)
    return text


def scan_used_keys(text: str) -> set[str]:
    """The set of citation keys actually used in body prose (markers stripped)."""
    return set(CITE_RE.findall(strip_tool_managed_blocks(text)))
