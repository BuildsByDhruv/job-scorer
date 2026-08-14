# EdgeDash — Project Steering Rules

These rules apply to every interaction in this project. Follow them exactly. If a rule needs to change, surface it explicitly and wait for approval before deviating.

---

## Project Overview

**EdgeDash** is an autonomous AI career intelligence agent. It runs on a schedule, fetches live job listings, scores them for fit against a user profile, surfaces skill gaps, verifies its own output, and publishes results to a Streamlit dashboard.

---

## Architecture

```
Trigger (scheduled)
  -> Orchestrator
       -> Fetcher       (sub-agent)
       -> Scorer        (sub-agent)
       -> GapAnalyzer   (sub-agent)
  -> Verifier
  -> Storage
  -> Dashboard (read-only)
```

- The **Orchestrator** reads state and delegates work. It never fetches data or scores jobs directly.
- Each **sub-agent** has exactly one goal and one stop condition.
- The **Dashboard** is read-only; it never writes to storage.
- Do not alter this architecture without explicitly proposing the change and receiving approval.

---

## Hard Rules

1. **Python 3.11+. Standard library first.** Add a third-party dependency only when it genuinely saves real work. State the reason before adding it.

2. **All storage access goes through a single `storage` module with a thin interface.** No other module may import `sqlite3` (or any DB driver) directly. The backend must be swappable from SQLite to hosted Postgres in week 4 with a one-file change.

3. **Never hardcode user-specific values.** Role, city, keywords, skills profile, and any other user-specific data must live in config (a config file or environment variables). Code must be profile-agnostic.

4. **No secrets in code.** Environment variables only, loaded in one place (e.g., a `config.py` or `settings.py` module). No credentials, API keys, or tokens anywhere else.

5. **Every agent run writes a row to `cycle_log`.** Required fields: what ran, timestamp, records touched, pass/fail status, retry reason (if any).

6. **Fail loudly.** No bare `except: pass` or silently swallowed exceptions. If something is wrong, raise or log it visibly. The operator must see failures.

7. **Type hints on every function signature.** Add docstrings only where the intent is not obvious from the name and parameters alone.

8. **Keep files under ~150 lines.** Proactively split a module before it grows past that point.

---

## Network & Sources

9. **Every external source lives behind a `Source` class with a uniform interface.** The Fetcher never contains source-specific parsing logic. Adding a new source must never require editing the Fetcher.

10. **Every `Source` returns a list of normalised dicts with exactly these keys:** `source`, `external_id`, `title`, `company`, `location`, `url`, `description`, `posted_at`, `raw`. Missing values are `None` — never an empty string, never `"N/A"`.

11. **All network calls go through one shared helper** that enforces a 10-second timeout, 2 retry attempts with exponential backoff, and a `User-Agent` header. No bare `requests.get` anywhere else in the codebase.

12. **A source failing must never kill the cycle.** Catch exceptions per-source, log the failure to `cycle_log` with `status="failed"`, and continue to the next source. One dead job board must not stop the others.

13. **Secrets come from environment variables loaded via a `.env` file that is gitignored.** Never a literal key in code, never a key in `config.yaml`. If a required key is missing, that source skips itself with a clear log line — it does not crash the cycle.

14. **Respect the source.** Rate-limit to at most 1 request per second per source, set a real `User-Agent`, and honour any documented page limits.

---

## Intelligence & Scoring

15. **All LLM calls go through one module, `edgedash/llm.py`, exposing one function.** The provider and model name come from config, never hardcoded. Rate-limit to stay inside a free tier (default 1 request per second, max 15 per minute). No other file imports an LLM SDK.

16. **Never ask a model for a final score, ranking, or numeric rating.** The model extracts structured facts only. All scoring arithmetic is deterministic Python in one function. The model never sees the scoring weights.

17. **Every model response is validated against an explicit schema before use.** A response that fails validation is retried once, then logged as a failure for that listing only — it must not crash the cycle or stop the remaining listings. Never `json.loads` raw model text without a validation and repair path.

18. **Scoring is idempotent.** Never re-score a listing that already has a score. Select only listings `WHERE fit_score IS NULL`. Cache extraction results keyed on a hash of the job description so the same text is never sent to the model twice.

19. **Every score carries a human-readable reason generated from the score components by our code** — never free text written by the model.

20. **Log the score distribution (count, min, max, mean, spread) to `cycle_log` on every scoring run.** A run where all scores fall within 10 points is a suspect run and must be logged as such.

21. **Cap listings scored per cycle at a configurable batch size (default 25)** so a cost or rate-limit blowup is structurally impossible.

---

## Aggregate Analysis

22. **Aggregate analysis is deterministic SQL and Python.** No LLM call may produce, adjust, or rank an aggregate number. A model may only SUGGEST canonical groupings for a human to approve.

23. **Skill names are canonicalised through an explicit alias map in `config.yaml` that I own and can read.** Never auto-merge skill names by model judgement or string similarity alone.

24. **Gap ranking is weighted by the fit score of the listing the gap came from.** A gap in a listing scored 20 is worth far less than a gap in a listing scored 85. Never rank gaps by raw frequency alone.

25. **Every gap report run writes a timestamped snapshot.** Never overwrite the previous report. Trend over time is a first-class output, not an afterthought.

26. **Every aggregate number must be traceable to the rows that produced it.** Any reported gap must be able to list the specific listing IDs it was computed from. No number appears in the dashboard that cannot be drilled into.

27. **Report the sample size alongside every aggregate.** A gap computed from 3 listings and a gap computed from 90 listings must never be presented as equally reliable.

---

## Style

- Small, testable functions over large monolithic ones.
- Plain, readable Python over clever Python.
- When asked to build one module, build that module only — do not scaffold the whole application.
