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

Live dashboard (Streamlit Community Cloud):

```bash
python -m streamlit run app.py
```

---

## What it actually does

```
06:00 IST  →  GitHub Actions triggers the daily cycle

             Fetcher pulls new listings from Arbeitnow / Apify
             Stable-hash deduplication — same job never stored twice

             Scorer extracts facts via LLM (one call per unique description)
             then scores deterministically — no model touches the number
             scored 25 · range 31-84 · mean 61 · spread OK

             GapAnalyzer compares required skills to your profile
             ranks gaps by Σ(fit_score/100) — a gap in an 85-score listing
             outweighs a gap in a 20-score listing, even at equal frequency
             writes one timestamped snapshot — previous snapshots never overwritten

             Verifier runs deterministic checks on the output distribution
             score_spread · extraction_sanity · gap_sample_size · freshness
             On failure: retries the responsible agent once, then marks degraded
             Dashboard only shows data from the last PASSING cycle

             Health check reports system status — fails the job if stale
```

Results land in hosted Postgres (Supabase). The Streamlit dashboard reads from
there — a separate process that never runs a cycle.

---

## Architecture

```
GitHub Actions (cron 06:00 IST)
    │
    └─ run_cycle.py
         │
         ├─ state.py          read_state(config, now) → SystemState
         ├─ planning.py       build_plan(state, config) → Plan  (pure function)
         │
         └─ orchestrator.py   executes plan, one summary row per cycle
                  │
                  ├── Fetcher       one source = one try/except
                  ├── Scorer        LLM extract → deterministic score
                  ├── GapAnalyzer   pure arithmetic, no model
                  └── Verifier      distribution checks, no LLM

storage.py  ←──  the ONLY file that touches the database driver
                 SQLite locally · Postgres in production (DATABASE_URL)

app.py  ←──  Streamlit dashboard  (read-only, never runs a cycle)

edgedash/health.py  ←──  health checks (db, freshness, recency, streak)
```

**Adding a new agent: one registry entry + one decision block in `build_plan`. Nothing else changes.**

---

## Module index

| Module | What it does |
|---|---|
| `edgedash/config.py` | `Config` dataclass from `config.yaml`; skill alias map |
| `edgedash/storage.py` | Dual SQLite/Postgres backend; the only file that imports a DB driver |
| `edgedash/state.py` | `read_state(config, now)` — clock is a parameter, fully testable |
| `edgedash/planning.py` | `build_plan(state, config)` pure function; skips are explicit |
| `edgedash/orchestrator.py` | State-driven cycle; verification + retry; one summary row |
| `edgedash/verification.py` | 4 deterministic checks; no LLM; thresholds in `config.yaml` |
| `edgedash/health.py` | 4 health checks; CLI exits non-zero if unhealthy; dashboard status bar |
| `edgedash/agents/verifier.py` | Runs verification checks; writes no data |
| `edgedash/agents/fetcher.py` | Live fetcher; per-source isolation; respects `max_listings` |
| `edgedash/agents/mock_fetcher.py` | Offline mock; `use_mock_fetcher: true` in config |
| `edgedash/agents/scorer.py` | Batch scorer; quota-exhaustion stops immediately |
| `edgedash/agents/extractor.py` | LLM extraction with description-hash cache |
| `edgedash/agents/gap_analyzer.py` | Deterministic gap ranking by opportunity cost |
| `edgedash/sources/base.py` | `Source` protocol; `@register` decorator |
| `edgedash/sources/http.py` | Shared HTTP helper: timeout, retries, rate-limit, User-Agent |
| `edgedash/sources/arbeitnow.py` | Arbeitnow free board (no key needed) |
| `edgedash/sources/apify.py` | Apify / Indeed scraper (`APIFY_TOKEN` in `.env`) |
| `edgedash/llm.py` | Single LLM gateway; 503 retries; daily quota detected fast |
| `edgedash/scoring.py` | Four-component deterministic scorer; model never touches the number |
| `edgedash/skills.py` | `canonical()` pipeline; `--audit`; `--suggest-aliases` |
| `edgedash/gaps.py` | `--compare` (freq vs cost); `--trend` (earliest → latest snapshot) |
| `edgedash/verdicts.py` | Verification history CLI; `--check <name>` filter |
| `edgedash/query/tools.py` | 7 parameterised read-only query tools; `@tool` registry |
| `edgedash/query/ask.py` | Two-call NL pipeline: route → execute → phrase |
| `edgedash/diagnose.py` | Read-only DB health check |
| `edgedash/rescore.py` | Wipe scores without touching extraction cache |
| `app.py` | Streamlit dashboard — read-only, verified-cycle data, health status bar |
| `run_cycle.py` | Entry point: `--dry-run`, `--force`, `--explain` |
| `.github/workflows/cycle.yml` | Daily cron + manual trigger; 10-min timeout; health check step |
| `tests/` | 192 tests — scorer, skills, planning, verification, query tools |

---

## Deployment

### Environment variables

| Variable | Where to set | Required |
|---|---|---|
| `DATABASE_URL` | Streamlit secrets · GitHub Actions secret | Yes |
| `GEMINI_API_KEY` | Streamlit secrets · GitHub Actions secret | Yes |
| `APIFY_TOKEN` | GitHub Actions secret (optional) | No |

Streamlit Community Cloud: App → Settings → Secrets (TOML format):
```toml
DATABASE_URL  = "postgresql://user:pass@host:6543/postgres?pgbouncer=true"
GEMINI_API_KEY = "..."
```

