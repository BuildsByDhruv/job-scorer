# EdgeDash

> **Your job search, running while you sleep.**

EdgeDash is an autonomous career intelligence agent. Every day it fetches live
job listings, scores each one against your profile, ranks your skill gaps by the
cost they're actually extracting from you, and surfaces the delta since yesterday.
You wake up to a ranked table, not a pile of unread postings.

---

## What it actually does

```
08:00  →  Fetcher pulls 37 new listings from Arbeitnow
           4 are genuinely new (stable-hash deduplication)

08:01  →  Scorer extracts facts via LLM (one call per unique description)
           then scores deterministically — no model touches the number
           scored 25 · range 31-84 · mean 61 · spread OK

08:02  →  GapAnalyzer compares required skills to your profile
           ranks gaps by Σ(fit_score/100) — a gap in an 85-score listing
           outweighs a gap in a 20-score listing, even at equal frequency

08:02  →  Writes one timestamped snapshot to skill_gap_snapshots
           Previous snapshots never overwritten — trend data is permanent
```

Then you run:

```
python -m edgedash.gaps
```

And you see this:

```
════════════════════════════════════════════════════════════════════════
  SKILL GAP REPORT  ·  snapshot: 2026-08-19T08:02:31

   #  SKILL                  BLOCKED  OPP. COST   MEAN   TOP  BAR
   1  kubernetes                  31      24.10     78    91  ████████████████████
   2  terraform                   18      13.40     74    88  ███████████░░░░░░░░░
   3  ci/cd                       22      12.90     59    84  ██████████░░░░░░░░░░
════════════════════════════════════════════════════════════════════════
```

---

## Architecture

```
run_cycle.py
    │
    ├─ state.py         read_state(config, now) → SystemState
    │                   cheap MAX/COUNT queries, clock is a parameter
    │
    ├─ planning.py      build_plan(state, config) → Plan
    │                   pure function, no I/O, skips are explicit
    │
    └─ orchestrator.py  executes the plan, wraps each agent in try/except,
                        writes exactly one summary row, marks cycle partial
                        if any agent fails — never stops the cycle
                             │
                             ├── Fetcher       one source = one try/except
                             ├── Scorer        LLM extract → deterministic score
                             └── GapAnalyzer   pure arithmetic, no model
                                      │
                                      └── storage.py  the ONLY file that
                                                       touches sqlite3
```

**Adding a fourth agent: one registry entry + one decision block in `build_plan`.
Nothing else changes.**

---

## Current status

### Built
| Module | What it does |
|---|---|
| `edgedash/config.py` | `Config` dataclass from `config.yaml`; `skill_aliases` map |
| `edgedash/storage.py` | SQLite behind a thin interface; `skill_gap_snapshots` append-only |
| `edgedash/state.py` | `read_state(config, now)` — `now` is a param, fully testable |
| `edgedash/planning.py` | `build_plan(state, config)` pure function; explicit skip reasons |
| `edgedash/orchestrator.py` | State-driven cycle; one summary row; `partial` on any failure |
| `edgedash/agents/base.py` | `Agent` protocol; `stop_conditions` passed by Orchestrator |
| `edgedash/agents/fetcher.py` | Live fetcher; per-source isolation; respects `max_listings` |
| `edgedash/agents/mock_fetcher.py` | Offline mock; `use_mock_fetcher: true` in config |
| `edgedash/agents/scorer.py` | Batch scorer; quota-exhaustion stops immediately, not per-listing |
| `edgedash/agents/extractor.py` | LLM extraction with description-hash cache |
| `edgedash/agents/gap_analyzer.py` | Deterministic gap ranking by opportunity cost |
| `edgedash/sources/base.py` | `Source` protocol; `@register` decorator |
| `edgedash/sources/http.py` | Shared HTTP helper: timeout, retries, rate-limit, User-Agent |
| `edgedash/sources/arbeitnow.py` | Arbeitnow free board (no key) |
| `edgedash/sources/apify.py` | Apify / Indeed scraper (`APIFY_TOKEN` in `.env`) |
| `edgedash/llm.py` | Single LLM gateway; 503 retries; daily quota detected and stopped fast |
| `edgedash/scoring.py` | Four-component deterministic scorer; model never touches the number |
| `edgedash/skills.py` | `canonical()` pipeline; `--audit`; `--suggest-aliases` |
| `edgedash/gaps.py` | `--compare` (freq vs cost); `--trend` (earliest → latest snapshot) |
| `edgedash/diagnose.py` | Read-only DB health check |
| `edgedash/rescore.py` | Wipe scores without touching extraction cache |
| `run_cycle.py` | Entry point: `--dry-run`, `--force`, `--explain` |
| `tests/test_scoring.py` | 25 tests — deterministic scorer |
| `tests/test_skills.py` | 26 tests — `canonical()` |
| `tests/test_planning.py` | 32 tests — `build_plan()` and `Plan.render()` |

### Remaining
- [ ] `Verifier` agent — consistency check before committing results
- [ ] Streamlit dashboard — read-only view of scored listings and gaps

### Week 4
- [ ] Swap SQLite → hosted Postgres (one-file change in `storage.py`)
- [ ] Deploy scheduled trigger (cron / cloud scheduler)

---

## Setup

