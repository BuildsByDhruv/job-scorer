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
    |-- Scorer        scores each listing for fit against your profile
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
- [x] `edgedash/orchestrator.py` — reads state, plans, runs registered agents, logs every run to `cycle_log`
- [x] `edgedash/diagnose.py` — read-only diagnostic: counts, cross-source duplicates, recent listings, quality issues
- [x] `run_cycle.py` — single entry point

### Week 2 (remaining)
- [ ] `Scorer` agent (LLM or rule-based fit scoring)

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

Copy `.env.example` to `.env` and fill in any API keys you want to use:

```bash
cp .env.example .env
# then edit .env and set APIFY_TOKEN=your_token_here
```

Edit `config.yaml` to set your target role, city, skills, and enabled sources.
Every user-specific value lives there — nothing is hardcoded.

```bash
python run_cycle.py
```

Running it twice in a row will show deduplication in action: the second run
reports `0 new rows` for any listings already in the database.

To inspect the database without running a cycle:

```bash
python -m edgedash.diagnose
```

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

**Secrets load in one place.**
`run_cycle.py` calls `load_dotenv()` before any other import. No other file reads
`.env`. If a key is missing, the relevant source skips itself with a log line
rather than raising.
