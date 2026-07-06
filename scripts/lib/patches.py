"""Patch frontmatter loader for domain-conditional overrides.

Each patches/<domain>.md may begin with a YAML frontmatter block enclosed
by `---` delimiters. The body markdown is unchanged.

Schema:

    ---
    domain: <name>
    gap_types_dominant: [decision, mechanism, ...]
    evidence_base:
      primary: [...]
      secondary: [...]
      not_applicable: [rct, meta_of_rcts, ...]
    term_check_overrides:
      require_rct_or_meta: false
      require_primary_evidence_per_gap: 1
    default_search_sources: [crossref, semantic_scholar]
    protocol_defaults:
      inclusion: "..."
      exclusion: "..."
      outcomes: "..."
    ---

The `health` domain has patches/health.md whose frontmatter mirrors
``DEFAULT_PATCH`` (CLAUDE.md default tier in code form). ``DEFAULT_PATCH``
remains the merge base for every patch and the fallback for a
KNOWN_DOMAINS entry that has no patch file yet.

**Enforcement scope** (be honest about what each field actually does today):

- ``term_check_overrides.require_rct_or_meta`` — wired into term_check.py.
  When ``false`` (physics / animals / food-science), the ≥1 RCT/meta gate
  is skipped; ≥3 verified per gap still applies.
- ``KNOWN_DOMAINS`` — wired into bootstrap_topic.py ``--domain`` choices.
- ``REQUIRED_FIELDS_BY_GAP_TYPE`` — wired into verify.py declare-gap
  WARN messages and lint_review.py classification check.
- ``evidence_base.primary`` / ``.secondary`` / ``.not_applicable`` —
  **ADVISORY METADATA**. Today these fields are documentation only; no
  tool enforces "≥N primary evidence per gap" because store entries land
  almost universally as ``study_type="other"`` for non-clinical domains
  (CrossRef metadata doesn't distinguish ``experimental_measurement`` vs
  ``numerical_simulation`` etc). Wiring real enforcement requires
  extending the StudyType taxonomy or per-entry tagging — tracked as
  future work, not enforced now.
- ``default_search_sources`` — **ADVISORY**. search.py currently defaults
  to ``--source both`` (CrossRef + Semantic Scholar) regardless of patch.
  Future work could add EuPMC for health domain etc.
- ``protocol_defaults`` — wired into bootstrap_topic.py: inclusion / exclusion
  / outcomes prose rendered into research_log.md's protocol section (the user
  then specialises per topic). A missing key falls back to the ``_user 填_``
  placeholder.
- ``gap_types_dominant`` — informational; not used by any tool.
"""
from __future__ import annotations

import pathlib
from typing import TypedDict, cast

import yaml


class EvidenceBase(TypedDict, total=False):
    primary: list[str]
    secondary: list[str]
    not_applicable: list[str]


class TermCheckOverrides(TypedDict, total=False):
    require_rct_or_meta: bool
    require_primary_evidence_per_gap: int


class ProtocolDefaults(TypedDict, total=False):
    # Domain-stable protocol prose rendered into research_log.md's protocol
    # section by bootstrap_topic.py (the user then specialises per topic).
    inclusion: str
    exclusion: str
    outcomes: str


class PatchConfig(TypedDict, total=False):
    domain: str
    gap_types_dominant: list[str]
    evidence_base: EvidenceBase
    term_check_overrides: TermCheckOverrides
    default_search_sources: list[str]
    protocol_defaults: ProtocolDefaults


# Merge base for every patch + fallback for a KNOWN_DOMAINS entry with
# no patch file. Mirrors patches/health.md frontmatter (CLAUDE.md
# baseline — health / medicine / nutrition / exercise). Other domains
# override via their frontmatter; missing keys fall back to these.
DEFAULT_PATCH: PatchConfig = {
    "domain": "health",
    "gap_types_dominant": ["decision", "comparison", "mechanism", "safety"],
    "evidence_base": {
        "primary": ["meta", "rct", "large_cohort", "guideline"],
        "secondary": ["small_cohort", "case_control", "case_series"],
        "not_applicable": [],
    },
    "term_check_overrides": {
        "require_rct_or_meta": True,
        "require_primary_evidence_per_gap": 1,
    },
    "default_search_sources": ["crossref", "semantic_scholar"],
    "protocol_defaults": {
        "inclusion": (
            "人体研究：系统综述/meta、RCT、大型前瞻队列、临床指南与共识；"
            "英文为主；近 10–15 年优先，奠基性研究不限年代"
        ),
        "exclusion": (
            "纯动物/体外机制研究（仅在缺人体证据时作机制支撑）、无对照个案、"
            "未标注的预印本"
        ),
        "outcomes": (
            "以临床/功能结局为准（疗效、风险、剂量反应；硬终点优先于替代指标）"
        ),
    },
}

