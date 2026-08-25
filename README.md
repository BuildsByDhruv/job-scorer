# ⚡ EdgeDash

> **Your job search, running while you sleep.**

EdgeDash is an autonomous career intelligence agent. Every day it fetches live
job listings, scores each one against your profile, ranks your skill gaps by the
cost they're actually extracting from you, and surfaces the delta since yesterday.
You wake up to a ranked table, not a pile of unread postings.

---

## Dashboard

![EdgeDash activity log — pass/fail/degraded cycle cards with verdict, agents ran, duration, and failing check name](docs/dashboard-activity.png)

*Header strip: last verified cycle, totals, coverage, verdict. Activity log: one card per cycle, colour-coded by verdict.*

![EdgeDash listings and skill gaps — scored listings with progress bars, skill gap chart sorted by opportunity cost](docs/dashboard-listings.png)

*Top scored listings with fit bars and clickable titles. Skill gap chart sorted by weighted opportunity cost.*

Run the dashboard:

```bash
python -m streamlit run app.py
```

---

## What it actually does

```
08:00  →  Fetcher pulls new listings from Arbeitnow / Apify
           Stable-hash deduplication — same job never stored twice

08:01  →  Scorer extracts facts via LLM (one call per unique description)
           then scores deterministically — no model touches the number
           scored 25 · range 31-84 · mean 61 · spread OK

08:02  →  GapAnalyzer compares required skills to your profile
           ranks gaps by Σ(fit_score/100) — a gap in an 85-score listing
           outweighs a gap in a 20-score listing, even at equal frequency
           writes one timestamped snapshot — previous snapshots never overwritten

08:02  →  Verifier runs deterministic checks on the output distribution
           score_spread · extraction_sanity · gap_sample_size · freshness
           On failure: retries the responsible agent once, then marks degraded
           Dashboard only shows data from the last PASSING cycle
```

---

## Architecture

```
run_cycle.py
    │
    ├─ state.py          read_state(config, now) → SystemState
    │                    cheap MAX/COUNT queries, clock is a parameter
    │
    ├─ planning.py       build_plan(state, config) → Plan
    │                    pure function, no I/O, skips are explicit
    │
    ├─ orchestrator.py   executes the plan, wraps each agent in try/except,
    │                    writes exactly one summary row, marks cycle partial
    │                    if any agent fails — never stops the cycle
    │                         │
    │                         ├── Fetcher       one source = one try/except
    │                         ├── Scorer        LLM extract → deterministic score
    │                         ├── GapAnalyzer   pure arithmetic, no model
    │                         └── Verifier      distribution checks, no LLM
    │
    └─ storage.py        the ONLY file that touches sqlite3
```

**Adding a new agent: one registry entry + one decision block in `build_plan`. Nothing else changes.**

---

## Current status

| Module | What it does |
|---|---|
| `edgedash/config.py` | `Config` dataclass from `config.yaml`; `skill_aliases` map |
| `edgedash/storage.py` | SQLite behind a thin interface; `skill_gap_snapshots` append-only |
| `edgedash/state.py` | `read_state(config, now)` — `now` is a param, fully testable |
| `edgedash/planning.py` | `build_plan(state, config)` pure function; explicit skip reasons |
| `edgedash/orchestrator.py` | State-driven cycle; verification + retry; one summary row |
| `edgedash/verification.py` | 4 deterministic checks; no LLM; thresholds in `config.yaml` |
| `edgedash/agents/verifier.py` | Runs checks, returns verdict; writes no data |
| `edgedash/agents/fetcher.py` | Live fetcher; per-source isolation; respects `max_listings` |
| `edgedash/agents/mock_fetcher.py` | Offline mock; `use_mock_fetcher: true` in config |
| `edgedash/agents/scorer.py` | Batch scorer; quota-exhaustion stops immediately |
| `edgedash/agents/extractor.py` | LLM extraction with description-hash cache |
| `edgedash/agents/gap_analyzer.py` | Deterministic gap ranking by opportunity cost |
| `edgedash/sources/base.py` | `Source` protocol; `@register` decorator |
| `edgedash/sources/http.py` | Shared HTTP helper: timeout, retries, rate-limit, User-Agent |
| `edgedash/sources/arbeitnow.py` | Arbeitnow free board (no key) |
| `edgedash/sources/apify.py` | Apify / Indeed scraper (`APIFY_TOKEN` in `.env`) |
| `edgedash/llm.py` | Single LLM gateway; 503 retries; daily quota detected fast |
| `edgedash/scoring.py` | Four-component deterministic scorer; model never touches the number |
| `edgedash/skills.py` | `canonical()` pipeline; `--audit`; `--suggest-aliases` |
| `edgedash/gaps.py` | `--compare` (freq vs cost); `--trend` (earliest → latest snapshot) |
| `edgedash/verdicts.py` | Verification history CLI; `--check <name>` filter |
| `edgedash/diagnose.py` | Read-only DB health check |
| `edgedash/rescore.py` | Wipe scores without touching extraction cache |
| `app.py` | Streamlit dashboard — read-only, verified-cycle data only |
| `run_cycle.py` | Entry point: `--dry-run`, `--force`, `--explain` |
| `tests/` | 111 tests — scorer, skills, planning, verification |

