"""Filesystem layout helpers for a single review topic directory.

Centralizes all paths that used to be hardcoded across reviewer / render /
workflow_status / self_review tooling. The goal is one source of truth so
the layout can evolve without scattering string literals.

Layout (post-2026-05 reorg):

    reviews/<topic>/
    ├── review.md                  ← topic body
    ├── research_log.md            ← human-facing audit log
    ├── references_store.json
    ├── references_store.json.lock ← refs.py file lock (toplevel)
    ├── <主词>综述.pdf              ← delivered PDF (Chinese name)
    ├── references/                ← per-DOI entry JSON
    ├── figures/
    ├── notes/                     ← per-round, per-gap analyst notes
    ├── reviewers/                 ← Phase 6 评审循环
    │   ├── prompt_round_R.md
    │   ├── round_R_N.md           ← N ∈ {1, 2, 3}; was reviewer_round_R_N.md
    │   ├── revision_log.md
    │   └── failure_report.md
    ├── drafts/                    ← Phase 5/7 写作准备 + 收尾
    │   ├── gaps_draft.md          ← optional
    │   ├── outline_draft.md       ← optional
    │   ├── self_review.md
    │   └── signoff.md
    └── meta/                      ← 机器副产物
        └── citation_stats.md      ← render_refs.py auto-overwrites

Toplevel files that stay at toplevel: review.md, research_log.md,
references_store.json (+ .lock), <主词>综述.pdf, plus references/ figures/
notes/ subdirs.
"""
from __future__ import annotations

import re
from pathlib import Path


# Subdirectory names — exported so callers can construct artifact entries
# (e.g. `_exists_entry(topic_dir, f"{REVIEWERS_DIRNAME}/round_1_1.md", "file")`).
REVIEWERS_DIRNAME = "reviewers"
DRAFTS_DIRNAME = "drafts"
META_DIRNAME = "meta"

# Matches reviewer output files under reviewers/ — captures (round_num, reviewer_num).
REVIEWER_FILE_RE = re.compile(r"^round_(\d+)_(\d+)\.md$")


def reviewers_dir(topic_dir: Path) -> Path:
    return topic_dir / REVIEWERS_DIRNAME


def drafts_dir(topic_dir: Path) -> Path:
    return topic_dir / DRAFTS_DIRNAME


def meta_dir(topic_dir: Path) -> Path:
    return topic_dir / META_DIRNAME


def reviewer_round_path(topic_dir: Path, round_num: int, reviewer_num: int) -> Path:
    return reviewers_dir(topic_dir) / f"round_{round_num}_{reviewer_num}.md"


def reviewer_prompt_path(topic_dir: Path, round_num: int) -> Path:
    return reviewers_dir(topic_dir) / f"prompt_round_{round_num}.md"


def revision_log_path(topic_dir: Path) -> Path:
    return reviewers_dir(topic_dir) / "revision_log.md"


def failure_report_path(topic_dir: Path) -> Path:
    return reviewers_dir(topic_dir) / "failure_report.md"


def self_review_path(topic_dir: Path) -> Path:
    return drafts_dir(topic_dir) / "self_review.md"


def signoff_path(topic_dir: Path) -> Path:
    return drafts_dir(topic_dir) / "signoff.md"


def gaps_draft_path(topic_dir: Path) -> Path:
    return drafts_dir(topic_dir) / "gaps_draft.md"


def outline_draft_path(topic_dir: Path) -> Path:
    return drafts_dir(topic_dir) / "outline_draft.md"


def citation_stats_path(topic_dir: Path) -> Path:
    return meta_dir(topic_dir) / "citation_stats.md"


def ensure_subdirs(topic_dir: Path) -> None:
    """Create reviewers/ drafts/ meta/ if missing. Idempotent."""
    for name in (REVIEWERS_DIRNAME, DRAFTS_DIRNAME, META_DIRNAME):
        (topic_dir / name).mkdir(exist_ok=True)