# Domains the workflow knows about. New patches must be added here so
# bootstrap_topic.py --domain can validate.
KNOWN_DOMAINS: tuple[str, ...] = (
    "health",
    "animals",
    "education-psychology",
    "physics",
    "food-science",
)


# Required sub-fields per gap_type. Subagent-derived from the 26-review
# survey (see docs/methodology_playbook.md §2). verify.py uses this to
# WARN on missing fields at declare-gap time; lint_review.py uses it to
# WARN/FAIL on published review.md gap classification.
REQUIRED_FIELDS_BY_GAP_TYPE: dict[str, tuple[str, ...]] = {
    "decision": ("population", "intervention", "comparator", "outcome"),
    "mechanism": ("phenomenon", "candidate_mechanisms", "evidence_types"),
    "comparison": ("item_a", "item_b", "dimensions", "comparison_level"),
    "methodology": (
        "process",
        "audience",
        "decision_question",
        "reference_standard",
    ),
    "safety": (
        "exposure",
        "at_risk_population",
        "adverse_outcomes",
        "threshold_ref",
    ),
    "diagnostic": (
        "differential_list",
        "discriminating_features",
        "reference_standard",
    ),
    "descriptive": ("phenomenon", "measurement_method", "population_setting"),
}


def _skill_root() -> pathlib.Path:
    """The skill root (ebr/), which ships the default patches/."""
    # ebr/scripts/lib/patches.py → ../../../ = ebr/
    return pathlib.Path(__file__).resolve().parent.parent.parent


def patches_dir() -> pathlib.Path:
    # Prefer a workspace-local patches/ (user-editable, survives skill reinstall);
    # fall back to the patches/ shipped with the skill.
    try:
        from . import project as _project

        ws = _project.project_root() / "patches"
        if ws.is_dir():
            return ws
    except Exception:
        pass
    return _skill_root() / "patches"


def patch_path(domain: str) -> pathlib.Path | None:
    """Resolve patches/<domain>.md, or None for any domain without a patch
    file. `health` now has patches/health.md (frontmatter mirrors
    DEFAULT_PATCH); a KNOWN_DOMAINS entry lacking a file still returns
    None and falls back to DEFAULT_PATCH in load_patch()."""
    path = patches_dir() / f"{domain}.md"
    return path if path.exists() else None


def load_frontmatter(path: pathlib.Path) -> dict:
    """Return parsed YAML frontmatter from a patch file, or {} if absent.

    Frontmatter is the leading `---\\n...\\n---\\n` block; missing block
    or parse failure returns {}."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end == -1:
        # Allow trailing `---` at EOF too.
        end = text.find("\n---", 4)
        if end == -1:
            return {}
    block = text[4:end]
    try:
        parsed = yaml.safe_load(block)
    except yaml.YAMLError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _merge(default: dict, override: dict) -> dict:
    """Shallow-merge override on top of default, recursing into nested dicts."""
    result = dict(default)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load_patch(domain: str) -> PatchConfig:
    """Return the effective PatchConfig for a domain.

    `health` (or unknown domain) returns DEFAULT_PATCH. Otherwise merges
    DEFAULT_PATCH with the parsed frontmatter so callers always see all
    keys (no KeyError on missing override fields)."""
    if domain not in KNOWN_DOMAINS:
        return cast(PatchConfig, dict(DEFAULT_PATCH))
    path = patch_path(domain)
    if path is None:
        # A KNOWN_DOMAINS entry without a patch file (health.md now exists,
        # so in practice this is the defensive fallback path only).
        config = cast(PatchConfig, dict(DEFAULT_PATCH))
        config["domain"] = domain
        return config
    front = load_frontmatter(path)
    merged = _merge(cast(dict, DEFAULT_PATCH), front)
    merged["domain"] = domain  # ensure correct, regardless of frontmatter
    return cast(PatchConfig, merged)


def requires_rct_or_meta(patch: PatchConfig) -> bool:
    """Whether term_check should still enforce ≥1 RCT/meta per gap.

    Default (health) is True. Physics / food-science / animals override
    to False — those domains don't routinely produce RCT/meta and use
    other primary evidence types."""
    return bool(
        patch.get("term_check_overrides", {}).get("require_rct_or_meta", True)
    )


# NOTE: `is_evidence_primary` / `primary_evidence_min_per_gap` /
# `default_search_sources` helpers were drafted then removed (Codex P2
# 2026-05-24). The corresponding frontmatter fields are kept as advisory
# documentation — see module docstring "Enforcement scope" for the
# honest accounting of what each field does and doesn't do today.
