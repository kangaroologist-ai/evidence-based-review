# EBR — Setup Guide

EBR (Evidence-Based Review) is a toolchain for writing rigorously-cited literature
reviews. Every `[@key]` in the output resolves to a DOI verified against CrossRef,
so the review can't hallucinate citations.

This guide is for **a human who wants to run the skill**. It covers the workspace,
dependencies, and (optional) API keys. You can be reviewing literature in about two
minutes. **EBR runs end-to-end with zero API keys** — see the [Zero-key](#zero-key--no-api-keys-required) section.

---

## 1. Requirements

- **Python 3.12+** (the code uses 3.12 syntax — `type` aliases, `X | None`).
- A workspace directory where your reviews will live (separate from the skill code).

Check your Python:

```bash
python3 --version    # must be >= 3.12
```

---

## 2. Install dependencies

From the skill root (`skills/ebr/`):

```bash
pip install -r requirements.txt
```

That pulls four packages:

| Package         | Why                                                    |
|-----------------|--------------------------------------------------------|
| `httpx[socks]`  | HTTP client for CrossRef / OpenAlex / Semantic Scholar |
| `pymupdf`       | PDF text extraction for full-text fetch                |
| `pytest`        | running the test suite (dev only)                      |
| `pytest-mock`   | test fixtures (dev only)                               |

If an `import` fails after this, you're almost certainly on the wrong interpreter —
check `which python3` before re-installing anything.

---

## 3. Create a workspace

Your **reviews, reference stores, and caches** live in a workspace directory *separate*
from the skill code (the skill itself ships its `patches/` domain rules). Scaffold it
once with the setup tool:

```bash
python scripts/setup.py init --workspace ~/ebr-workspace
export HEALTH_REVIEW_ROOT=~/ebr-workspace        # setup.py prints this exact line
```

`setup.py init` creates `reviews/ state/ tmp/` under the workspace and prints the
`export` line to copy. It's idempotent (safe to re-run) and also takes the optional
`--email`, `--openalex-key`, `--semantic-scholar-key` flags (see §4). Run
`python scripts/setup.py status` anytime to see the current workspace + key state.

Manual equivalent, if you prefer:

```bash
mkdir -p ~/ebr-workspace/{reviews,state,tmp}
export HEALTH_REVIEW_ROOT=~/ebr-workspace
```

**`HEALTH_REVIEW_ROOT` is the single source of truth for where output goes.** Every
tool reads it first (`scripts/lib/project.py` → `project_root()`). Put the `export` in
your `~/.zshrc` / `~/.bashrc` so you don't set it each session.

> **Required, not optional.** With `HEALTH_REVIEW_ROOT` unset the tools now **fail
> loudly** (`RuntimeError: HEALTH_REVIEW_ROOT is not set …`) instead of silently writing
> into the skill install directory (which gets wiped on reinstall). Set it before
> running any tool. API keys (§4) still fall back gracefully without it.

Per-topic folders (`reviews/<topic>/`, `tmp/<topic>/`, `figures/`) are created
automatically by `bootstrap_topic.py` — you don't make those by hand.

---

## 4. API keys — all optional, all with fallbacks

EBR talks to three scholarly APIs. **None of them require a key.** Keys only raise
rate limits or unlock premium tiers. Each is read by `scripts/lib/apis.py` in this
order: **environment variable → `state/<file>` → `None` (fallback)**.

### CrossRef — citation verification

- **Need a key?** No. There is no key. **Zero registration.**
- **Setup:** nothing. The client hard-codes a `mailto` and rides the "polite pool."
- **Without it:** N/A — this is the only mode.

### OpenAlex — genealogy (citation-graph snowballing)

- **Need a key?** No. The optional key is **OpenAlex Premium** (paid —
  <https://openalex.org/pricing>). Most users don't have it and don't need it.
- **Setup (only if you bought Premium):**
  ```bash
  export OPENALEX_API_KEY=your_premium_key
  # or:  echo "your_premium_key" > "$HEALTH_REVIEW_ROOT/state/openalex_api_key"
  ```
- **Without it:** genealogy still runs fully — requests go to the shared `mailto`
  common pool, just rate-limited. The tool backs off and retries automatically.

### Semantic Scholar — search (second source alongside CrossRef)

- **Need a key?** No. A **free** key is available and only speeds things up.
- **Get one (free):** fill out the form at
  <https://www.semanticscholar.org/product/api#api-key-form>.
- **Setup:**
  ```bash
  export SEMANTIC_SCHOLAR_API_KEY=your_key
  # or:  echo "your_key" > "$HEALTH_REVIEW_ROOT/state/semantic_scholar_api_key"
  ```
- **Without it:** search still runs — Semantic Scholar requests are throttled to the
  shared pool (~1 req/s) with automatic backoff. CrossRef results come back regardless.

### Contact email (polite-pool identifier)

CrossRef and OpenAlex want a contact email in the `mailto`/User-Agent so they can
reach you if a script misbehaves. It's **configurable**, same precedence as the keys
(env → `state/<file>` → neutral default):

```bash
python scripts/setup.py init --workspace ~/ebr-workspace --email you@example.com
# or:  export EBR_EMAIL=you@example.com
# or:  echo "you@example.com" > "$HEALTH_REVIEW_ROOT/state/email"
```

Without it, requests use a neutral placeholder (`ebr-tools@users.noreply.example.com`) —
fine for light use, but set your own before any heavy crawling so the APIs can reach you.

---

## Zero-key — no API keys required

**You do not need to register for, pay for, or configure any API key to use EBR.**
With zero keys set, a full review still runs start to finish:

- **CrossRef** verification — always keyless (polite pool).
- **OpenAlex** genealogy — keyless common pool, just slower.
- **Semantic Scholar** search — shared pool (~1 req/s), just slower.

The only difference keys make is **speed**: genealogy snowballing and search hit
shared rate limits and occasionally get throttled. Every tool has built-in
exponential backoff + retry, so throttling slows a run down — it never breaks it.

Add keys later if and when the wait bothers you. Nothing else changes.

**Optional: OCR for scanned PDFs.** `fetch.py` falls back to OCR only when a PDF's
born-digital text extraction comes back empty (old, scanned-only papers). This needs
a [MinerU](https://github.com/opendatalab/MinerU)-class CLI on PATH (`mineru` /
`magic-pdf`, or point `HEALTH_REVIEW_OCR_CMD` at one) — entirely optional, every
failure mode (binary missing, nonzero exit, timeout) degrades silently to
`title_only` grounding rather than erroring.

---

## 5. Quick start

Once dependencies are installed and `HEALTH_REVIEW_ROOT` is exported:

```bash
# 1. bootstrap a new review topic (creates reviews/<topic>/ + research_log.md)
python3 scripts/bootstrap_topic.py "<your topic>" --domain health
```

`--domain` selects the rule set under `patches/`. Valid values:
`health` (default), `animals`, `education-psychology`, `physics`, `food-science`.

From there, follow the workflow in **`SKILL.md`** (the skill's main instructions):
declare gaps → search / verify / genealogy / fetch → notes → synthesize → lint →
render references → reviewer pass. Two reference docs go deeper:

- `references/prose-style.md` — writing rules: decision-first titles, numbers-with-meaning, ≤3 cites/claim, term glosses.
- `references/evidence-tiers.md` — cross-domain evidence hierarchy + per-domain patch overrides.

A read-only status panel tells you what any in-progress topic still needs:

```bash
python3 scripts/workflow_status.py reviews/<topic>
```
