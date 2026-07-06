---
name: ebr
description: "Produce a comprehensive, evidence-based research review on a topic that spans SEVERAL sub-questions, with verified citations across any domain (health, medicine, nutrition, veterinary, education, physics, food science, ML, economics, policy, materials…). Use EBR when the question is broad enough to decompose into multiple distinct gaps — 'how do I maintain X?', 'what drives Y and what can I do about it?', 'compare A vs B across these dimensions', 'what's the full evidence picture on Z?' — and you want a multi-round, gap-driven review with a 3-reviewer quality gate and a strict no-hallucinated-citations contract (every [@key] resolves to a verified entry in references_store.json). Trigger even when phrased casually. For a SINGLE tightly-scoped proposition ('does X work?'), prefer the lighter quick-research skill. Do NOT use for questions with no academic literature."
---

# EBR — Evidence-Based Research (multi-gap)

A topic is decomposed into several **gaps** (sub-questions), each iterated over 3+ rounds of search → snowball → analyst triage, then synthesized into one decision-grade review and gated by 3 independent reviewers. Every `[@citation_key]` in the body must resolve to a **verified** entry — no citation from memory, ever.

> **Single-question?** If the user's ask is one tight proposition, use `quick-research` instead. EBR is for topics that genuinely fan out into multiple gaps.

---

## Setup (once) — see `SETUP.md` for the full guide

EBR scripts write into a **workspace** directory (holds `reviews/ state/ tmp/ patches/`). Establish it once:

```bash
pip install -r requirements.txt              # httpx[socks], pymupdf, pytest (Python 3.12+)
python scripts/setup.py --workspace <dir>    # scaffolds the workspace, copies patches/
export HEALTH_REVIEW_ROOT="<dir>"            # every script honors this first
```

**API keys are all OPTIONAL — EBR runs end-to-end with zero keys** (CrossRef + OpenAlex use the free polite/common pool via your email; Semantic Scholar uses the shared pool). Keys only raise rate limits. Set an email and optional keys with `setup.py --email/--openalex-key/--semantic-scholar-key`. Full registration + fallback details in `SETUP.md`.

All commands below are `python scripts/*.py …`, run from the skill dir with `HEALTH_REVIEW_ROOT` set (or `python3` in sandboxes without anaconda).

---

## Outputs — what to hand the user