### Week 4
- [ ] Swap SQLite → hosted Postgres (one-file change in `storage.py`)
- [ ] Deploy scheduled trigger (cron / cloud scheduler)

---

## Setup

Requires **Python 3.11+**.

```bash
git clone https://github.com/BuildsByDhruv/job-scorer.git
cd job-scorer
pip install -r requirements.txt
cp .env.example .env   # add GEMINI_API_KEY, optionally APIFY_TOKEN
```

Edit `config.yaml` — role, city, skills, seniority, sources, alias map.
Every user-specific value lives there. Nothing is hardcoded.

```bash
python run_cycle.py          # run a full cycle
python -m streamlit run app.py   # open the dashboard
```

---

## Commands

### Cycle control

| Command | What it does |
|---|---|
| `python run_cycle.py` | Run a full state-driven cycle |
| `python run_cycle.py --dry-run` | Print the plan, exit — zero writes, zero API calls |
| `python run_cycle.py --dry-run --explain` | Show every state value + decision, then exit |
| `python run_cycle.py --explain` | Same breakdown, then execute |
| `python run_cycle.py --force scorer` | Force scorer even if nothing is unscored |
| `python run_cycle.py --force fetcher --force scorer` | Force multiple agents |

### Dashboard

| Command | What it does |
|---|---|
| `python -m streamlit run app.py` | Open the live dashboard at `localhost:8501` |

### Gap and verification intelligence

| Command | What it does |
|---|---|
| `python -m edgedash.gaps` | Latest gap snapshot, ranked by opportunity cost |
| `python -m edgedash.gaps --compare` | Frequency vs cost rankings side by side |
| `python -m edgedash.gaps --trend` | How top 10 gaps moved since the first snapshot |
| `python -m edgedash.verdicts` | Last 20 cycle verdicts — pass/fail/degraded |
| `python -m edgedash.verdicts --check extraction_sanity` | Filter to cycles where that check failed |
| `python -m edgedash.verdicts --limit 50` | Show more history |

### Skill maintenance

| Command | What it does |
|---|---|
| `python -m edgedash.skills --audit` | 40 most common raw strings + singletons |
| `python -m edgedash.skills --suggest-aliases` | Model proposes aliases; writes nothing |

### Diagnostics and maintenance

| Command | What it does |
|---|---|
| `python -m edgedash.diagnose` | Counts, duplicates, quality issues |
| `python -m edgedash.agents.scorer --limit 5` | Score N listings manually |
| `python -m edgedash.rescore --id <id>` | Clear one listing's score |
| `python -m edgedash.rescore --all` | Clear all scores (prompts for confirmation) |
| `python -m edgedash.llm --check` | Verify LLM provider and model |
| `python -m pytest tests/` | Run the full test suite (111 tests, ~0.9s) |

---

## How scoring works

**Step 1 — Extract** (one LLM call per unique description, cached forever by description hash):
```
job description  →  { required_skills, nice_to_have, seniority, remote_ok, years }
```

**Step 2 — Score** (pure Python, no model):
```
skill_match    × 0.45   fraction of required skills you have
seniority_fit  × 0.25   band distance from your target
location_fit   × 0.15   remote / city match / elsewhere
recency        × 0.15   linear decay: 1.0 today → 0.0 at 30 days
─────────────────────
fit_score  ∈ [0, 100]
```

All weights are tunable in `config.yaml`. The reason string is built from
component values by code — the model never writes a word of it.