GitHub Actions: Settings → Secrets and variables → Actions → New repository secret.

### Scheduled job

`.github/workflows/cycle.yml` runs at **06:00 IST / 00:30 UTC** daily.
Manual trigger: `gh workflow run cycle.yml`

The job:
1. Migrates the database (`--migrate` is idempotent)
2. Runs the full cycle
3. Runs `python -m edgedash.health` — fails the job if the system is unhealthy
4. Exports the cycle log as a 14-day artifact

---

## Setup (local development)

Requires **Python 3.11+**.

```bash
git clone https://github.com/BuildsByDhruv/job-scorer.git
cd job-scorer
pip install -r requirements.txt
cp .env.example .env   # add GEMINI_API_KEY, optionally APIFY_TOKEN
```

Edit `config.yaml` — role, city, skills, seniority, sources, alias map.
Without `DATABASE_URL` set, the app falls back to local SQLite (`edgedash.db`).

```bash
python -m edgedash.storage --migrate   # create tables
python run_cycle.py                    # run a full cycle
python -m streamlit run app.py         # open the dashboard
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

### Health and diagnostics

| Command | What it does |
|---|---|
| `python -m edgedash.health` | Run all 4 health checks; exits 1 if any fail |
| `python -m edgedash.storage --check` | Backend type, connectivity, row counts per table |
| `python -m edgedash.storage --migrate` | Create / update all tables (idempotent) |
| `python -m edgedash.diagnose` | Counts, duplicates, quality issues |

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

### Maintenance

| Command | What it does |
|---|---|
| `python -m edgedash.agents.scorer --limit 5` | Score N listings manually |
| `python -m edgedash.rescore --id <id>` | Clear one listing's score |
| `python -m edgedash.rescore --all` | Clear all scores (keeps extraction cache) |
| `python -m edgedash.llm --check` | Verify LLM provider and model |
| `python -m pytest tests/` | Run the full test suite (192 tests, ~9s) |

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
at the same raw frequency rank differently if the listings behind them differ in quality.

---

## How verification works

After every cycle the Verifier runs four deterministic checks — no LLM involved:

| Check | What it catches | Threshold (`config.yaml`) |
|---|---|---|
| `score_spread` | All scores in a narrow band — model inflation | `min_score_spread: 10`, `min_score_stdev: 5` |
| `extraction_sanity` | Broken extractor or model returned a sentence as a skill list | `max_empty_extraction_pct: 20`, `max_skills_per_listing: 30` |
| `gap_sample_size` | Top gap computed from too few listings — ranking a rumour | `min_gap_sample: 1` |
| `freshness` | Database stale — fetcher hasn't run recently | `max_data_age_days: 3` |

On failure: retry the responsible agent once with adjusted context, then re-verify.
If it fails again: mark cycle **degraded**, stop. The dashboard always shows the
last **passing** cycle — stale verified data beats fresh unverified data.

---

## How health reporting works

`python -m edgedash.health` runs four read-only checks and exits non-zero if any fail:

| Check | Fails when |
|---|---|
| `db_reachable` | Database cannot be queried |
| `data_freshness` | Newest listing is older than 3 days |
| `cycle_recency` | No successful cycle in the last 48 hours |
| `verification_streak` | Last 3 cycles all failed verification |

The GitHub Actions workflow runs this after every cycle. A failing health check
fails the job and triggers a GitHub notification — no external monitoring service needed.

The dashboard shows a one-line status bar (green / amber / red) at the top of every page.

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

---

## How the query pipeline works

The "Ask your data" panel in the dashboard runs a two-call pipeline:

1. **Route** — one LLM call picks a tool from a fixed registry of 7 parameterised
   read-only functions. The model never sees SQL, table names, or column names.
2. **Execute** — the tool runs with validated, clamped parameters. Model-supplied
   values are untrusted input.
3. **Phrase** — one LLM call turns the returned rows into 2–3 sentences using
   only the numbers present in the data.

Every answer displays the raw rows alongside the prose. If no tool matches the
question, a fixed message lists what can be asked — the model never guesses.

---

## Design decisions

**Storage is isolated behind one module.**
`edgedash/storage.py` is the only file permitted to import a database driver.
Switching from SQLite to Postgres required changing exactly one file.

**Listing IDs are stable hashes.**
`SHA-256(source + url)` is the primary key. The same job re-fetched later hits
`INSERT OR IGNORE` / `ON CONFLICT DO NOTHING` silently.

**The Orchestrator is state-driven, not sequence-driven.**
`build_plan` is a pure function of `(state, config)`. Skips are explicit plan
entries with the state value that caused them.

**Scoring is split: model extracts facts, Python scores them.**
The model never sees weights and never produces a number. Scores are reproducible,
auditable, and free to recompute when you tune the weights.

**Verification is deterministic and has no LLM.**
A model cannot be the judge of a model's output. All thresholds live in `config.yaml`
with comments naming the failure mode each one catches.

**Gap history is append-only by design.**
`skill_gap_snapshots` is `INSERT`-only. Each run gets a UUID. Trend data exists
because it was never overwritten.

**The dashboard reads from the last passing cycle only.**
Stale verified data always beats fresh unverified data. When the latest cycle fails,
the dashboard shows a warning banner with both timestamps.

**The scheduler and dashboard are separate processes.**
They share only the database. The dashboard never runs a cycle. The scheduler
never serves a page. A hostile Streamlit startup (empty DB, unreachable DB,
mid-migration) shows a clear status card instead of a stack trace.
