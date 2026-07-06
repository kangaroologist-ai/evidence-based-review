# EBR — Evidence-Based Research (multi-gap)

A portable [Claude Skill](https://www.anthropic.com/news/skills) for producing rigorously-cited,
multi-round evidence reviews. Give it a topic that decomposes into several sub-questions
("gaps") — health/medicine/nutrition/exercise, companion-animal nutrition & veterinary
medicine, education/learning sciences, physics, food science, or any other empirically
answerable domain — and it runs the full pipeline: declare evidence gaps → search +
citation-network snowballing → verify each candidate against CrossRef → per-gap analyst
triage → synthesis → independent 3-reviewer gate → final draft.

Every `[@citation_key]` in the output resolves to a **verified** entry in
`references_store.json` — citations are never invented from memory or impression.

> **Single tightly-scoped proposition** (e.g. "does X work?")? Use the lighter sibling
> skill, `quick-research`, instead — EBR is for topics that genuinely fan out into
> multiple gaps.

## Quick start

```bash
pip install -r requirements.txt              # httpx[socks], pymupdf, pytest (Python 3.12+)
python scripts/setup.py --workspace <dir>    # scaffold a workspace, copy patches/
export HEALTH_REVIEW_ROOT="<dir>"            # every script reads this first
```

Full guide (dependencies, optional API keys, zero-key mode) in [`SETUP.md`](SETUP.md).
**EBR runs end-to-end with zero API keys** — CrossRef/OpenAlex use the free polite pool,
Semantic Scholar the shared pool; keys only raise rate limits.

The complete skill contract (trigger conditions, core principles, the 7-phase workflow,
gap-type field reference, review structure, quality gates) lives in [`SKILL.md`](SKILL.md).
It's written to be loaded and executed by Claude, not manually copy-pasted.

## Layout

| Path | Contents |
|---|---|
| `scripts/` | Workflow CLIs (search, verify, citation-network expansion, lint, render, reviewer orchestration…) |
| `patches/` | Per-domain rule patches (naming conventions, first-tier evidence, abbreviation glosses, known limitations) |
| `references/` | Prose-style and evidence-hierarchy reference docs |
| `SKILL.md` | Skill definition — triggers + full workflow contract |
| `SETUP.md` | Environment setup and API key configuration |

## Note on output language

Review *output* (prose, headings, abstracts) is written in Chinese per the domain
patches in `patches/`; the skill's own documentation (this README, `SKILL.md`,
`SETUP.md`) is in English. Adjust the patches if you want reviews in another language.

## License

No license file — all rights reserved by default.
