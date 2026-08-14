# EdgeDash

EdgeDash is an autonomous career intelligence agent that runs on a daily schedule,
fetches live job listings from configurable sources, scores each listing for fit
against your personal profile, identifies skill gaps between your current skills
and what the market is asking for, verifies its own output for consistency, and
publishes the results to a local Streamlit dashboard — all without manual
intervention.

---

## Architecture

```
Trigger (scheduled)
    |
    v
Orchestrator
    |-- Fetcher       fetches raw listings from job sources
    |-- Scorer        extracts facts (LLM) then scores deterministically
    +-- GapAnalyzer   surfaces skills that appear in listings but not your profile
         |
         v
       Verifier       checks output for consistency before committing
         |
         v
       Storage        single module; all DB access goes through here
         |
         v
       Dashboard      read-only Streamlit view of scored listings and gaps
```

---

## Current status

### Built
- [x] `edgedash/config.py` — `Config` dataclass loaded from `config.yaml`
- [x] `edgedash/storage.py` — SQLite backend behind a thin interface
- [x] `edgedash/agents/base.py` — `Agent` protocol and `AgentResult` dataclass
- [x] `edgedash/agents/mock_fetcher.py` — offline mock; swap back in via `use_mock_fetcher: true` in `config.yaml`
- [x] `edgedash/agents/fetcher.py` — real Fetcher; queries all enabled sources, logs each per-source outcome
- [x] `edgedash/sources/base.py` — `Source` protocol, `SOURCES` registry, `@register` decorator
- [x] `edgedash/sources/http.py` — shared HTTP helper (timeout, retries, rate-limiting, User-Agent)
- [x] `edgedash/sources/arbeitnow.py` — Arbeitnow free public job board (no key required)
- [x] `edgedash/sources/apify.py` — Apify / Indeed scraper (requires `APIFY_TOKEN` in `.env`)
- [x] `edgedash/llm.py` — single LLM gateway; Gemini and Ollama providers; rate-limited; validates all responses
- [x] `edgedash/scoring.py` — deterministic scorer; four weighted components; no model calls
- [x] `edgedash/agents/extractor.py` — LLM-backed fact extraction with description-hash cache
- [x] `edgedash/agents/scorer.py` — Scorer agent; per-listing error isolation; distribution logging
- [x] `edgedash/orchestrator.py` — reads state, plans, runs registered agents, logs every run to `cycle_log`
- [x] `edgedash/diagnose.py` — read-only diagnostic: counts, cross-source duplicates, recent listings, quality issues
- [x] `edgedash/rescore.py` — manual re-scoring escape hatch; never clears the extraction cache
- [x] `run_cycle.py` — single entry point
- [x] `tests/test_scoring.py` — 25 unit tests for the deterministic scorer

### Week 3
- [ ] `GapAnalyzer` agent
- [ ] `Verifier` agent
- [ ] Streamlit dashboard (read-only)

### Week 4
- [ ] Swap SQLite for hosted Postgres (one-file change in `storage.py`)
- [ ] Deploy scheduled trigger (cron / cloud scheduler)

---

## Setup

Requires **Python 3.11+**.

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your API keys:

```bash
cp .env.example .env
# edit .env and set APIFY_TOKEN and GEMINI_API_KEY
```

Edit `config.yaml` to set your target role, city, skills, seniority, and enabled
sources. Every user-specific value lives there — nothing is hardcoded.

```bash
python run_cycle.py
```

---

## Commands

| Command | What it does |
|---|---|
| `python run_cycle.py` | Run a full fetch + score cycle |
| `python -m edgedash.diagnose` | Read-only DB diagnostic (counts, gaps, quality) |
| `python -m edgedash.agents.scorer --limit 5` | Score up to 5 listings manually |
| `python -m edgedash.rescore --id <id>` | Clear one listing's score for re-scoring |
| `python -m edgedash.rescore --all` | Clear all scores (prompts for confirmation) |
| `python -m edgedash.llm --check` | Verify LLM provider and model are working |
| `python -m pytest tests/` | Run the test suite |

---

