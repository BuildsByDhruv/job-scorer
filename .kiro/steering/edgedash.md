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

## Style

- Small, testable functions over large monolithic ones.
- Plain, readable Python over clever Python.
- When asked to build one module, build that module only — do not scaffold the whole application.
