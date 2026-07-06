from __future__ import annotations

import os
import pathlib
import re

_SAFE_RE = re.compile(r"[^\w.\-]")


def project_root() -> pathlib.Path:
    env_root = os.getenv("HEALTH_REVIEW_ROOT")
    if env_root:
        return pathlib.Path(env_root).resolve(strict=False)

    here = pathlib.Path(__file__).resolve(strict=False)
    for candidate in (here, *here.parents):
        has_tools = (candidate / "tools").is_dir()
        has_reviews = (candidate / "reviews").is_dir()
        has_claude = (candidate / "CLAUDE.md").exists()
        if has_tools and (has_reviews or has_claude):
            return candidate

    fallback = here.parent.parent.parent
    # EBR skill layout (scripts/lib/project.py, no tools/+reviews marker): the
    # walk can only reach the skill install dir itself. Writing reviews/state/
    # tmp there pollutes a reinstall-wiped, per-version dir. Fail loud instead —
    # the user must point HEALTH_REVIEW_ROOT at a real workspace.
    if (fallback / "SKILL.md").exists():
        raise RuntimeError(
            "HEALTH_REVIEW_ROOT is not set and no project workspace was found. "
            "Run:  python scripts/setup.py init --workspace <dir>  then  "
            'export HEALTH_REVIEW_ROOT="<dir>"  (see SETUP.md).'
        )
    return fallback


def topic_dir(topic: str) -> pathlib.Path:
    return project_root() / "reviews" / topic


def topic_tmp(topic: str) -> pathlib.Path:
    return project_root() / "tmp" / topic


def blocklist_path() -> pathlib.Path:
    return project_root() / "state" / "blocklist.json"


def review_path(topic_dir: str | pathlib.Path) -> pathlib.Path:
    """The review markdown, named after the topic (not a generic 'review.md').

    Prefers ``<topic>/<topic>.md``; falls back to a legacy ``review.md`` if
    that's what is on disk; defaults to the topic-named path for a fresh topic
    so the deliverable the user receives is identifiable by its question."""
    topic_dir = pathlib.Path(topic_dir)
    named = topic_dir / f"{topic_dir.name}.md"
    if named.exists():
        return named
    legacy = topic_dir / "review.md"
    if legacy.exists():
        return legacy
    return named


def safe_doi(doi: str) -> str:
    return _SAFE_RE.sub("_", doi.lower())


def to_rel(path_value: str | pathlib.Path | None) -> str | None:
    if path_value is None:
        return None

    root = project_root().resolve(strict=False)
    path = pathlib.Path(path_value)
    resolved = (
        path.resolve(strict=False)
        if path.is_absolute()
        else (root / path).resolve(strict=False)
    )
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(
            f"path {resolved} is outside project root {root}"
        ) from exc


def to_abs(path_value: str | pathlib.Path | None) -> pathlib.Path | None:
    if path_value is None:
        return None

    path = pathlib.Path(path_value)
    if path.is_absolute():
        return path.resolve(strict=False)
    return (project_root() / path).resolve(strict=False)