## Sources

| Source | Key required | Notes |
|---|---|---|
| `arbeitnow` | No | Free public API; Europe-focused |
| `apify` | `APIFY_TOKEN` in `.env` | Scrapes Indeed via Apify actor |

Enable or disable sources in `config.yaml`:

```yaml
sources:
  - "arbeitnow"
  # - "apify"   # uncomment after adding APIFY_TOKEN to .env
```

Set `use_mock_fetcher: true` to run entirely offline with hardcoded listings.

---

## Scoring

Scoring is split into two steps that run in sequence for each listing:

**Extraction** (`extractor.py`) calls the LLM once per unique job description to
pull out structured facts: required skills, nice-to-haves, seniority level, years
required, and remote availability. Results are cached by a hash of the description
text — the same text is never sent to the model twice, even across cycles.

**Scoring** (`scoring.py`) takes those facts and computes a deterministic 0–100
score using four weighted components. No model is involved in this step.

| Component | Default weight | Logic |
|---|---|---|
| `skill_match` | 0.45 | Fraction of required skills you have; nice-to-haves count at ⅓ weight |
| `seniority_fit` | 0.25 | Band distance from your target: exact=1.0, ±1=0.6, ±2=0.25, ≥3=0.0 |
| `location_fit` | 0.15 | Remote→1.0, city match→1.0, unknown→0.5, elsewhere→0.1 |
| `recency` | 0.15 | Linear decay from 1.0 (today) to 0.0 (30 days old) |

All four weights are tunable in `config.yaml`. The reason string is assembled from
the component values by code — the model never writes it.

Re-scoring is free: `python -m edgedash.rescore --all` clears scores without
touching the extraction cache, so the next cycle re-scores using cached facts.

---

## LLM configuration

```yaml
llm_provider: "gemini"          # "gemini" or "ollama"
llm_model: "gemini-2.5-flash"
llm_batch_size: 25              # max listings scored per cycle
```

The Gemini free tier allows 20 requests per day on `gemini-2.5-flash`. Set
`llm_batch_size` to 15 or lower to stay within the limit per run, or use
`gemini-2.5-flash-lite` for a higher quota. For fully local inference with no API
key, set `llm_provider: "ollama"` and run Ollama locally.

---

## Design decisions

**Storage is isolated behind one module.**
`edgedash/storage.py` is the only file permitted to import `sqlite3`. Every other
module calls its thin public interface. When the backend moves to Postgres in
week 4, the change is contained to that one file — no grep-and-replace across
the codebase.

**Listing IDs are stable hashes of source and URL.**
A SHA-256 digest of `(source, url)` is computed before any insert. The same job
re-fetched on a later run produces the same ID, so `INSERT OR IGNORE` silently
skips it. The count of genuinely new rows is returned and logged, making
deduplication observable rather than invisible.

**The Orchestrator delegates; it never does the work itself.**
The Orchestrator reads state, decides which agents to run and why, then hands off
to each agent. This keeps the decision logic and the execution logic separate,
makes each agent independently testable, and means a new agent can be wired in by
adding one entry to the registry without touching any existing code.

**Every source sits behind a uniform interface.**
The `Source` protocol and `@register` decorator mean the Fetcher never contains
source-specific logic. Adding a new job board is one new file and one decorator —
nothing else changes. A source failing never kills the cycle; errors are caught
per-source, logged to `cycle_log`, and the next source continues.

**Scoring is split: LLM extracts facts, Python scores them.**
The model never sees scoring weights and never produces a number. It reads a job
description and returns structured facts. A pure Python function turns those facts
into a score using fixed arithmetic. This keeps scores reproducible, auditable,
and cheap to recompute when you tune the weights.

**Extraction results are cached by description hash.**
The same job description, re-fetched from a different source or on a later run,
hits the cache and costs zero API calls. Re-scoring after changing your skills or
weights is free.

**Secrets load in one place.**
`run_cycle.py` calls `load_dotenv()` before any other import. No other file reads
`.env`. If a key is missing, the relevant source or provider skips itself with a
log line rather than raising.