Re-scoring is free. `python -m edgedash.rescore --all` clears scores without
touching the extraction cache. Next cycle re-scores from cached facts at zero API cost.

---

## How gap ranking works

```
opportunity_cost(skill) = Σ (fit_score / 100)
                            for each scored listing where:
                              skill ∈ required_skills
                              AND skill ∉ my_skills
```

A listing scored 85 contributes 0.85. One scored 20 contributes 0.20. Two gaps
at the same raw frequency rank differently if the listings behind them differ in
quality. `--compare` shows both rankings side by side so you can see what moved and why.

---

## How verification works

After every cycle the Verifier runs four deterministic checks — no LLM involved:

| Check | What it catches | Threshold (`config.yaml`) |
|---|---|---|
| `score_spread` | All scores in a narrow band — model inflation | `min_score_spread: 10`, `min_score_stdev: 5` |
| `extraction_sanity` | Extractor broken (empty lists) or returned a sentence as skills | `max_empty_extraction_pct: 20`, `max_skills_per_listing: 30` |
| `gap_sample_size` | Top-ranked gap computed from too few listings | `min_gap_sample: 3` |
| `freshness` | Database is stale — fetcher hasn't run recently | `max_data_age_days: 3` |

On a failed check the Orchestrator retries the responsible agent once with
adjusted context (e.g. `widen_distribution=True` for a spread failure), then
re-verifies. If it fails again the cycle is marked **degraded** and stops.
The dashboard always shows data from the last **passing** cycle — stale verified
data beats fresh unverified data.

---

## How the Orchestrator decides

```
state.hours_since_fetch >= fetch_interval_hours  →  ▶ RUN  fetcher
state.unscored_count > 0                         →  ▶ RUN  scorer
state.gaps_stale OR gaps_computed_at is None     →  ▶ RUN  gap_analyzer
always                                           →  ▶ RUN  verifier
otherwise                                        →  ○ SKIP  (this is a success)
```

Every skip is explicit and logged with the state value that caused it.
Use `--explain` to see the full breakdown. Use `--force <agent>` to override
a skip; the override is recorded in the cycle summary row.

---

## Skill canonicalisation

Raw strings from job listings go through a normalisation pipeline:

```
"Kubernetes (EKS)"  →  lowercase  →  drop (qualifier)  →  strip punctuation
                    →  collapse whitespace  →  alias lookup  →  "kubernetes"
```

The alias map in `config.yaml` is yours to own. The model never merges names.
Run `--audit` to find collisions. Run `--suggest-aliases` to get model proposals —
it prints YAML, writes nothing, and flags conflicts with your existing map.

---

## LLM configuration

```yaml
llm_provider: "gemini"
llm_model: "gemini-2.5-flash"
llm_batch_size: 25
```

| Scenario | Behaviour |
|---|---|
| Daily quota hit | `LLMQuotaExhausted` stops the batch immediately |
| 503 overload | Backs off and retries up to 3× before raising `LLMError` |
| Per-minute 429 | Honours `retry-after` from the API response |
| No API key | Set `llm_provider: "ollama"` and run locally |

---

## Design decisions

**Storage is isolated behind one module.**
`edgedash/storage.py` is the only file permitted to import `sqlite3`. Moves to Postgres in week 4 with a single-file change.

**Listing IDs are stable hashes.**
`SHA-256(source + url)` is the primary key. The same job re-fetched later hits `INSERT OR IGNORE` silently.

**The Orchestrator is state-driven, not sequence-driven.**
`build_plan` is a pure function of `(state, config)`. Skips are explicit plan entries. One agent failing marks the cycle `partial` — the remaining agents still run.

**Scoring is split: model extracts facts, Python scores them.**
The model never sees weights and never produces a number. Scores are reproducible, auditable, and free to recompute when you tune the weights.

**Verification is deterministic and has no LLM.**
A model cannot be the judge of a model's output. All thresholds live in `config.yaml` with comments naming the failure mode each one catches.

**Gap history is append-only by design.**
`skill_gap_snapshots` is `INSERT`-only. Each run gets a UUID. Trend data exists because it was never overwritten.

**The dashboard reads from the last passing cycle only.**
Stale verified data always beats fresh unverified data. When the latest cycle fails, the dashboard shows a warning banner with both timestamps and continues showing the last known-good data.