Deliver exactly two files: **`reviews/<topic>/<topic>.md`** and **`reviews/<topic>/<topic>.html`** (`md_to_html.py`, for viewers that don't render inline LaTeX). Don't dump anything else at the user unprompted.

> `<topic>.md` means the file *named after the topic* (e.g. `reviews/钙与骨折/钙与骨折.md`), not a literal `review.md` — so the deliverable is identifiable by its question. `bootstrap_topic.py` creates this topic-named skeleton; `lint_review` / `render_refs` / `workflow_status` locate it automatically via `project.review_path()`. You just `Write` your review to that path.

Everything else is workspace-internal:
- **Load-bearing — keep, but show only on request.** `references_store.json` + `references/*.json` are the no-hallucination audit trail (every `[@key]` resolves here; lint/render read them). `research_log.md` holds the protocol + per-round record (term_check/lint read it). These are *not* logs to delete — they're the correctness substrate.
- **Scratch — ignore; safe to delete after delivery.** `notes/`, `reviewers/`, `drafts/`, `meta/`, and `tmp/<topic>/` (fetched abstract working copies).
- **Cache — invisible infra, leave it.** API responses cache under `~/.cache/health-review` (keyed by URL, TTL'd) — makes re-runs cheap and dodges rate limits. Fetching abstract *text* is necessary: analysts extract real numbers from real abstracts, which is what keeps citations non-hallucinated. Full-text PDFs are fetched on-demand only (reviewer fidelity checks), never bulk-cached.

---

## Core principles

1. **Structure around actionable claims; evidence supports, it isn't the skeleton.** Section titles are decisions ("morning fasted is the single most reliable measurement window"), not evidence-type or mechanism categories. Readers want "what to do / how to think," not "what studies exist."
2. **Few, strong citations.** Each core claim gets 1–3 best sources (meta > RCT > large cohort > review > mechanism). `verified ≠ must-cite` — extra verified entries can stay in the store, uncited.
3. **Abstract gives operational verdicts** — tell the reader what to do or think, not "what was studied."
4. **Surface conflicts and boundaries** — main controversies, competing explanations, limited populations.
5. **Say so explicitly when evidence is thin** — don't paper over uncertainty.
6. **Textbook consensus may be stated plainly**, marked "standard textbook knowledge," when no single citable source exists.

Prose-level execution rules (decision-first first sentences, numbers-with-meaning, ≤3 cites/point, **term/abbreviation Chinese-gloss on first use = MANDATORY**, limitation-says-what-it-blocks): see `references/prose-style.md`.

## Evidence hierarchy

Roughly: systematic reviews / meta-analyses > large RCTs > major prospective cohorts > clinical guidelines / consensus > mechanistic / animal (only when human data is absent or to support mechanism). `study_type="other"` ≠ weak evidence (metadata is just coarse — name the design in prose). Domain-specific tiers (which institutional reports / handbooks count as first-tier; relative "large cohort" thresholds) live in `patches/<domain>.md` and `references/evidence-tiers.md`.

## Domain patches

Every domain has a patch (`patches/<domain>.md`) with naming rules, first-tier-evidence extensions, "large cohort" thresholds, abbreviation glosses, and known limits. The base workflow is domain-agnostic; the patch overrides it (patch wins on conflict). Read the matching patch before starting.

| Domain | Patch |
|---|---|
| health / medicine / nutrition / exercise | `patches/health.md` (default baseline) |
| companion animals / veterinary | `patches/animals.md` |
| education / learning sciences / dev-ed psychology | `patches/education-psychology.md` |
| physics | `patches/physics.md` |
| food science | `patches/food-science.md` |
| other (sociology / economics / ML / materials …) | create `patches/<domain>.md` — template: ① naming rule ② first-tier extension ③ "large cohort" threshold ④ abbreviation glosses ⑤ known limits. Note which patch you loaded in research_log Round 1. |

`term_check.py` reads each patch's frontmatter (e.g. `require_rct_or_meta`: true for health/education, false for physics/animals/food-science which don't routinely produce RCTs).

---

## Suggested structure (all EBR reviews)

Narrative + decision + mechanism composite. Section order: ① Title ② Abstract (3 prose paragraphs: overall verdict / branch-by-goal / operational bottom line) ③ N proposition-titled sections (claim → key evidence → boundary) ④ Actionable recommendations table (branched by reader goal) ⑤ **§Limitations & controversies** (write "what this blocks," not a polite closer) ⑥ **§Methods** (at the END, before References: domain / search sources / Wohlin snowballing / PRISMA flow funnel [auto-appended by render_refs] / tool list) ⑦ References (auto-generated). Methods + limitations go last so a reviewer can audit them without interrupting the main prose.

Format: APA. In-text `[@key]`; References generated by `render_refs.py`. Math in LaTeX (`$...$` / `$$...$$`), never ASCII/Unicode spelling. Self-made figures → SVG into the topic's `figures/`.

---

## 7-phase workflow (fire-and-forget)

The main thread runs straight through; it does NOT stop at phase boundaries to wait. The user can interrupt anytime. No blocking `AskUserQuestion` inside a phase — async questions go through markdown artifacts (gaps_draft / outline_draft).

### Phase 1 — diagnose + bootstrap + protocol
1. Dedup: `python scripts/search_existing.py "<keywords>"` — hit → continue an existing topic; miss → new.
2. `python scripts/bootstrap_topic.py "<topic>" --domain {health|animals|education-psychology|physics|food-science}` — creates `reviews/<topic>/` + a research_log.md with a protocol stub + empty store. The domain binds the patch.
3. Fill the protocol stub (inclusion/exclusion criteria, outcomes, search-source declaration). Leave `_user 填_` placeholders only where genuinely unknown (lint warns).

### Phase 2 / 3 / 4 — Round 1 / 2 / 3 (same 9 steps each, logged to research_log)
1. **Declare gaps** — `verify.py --declare-gap gap-N "<desc>" --gap-type {decision|mechanism|comparison|methodology|safety|diagnostic|descriptive} --<subfields> --round R`. Subfields required per type (see gap_type table). Optional `--depends-on gap-M`, `--subgap-of gap-M`, `--secondary-type X`.
2. **Seed known DOIs** — `verify.py --add DOI TITLE YEAR AUTHORS --gap gap-N --source seed --round R`. Never hand-type DOIs from memory — copy from the source/landing page, or `search.py "<title>"` first. On `[ERROR] title_mismatch` (exit 2), verify.py prints candidate correct DOIs in stderr — pick the right one and re-add; don't reflexively `--force-mismatch` (a DOI pointing at another paper is the common case).
3. **Search** — `search.py "<query>"` (default `--source both` = CrossRef + Semantic Scholar). **Discovery-first**: run without `--auto-add`, eyeball titles, then `verify.py --add` the hits. Bounded auto-add (`--auto-add <topic> --gap gap-N --max-add N --rows 8`) only when you must; it's noisy (~75% reject rate unbounded).
4. **Genealogy (snowball)** — `genealogy.py <topic> --gap gap-N --round R --parallel 3`. OpenAlex ancestors + descendants, chained verify + abstract fetch. **Every declared round needs ≥1 genealogy entry.** Cap: early rounds wide (default 15, broad topics 20–25); final/confirmation round ≈ current eligible/10 (saturation triggers at new-verified/eligible ≤ 10%).
5. **Fetch missing abstracts** — `fetch.py <topic> --include abstract --parallel 3`.
6. **Notes per gap** — `notes.py <topic> --round R --force` (splits markdown per gap; the analyst return-schema is auto-injected into each notes file's header).
7. **Analyst subagent per gap** (the key step) — main thread, in ONE message, spawns one Opus subagent per gap (`model: "opus"`, self-contained prompt). Each reads `notes/round-R/gap-X.md` and returns ≤500-word structured analysis: ① 5–8 strongest sources (`[@key]` + key number + one-line claim) ② study_type distribution ③ conflicts ("X and Y disagree on … because …") ④ new-gap candidate (Yes → describe / No) ⑤ four lists (cite_recommend / exclude_recommend / keep_uncited / uncertain, one line + reason each) ⑥ overlap/over-broad judgment ⑦ per-entry verdicts → `notes/round-R/gap-X.annotated.md`. Tool discipline: the analyst only Reads the one notes file + Writes one annotated.md; it runs NO workflow tools.
8. **Post-round triage** — read all analyst summaries; run `exclude.py <topic> DOI "reason"` on exclude_recommend, `regap.py <topic> DOI gap-M` on misfiled; any analyst returning Yes on (4) → open the next round.
9. **Close the round** — `gaps_status.py <topic>` for per-gap state; write the research_log round section (gaps / targeted expansion / analyst four-list highlights / triage decisions — concise). Discipline: any gap with ≳5 new verified this round MUST get an analyst (step 7); skipping (only 1–2 weak entries, or a pure saturation-confirmation round) must be explicitly noted in research_log.

### Phase 5 — main thread integrates + writes <topic>.md
No writer subagent — whole-document coherence needs the main thread. Aggregate every round's cite_recommend; outline with proposition titles; `Write` <topic>.md (Abstract → §1-N proposition sections → §recommendations table → §limitations → **§Methods** → References). **Then lint, then render** so reviewers audit a near-final draft: `lint_review.py` (fix citation/structure), then `render_refs.py <topic>/<topic>.md <topic>` (generates the §Methods PRISMA flow funnel + References). render is idempotent; Phase 7 re-runs as needed.

### Phase 6 — 3 independent reviewers + revision loop (≤3 rounds)
1. `python scripts/reviewer.py prompt <topic> --round 1` → shared prompt template at `reviewers/prompt_round_1.md`.
2. Main thread spawns 3 Opus reviewers in ONE message (`model: "opus"`, **byte-identical prompts** pointing at the template, differing only in a trailing "you are reviewer N" line — preserves prefix-cache sharing and independence). Each writes `reviewers/round_1_{1,2,3}.md`.
3. `reviewer.py tally <topic> --round 1`:
   - **3/3 approve** → Phase 7.
   - any request_changes → revise per the FAIL items, then re-review. **Reviewer reuse (preferred, far cheaper):** in an interactive runtime that supports agent messaging (e.g. `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`), the MAIN thread can `SendMessage` each reviewer's agentId with "your FAIL list + the diff; only judge (a) fixed? (b) regression?" — the reviewer resumes with its cached round-1 context (re-read of <topic>.md/abstracts is free; you only pay for the diff), ~5× faster than re-spawning. **Fallback** (messaging unavailable — note: spawned subagents themselves have no message/spawn tools, so this only works main→reviewer): re-spawn 3 fresh reviewers with byte-identical convergent prompts (attach "round-1 FAIL list + diff; judge only fixed?+regression?, don't reopen unraised nitpicks"). Either path → `reviewers/round_K_{1,2,3}.md` → `reviewer.py tally --round K`. If revision changed `[@key]` citations, re-run `render_refs.py` before re-review.
   - **Don't reuse per-gap analysts** (each reads disjoint notes, no cross-round re-read → fresh spawn is already cheapest). Reuse only pays for agents that re-read the same large content across rounds (reviewers).
   - 3 rounds still not 3/3 → `reviewer.py failure-report <topic>`, stop and surface to the user.

### Phase 7 — final lint + re-render + sign-off
1. If Phase 6 changed citations → re-run `render_refs.py <topic>/<topic>.md <topic>` to sync References + PRISMA flow.
2. `lint_review.py <topic>` — must exit 0 or 2 (WARN acceptable).
3. `research_log.md` is required (concise per-round records: gaps / targeted expansion / analyst four-list highlights / triage). An optional sign-off note can list the top decisions if the user wants one.
4. Deliver `<topic>.md`. For viewers that don't render inline LaTeX, also `python scripts/md_to_html.py <topic>/<topic>.md` → `<topic>.html`.

---

## gap_type quick reference

| Type | Required subfields | Markers |
|---|---|---|
| **decision** | `--population --intervention --comparator --outcome` | "does X work" / "which to pick" / "recommended dose" |
| **mechanism** | `--phenomenon --candidate-mechanisms --evidence-types` | "why" / "mechanism" / "how does it cause" |
| **comparison** | `--item-a --item-b --dimensions --comparison-level` | "X vs Y" / "compare" / "how different" |
| **methodology** | `--process --audience --decision-question --reference-standard` | "how to do X" / "precision/error" / "formula source" |
| **safety** | `--exposure --at-risk-population --adverse-outcomes --threshold-ref` | "what's the risk" / "safety" / "toxicity" |
| **diagnostic** | `--differential-list --discriminating-features --reference-standard` | "differential dx" / "how to distinguish" |
| **descriptive** | `--phenomenon --measurement-method --population-setting` | "current state" / "baseline/distribution" / "prevalence" |

## Termination (decided by `term_check.py --json`)

Three states: `not_ready` (keep iterating), `saturated` (may write, write boundaries normally), `hard_stop` (round cap hit — must state the cap + remaining-evidence-insufficient in research_log + §limitations).

**Necessary floor for `saturated`** (all must hold): rounds ≥ 3; no gap declared in the latest round (`max(gap.created_round) < latest_round`); each declared gap ≥ 3 independent verified; each `decision`/`comparison` gap ≥ 1 RCT/meta **only when** the patch sets `require_rct_or_meta=true` (mechanism/safety/diagnostic/descriptive/methodology gaps are exempt — they rely on cohorts/case-series/mechanism). **Sufficient trigger:** latest round's new-verified < 10% → `saturated`; rounds ≥ 5 → `hard_stop`. Adding any new gap resets saturation, so a new gap means ≥ 2 more rounds. **Never touch <topic>.md before term_check returns saturated/hard_stop** — write once, after all rounds.

## Citation rules (hard)
- Body `[@key]` must come from that round's analyst **cite_recommend** lists — never from domain knowledge or memory. A landmark paper not surfaced this round → it wasn't retrieved (open a gap next round), or the analyst deprioritized it (accept that). Don't hand-insert a key.
- Exception: textbook consensus needs no citation — write it and mark "standard textbook knowledge."
- ≤ 3 citations per claim (meta + representative RCT + cohort, or the domain's first-tier). Same-direction replications → cite the strongest one; others merge into §limitations or stay uncited in the store.
- Retracted entries stay in the store (`retracted=true`) for transparency but are **forbidden in the body** (`lint_review.py` and `render_refs.py` both block them).

## Quick command reference

```bash
# Phase 1
python scripts/search_existing.py "<keywords>"
python scripts/bootstrap_topic.py "<topic>" --domain {health|animals|education-psychology|physics|food-science}
# Rounds
python scripts/verify.py <topic> --declare-gap gap-N "<desc>" --gap-type <type> <subfields> --round R
python scripts/verify.py <topic> --add DOI TITLE YEAR AUTHORS --gap gap-N --source seed --round R
python scripts/search.py "<query>" --rows 8                      # discovery-first (default --source both)
python scripts/genealogy.py <topic> --gap gap-N --round R --parallel 3
python scripts/fetch.py <topic> --include abstract --parallel 3
python scripts/notes.py <topic> --round R --force
python scripts/gaps_status.py <topic>
python scripts/regap.py <topic> DOI gap-N
python scripts/exclude.py <topic> DOI "reason"
# Gate / finish
python scripts/workflow_status.py <topic>                        # read-only "where am I" panel
python scripts/lint_review.py <topic>
python scripts/term_check.py <topic> --json                      # must be saturated/hard_stop to write
python scripts/render_refs.py <topic>/<topic>.md <topic>          # PRISMA flow + References
python scripts/reviewer.py prompt <topic> --round 1
python scripts/reviewer.py tally <topic> --round 1
python scripts/md_to_html.py <topic>/<topic>.md                   # HTML delivery
```

## Failure fallbacks
- **verify.py DOI → `failed`**: stays in store, forbidden in body. Re-check the DOI from the journal site, `--recheck`; still failing → swap a different source for that gap.
- **verify.py `[ERROR] title_mismatch`**: assume the DOI is wrong (OCR/copy error). Check `https://doi.org/<DOI>` landing page title + first author. Mismatch with the user's title → re-search the correct DOI and re-add (verify.py already lists candidates in stderr). Only if the landing page matches the user's title but CrossRef metadata is wrong: `--force-mismatch <DOI> --force-mismatch-reason "doi.org/<DOI> actual_title='…' actual_first_author='…' reason='…'"` (the tool forces a non-empty reason containing `doi.org/` and writes a permanent audit).
- **fetch.py abstract → `failed`**: no retry. If the entry is load-bearing, hand-place text at `tmp/<topic>/abstracts/<safe_doi>.md` and point the entry's `paths.abstract` at it. Never invent abstract text.
- **genealogy.py rate-limit / 404**: built-in backoff; long batches may still error mid-way. Re-run the same `--gap gap-N` (injected pending candidates aren't lost). A seed whose OpenAlex id 404s → fall back to `verify.py --add` manually.

## What `lint_review.py` covers (exit 0 = clean, 2 = WARN-acceptable)
Every `[@key]` resolves to a verified non-retracted entry; each declared gap ≥ 1 verified support; phantom-gap / depends_on / subgap_of checks; cited-verified ratio ≥ 50%; prose-adjacent author/year matches entry metadata; gap count ≤ 8; gap description ≤ 200 chars; §limitations + §Methods present; protocol non-empty; gap_type subfields complete; abbreviation-gloss spot checks. Auto-runs `term_check.py` at round ≥ 3.