Requires **Python 3.11+**.

```bash
pip install -r requirements.txt
cp .env.example .env   # then add GEMINI_API_KEY and optionally APIFY_TOKEN
```

Edit `config.yaml` — role, city, skills, seniority, sources, alias map. Every
user-specific value lives there. Nothing is hardcoded.

```bash
python run_cycle.py
```

---

## Commands

### Cycle control

| Command | What it does |
|---|---|
| `python run_cycle.py` | Run a full state-driven cycle |
| `python run_cycle.py --dry-run` | Print the plan, exit — zero writes, zero API calls |
| `python run_cycle.py --dry-run --explain` | Show every state value + the decision it drove, then exit |
| `python run_cycle.py --explain` | Same breakdown, then execute |
| `python run_cycle.py --force scorer` | Force scorer even if nothing is unscored |
| `python run_cycle.py --force fetcher --force scorer` | Force multiple agents |

### Gap intelligence

| Command | What it does |
|---|---|
| `python -m edgedash.gaps` | Latest gap snapshot, ranked by opportunity cost |
| `python -m edgedash.gaps --compare` | Both rankings side by side — frequency vs cost |
| `python -m edgedash.gaps --trend` | How the top 10 gaps moved since the first snapshot |

### Skill maintenance

| Command | What it does |
|---|---|
| `python -m edgedash.skills --audit` | 40 most common raw strings + singletons (typos/junk) |
| `python -m edgedash.skills --suggest-aliases` | One model call → ready-to-paste YAML; writes nothing |

### Diagnostics and maintenance

| Command | What it does |
|---|---|
| `python -m edgedash.diagnose` | Counts, duplicates, quality issues |
| `python -m edgedash.agents.scorer --limit 5` | Score N listings manually |
| `python -m edgedash.rescore --id <id>` | Clear one listing's score |
| `python -m edgedash.rescore --all` | Clear all scores (prompts for confirmation) |
| `python -m edgedash.llm --check` | Verify LLM provider and model |
| `python -m pytest tests/` | Run the test suite (83 tests, ~0.5s) |

---

## How scoring works

**Step 1 — Extract** (one LLM call per unique description, cached forever):
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
touching the extraction cache. Next cycle re-scores from cached facts at zero
API cost.

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
quality. `--compare` shows the two rankings side by side so you can see exactly
what moved and why.

---

## How the Orchestrator decides

```
state.hours_since_fetch >= fetch_interval_hours  →  ▶ RUN  fetcher
state.unscored_count > 0                         →  ▶ RUN  scorer
state.gaps_stale OR gaps_computed_at is None     →  ▶ RUN  gap_analyzer
otherwise                                        →  ○ SKIP  (this is a success)
```

Every skip is explicit and logged with the state value that caused it. Use
`--explain` to see the full breakdown. Use `--force <agent>` to override a skip;
the override is recorded in the cycle summary row.

---

## Skill canonicalisation

Raw strings from job listings go through a normalisation pipeline before any
comparison:

```
"Kubernetes (EKS)"  →  lowercase  →  drop (qualifier)  →  strip punctuation
                    →  collapse whitespace  →  alias lookup  →  "kubernetes"
```

The alias map in `config.yaml` is yours to own. The model never merges names.
Run `--audit` to find collisions. Run `--suggest-aliases` to get model proposals
— it prints YAML, writes nothing, and flags conflicts with your existing map.

---

## LLM configuration

```yaml
llm_provider: "gemini"        # "gemini" or "ollama"
llm_model: "gemini-3.5-flash"
llm_batch_size: 25            # Orchestrator passes this as stop_condition
```

| Scenario | Solution |
|---|---|
| Daily quota hit | `LLMQuotaExhausted` stops the batch immediately — no grinding retries |
| 503 overload | Backs off and retries up to 3× before raising `LLMError` |
| Per-minute 429 | Honours the `retry-after` from the API response |
| No API key | Set `llm_provider: "ollama"` and run locally |

---

## Design decisions

**Storage is isolated behind one module.**
`edgedash/storage.py` is the only file permitted to import `sqlite3`. The backend
moves to Postgres in week 4 with a single-file change.

**Listing IDs are stable hashes.**
`SHA-256(source + url)` is the primary key. The same job re-fetched on a later
run hits `INSERT OR IGNORE` silently. Deduplication is observable, not invisible.

**The Orchestrator is state-driven, not sequence-driven.**
`build_plan` is a pure function of `(state, config)`. Skips are explicit entries
in the plan with the state value that caused them. One agent failing marks the
cycle `partial` — it never stops the remaining agents.

**Scoring is split: model extracts facts, Python scores them.**
The model never sees weights and never produces a number. Scores are reproducible,
auditable, and free to recompute when you tune the weights.

**Gap history is append-only by design.**
`skill_gap_snapshots` is `INSERT`-only. Each run gets a UUID. If you want trend
data, you cannot retrofit it — the append-only design is why it exists at all.

**`--dry-run` is the preview tool.**
Read state, print the plan, exit. No side effects. The correct habit before
running `--force` on a production DB.

**The model may suggest aliases but never applies them.**
`--suggest-aliases` makes one call, prints ready-to-paste YAML, and exits. You
own the alias map. The system never merges skill names without your explicit edit.
