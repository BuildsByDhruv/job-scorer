"""Parameterised read-only query tool registry.

Rules enforced here:
  40 — no SQL generation from a model; every query is a function I wrote
  41 — every parameter is validated and clamped before use
  42 — model routes (picks a tool) and phrases (turns rows to prose); never touches DB
  43 — phrasing call uses only numbers from the returned rows
  44 — rows are always returned alongside any prose
  45 — unknown tool → list what is available; never guess
  46 — all reads from the last passing cycle only

Public API
----------
TOOLS : dict[str, ToolSpec]
    Registry of all tools.  Keys are tool names.

call(name, params, db, aliases) -> ToolResult
    Validate, clamp, execute, return rows + summary.
    Raises ToolNotFound for unknown names.
    Never raises on bad parameter values — clamps silently instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import edgedash.storage as storage
from edgedash.skills import canonical


# ---------------------------------------------------------------------------
# Registry types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ParamSpec:
    """Schema for one tool parameter — used by the router model to fill values."""
    type: str                        # "int" | "str"
    description: str
    default: Any
    # For ints only:
    min: int | None = None
    max: int | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str                 # what the router model reads to pick this tool
    params: dict[str, ParamSpec]
    fn: Callable                     # the actual implementation


@dataclass
class ToolResult:
    tool: str
    rows: list[dict[str, Any]]       # always returned alongside prose (rule 44)
    summary: str                     # "47 listings from the last 7 days"
    params_used: dict[str, Any]      # the clamped, validated params that ran


class ToolNotFound(Exception):
    """Raised when the router names a tool that is not in the registry."""


# ---------------------------------------------------------------------------
# Internal registry (populated by @tool)
# ---------------------------------------------------------------------------

TOOLS: dict[str, ToolSpec] = {}


# ---------------------------------------------------------------------------
# @tool decorator
# ---------------------------------------------------------------------------

def tool(
    name: str,
    description: str,
    params: dict[str, ParamSpec],
) -> Callable:
    """Register a query function in TOOLS.

    Usage
    -----
    @tool(
        name="best_matches",
        description="...",
        params={"n": ParamSpec(type="int", description="...", default=10, min=1, max=25)},
    )
    def _best_matches(n: int, db: str, aliases: dict) -> ToolResult:
        ...

    The decorated function is stored unchanged; the decorator only registers it.
    Parameters are validated and clamped by `call()`, not by the function itself,
    so each implementation can trust its arguments are already safe.
    """
    def decorator(fn: Callable) -> Callable:
        TOOLS[name] = ToolSpec(
            name=name,
            description=description,
            params=params,
            fn=fn,
        )
        return fn
    return decorator


# ---------------------------------------------------------------------------
# Parameter validation + clamping (rule 41)
# ---------------------------------------------------------------------------

def _clamp_int(value: Any, spec: ParamSpec) -> int:
    """Convert *value* to int and clamp to [spec.min, spec.max].

    Non-numeric input falls back to spec.default.  Every model-supplied
    integer is untrusted and goes through this path.
    """
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = int(spec.default)
    if spec.min is not None:
        v = max(spec.min, v)
    if spec.max is not None:
        v = min(spec.max, v)
    return v


def _clean_str(value: Any, spec: ParamSpec) -> str:
    """Coerce *value* to str and strip whitespace.

    Falls back to spec.default on None / empty.
    The result is canonicalised and checked against actual DB data in each
    tool implementation — it is NEVER interpolated into a query string.
    """
    if value is None:
        return str(spec.default)
    s = str(value).strip()
    return s if s else str(spec.default)


def _validate_params(
    tool_spec: ToolSpec,
    raw_params: dict[str, Any],
) -> dict[str, Any]:
    """Return a dict of clamped, safe parameter values for *tool_spec*."""
    cleaned: dict[str, Any] = {}
    for pname, spec in tool_spec.params.items():
        raw = raw_params.get(pname, spec.default)
        if spec.type == "int":
            cleaned[pname] = _clamp_int(raw, spec)
        else:
            cleaned[pname] = _clean_str(raw, spec)
    return cleaned


# ---------------------------------------------------------------------------
# Public entry point (rule 42 — model never touches DB directly)
# ---------------------------------------------------------------------------

def call(
    name: str,
    params: dict[str, Any],
    db: str,
    aliases: dict[str, str],
) -> ToolResult:
    """Validate params, clamp to safe ranges, execute the named tool.

    Parameters
    ----------
    name    : tool name from the router model
    params  : raw parameter dict from the router model — treated as untrusted
    db      : path to the SQLite database
    aliases : skill alias map from Config.skill_aliases

    Raises ToolNotFound when *name* is not in TOOLS (rule 45).
    Never raises on bad parameter values — clamps instead (rule 41).
    """
    spec = TOOLS.get(name)
    if spec is None:
        available = ", ".join(sorted(TOOLS))
        raise ToolNotFound(
            f"No tool named '{name}'. Available: {available}"
        )

    safe_params = _validate_params(spec, params)
    return spec.fn(**safe_params, db=db, aliases=aliases)


# ---------------------------------------------------------------------------
# Helper: resolve verified DB path (rule 46)
# ---------------------------------------------------------------------------

def _assert_verified(db: str) -> None:
    """Raise RuntimeError when no passing cycle exists yet.

    All tools call this before reading data so a router model can never
    surface unverified data through this interface.
    """
    cycle = storage.get_last_verified_cycle(db)
    if cycle is None:
        raise RuntimeError(
            "No verified cycle found. "
            "Run a full cycle so the Verifier can produce a passing verdict."
        )


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Tool 1 — companies_hiring
# ---------------------------------------------------------------------------

@tool(
    name="companies_hiring",
    description=(
        "List companies that posted job listings within the last N days, "
        "with a count of their listings. Use when the question asks which "
        "companies are actively hiring, who is posting jobs, or how many "
        "listings a specific company has. Default window is 7 days."
    ),
    params={
        "days": ParamSpec(
            type="int",
            description="How many days back to look for listings. Clamped 1–90.",
            default=7,
            min=1,
            max=90,
        ),
    },
)
def _companies_hiring(
    days: int,
    db: str,
    aliases: dict[str, str],
) -> ToolResult:
    _assert_verified(db)

    cutoff = (_now_utc() - timedelta(days=days)).isoformat()

    # get_listings returns all listings ordered by fetched_at desc.
    # We filter to the window in Python — no raw SQL constructed here.
    all_listings = storage.get_listings(db, limit=10_000)
    recent = [
        r for r in all_listings
        if (r.get("fetched_at") or "") >= cutoff
    ]

    # Aggregate by company
    counts: dict[str, int] = {}
    for r in recent:
        co = (r.get("company") or "").strip()
        if co:
            counts[co] = counts.get(co, 0) + 1

    rows = [
        {"company": co, "listings": n}
        for co, n in sorted(counts.items(), key=lambda x: -x[1])
    ]

    summary = (
        f"{len(recent)} listing(s) fetched in the last {days} day(s) "
        f"from {len(rows)} company(s)."
    )
    return ToolResult(
        tool="companies_hiring",
        rows=rows,
        summary=summary,
        params_used={"days": days},
    )


# ---------------------------------------------------------------------------
# Tool 2 — best_matches
# ---------------------------------------------------------------------------

@tool(
    name="best_matches",
    description=(
        "Return the N highest-scoring job listings with score, title, "
        "company, location, and the human-readable score reason. Use when "
        "the question asks for the best jobs, top matches, highest scores, "
        "or which roles fit the profile most. Default is top 10."
    ),
    params={
        "n": ParamSpec(
            type="int",
            description="Number of listings to return. Clamped 1–25.",
            default=10,
            min=1,
            max=25,
        ),
    },
)
def _best_matches(
    n: int,
    db: str,
    aliases: dict[str, str],
) -> ToolResult:
    _assert_verified(db)

    # get_listings with min_score=0 returns scored rows; unscored rows have
    # fit_score=NULL and are excluded by the WHERE clause in storage.
    all_scored = storage.get_listings(db, limit=10_000, min_score=0)
    all_scored = [r for r in all_scored if r.get("fit_score") is not None]
    all_scored.sort(key=lambda r: r["fit_score"], reverse=True)
    top = all_scored[:n]

    rows = [
        {
            "score":    r["fit_score"],
            "title":    r["title"],
            "company":  r["company"],
            "location": r.get("location") or "",
            "url":      r["url"],
            "reason":   r.get("fit_reason") or "",
            "posted_at": r.get("posted_at") or "",
        }
        for r in top
    ]

    summary = (
        f"Top {len(rows)} of {len(all_scored)} scored listing(s). "
        f"Score range in full set: "
        f"{min(r['fit_score'] for r in all_scored)}–"
        f"{max(r['fit_score'] for r in all_scored)}."
        if all_scored else "No scored listings found."
    )
    return ToolResult(
        tool="best_matches",
        rows=rows,
        summary=summary,
        params_used={"n": n},
    )


# ---------------------------------------------------------------------------
# Tool 3 — top_gaps
# ---------------------------------------------------------------------------

@tool(
    name="top_gaps",
    description=(
        "Return the top N skill gaps ranked by weighted opportunity cost "
        "(Σ fit_score/100 of listings that require the skill but the user "
        "lacks it). Use when the question asks which skills to learn, "
        "what is blocking the most jobs, or what skills are most valuable "
        "to acquire. Default is top 5."
    ),
    params={
        "n": ParamSpec(
            type="int",
            description="Number of gaps to return. Clamped 1–25.",
            default=5,
            min=1,
            max=25,
        ),
    },
)
def _top_gaps(
    n: int,
    db: str,
    aliases: dict[str, str],
) -> ToolResult:
    _assert_verified(db)

    all_gaps = storage.get_latest_gap_snapshot(db)
    top = all_gaps[:n]

    rows = [
        {
            "skill":            g["skill"],
            "listings_blocked": g["listings_blocked"],
            "opportunity_cost": round(g["opportunity_cost"], 2),
            "mean_score":       round(g["mean_score"], 1),
            "top_score":        g["top_score"],
            "low_confidence":   g["low_confidence"],
        }
        for g in top
    ]

    computed_at = all_gaps[0]["computed_at"][:10] if all_gaps else "—"
    summary = (
        f"Top {len(rows)} of {len(all_gaps)} skill gap(s) from snapshot "
        f"computed {computed_at}."
        if all_gaps else "No gap snapshot found."
    )
    return ToolResult(
        tool="top_gaps",
        rows=rows,
        summary=summary,
        params_used={"n": n},
    )


# ---------------------------------------------------------------------------
# Tool 4 — gap_detail  (rule 26 drill-down)
# ---------------------------------------------------------------------------

@tool(
    name="gap_detail",
    description=(
        "Show the specific listings blocked by one named skill gap — the "
        "listings that require that skill and that the user does not have. "
        "Use when the question drills into a single skill: 'which jobs need "
        "kubernetes?', 'show me the listings blocked by terraform', or "
        "'what roles require python that I am missing?'. "
        "Parameter skill must be a skill name, not a job title."
    ),
    params={
        "skill": ParamSpec(
            type="str",
            description="The skill name to drill into.",
            default="",
        ),
    },
)
def _gap_detail(
    skill: str,
    db: str,
    aliases: dict[str, str],
) -> ToolResult:
    _assert_verified(db)

    # Canonicalise the model-supplied skill name — never use it raw (rule 41).
    skill_canon = canonical(skill, aliases)

    # Find this skill in the latest snapshot to get the example_ids.
    # The snapshot already contains pre-computed example_ids (rule 26).
    all_gaps = storage.get_latest_gap_snapshot(db)
    gap_row   = next((g for g in all_gaps if g["skill"] == skill_canon), None)

    if gap_row is None:
        # Also try a case-insensitive search in case canonicalisation missed it.
        skill_lower = skill_canon.lower()
        gap_row = next(
            (g for g in all_gaps if g["skill"].lower() == skill_lower),
            None,
        )

    if gap_row is None:
        return ToolResult(
            tool="gap_detail",
            rows=[],
            summary=(
                f"Skill '{skill_canon}' not found in the current gap snapshot. "
                f"It may not be a gap (you may already have it, or no listings "
                f"in the last cycle required it)."
            ),
            params_used={"skill": skill_canon},
        )

    example_ids: list[str] = gap_row.get("example_ids") or []

    # Fetch the actual listing rows for the example IDs.
    # get_listings returns all listings; filter by id in Python — no SQL
    # construction from a model-supplied value (rule 40).
    all_listings = storage.get_listings(db, limit=10_000)
    id_set       = set(example_ids)
    matched      = [r for r in all_listings if r["id"] in id_set]
    matched.sort(key=lambda r: r.get("fit_score") or 0, reverse=True)

    rows = [
        {
            "id":       r["id"],
            "score":    r.get("fit_score"),
            "title":    r["title"],
            "company":  r["company"],
            "location": r.get("location") or "",
            "url":      r["url"],
        }
        for r in matched
    ]

    summary = (
        f"Skill '{skill_canon}': {gap_row['listings_blocked']} listing(s) blocked, "
        f"opportunity cost {gap_row['opportunity_cost']:.2f}, "
        f"{len(rows)} example listing(s) returned."
    )
    return ToolResult(
        tool="gap_detail",
        rows=rows,
        summary=summary,
        params_used={"skill": skill_canon},
    )


# ---------------------------------------------------------------------------
# Tool 5 — trend
# ---------------------------------------------------------------------------

@tool(
    name="trend",
    description=(
        "Show how skill gap opportunity costs have changed over the last N "
        "weeks by comparing the oldest available snapshot within that window "
        "to the latest snapshot. Use when the question asks whether a gap is "
        "growing or shrinking, how skills have moved over time, or which gaps "
        "are new vs persistent. Default is 3 weeks."
    ),
    params={
        "weeks": ParamSpec(
            type="int",
            description="Number of weeks to look back. Clamped 1–12.",
            default=3,
            min=1,
            max=12,
        ),
    },
)
def _trend(
    weeks: int,
    db: str,
    aliases: dict[str, str],
) -> ToolResult:
    _assert_verified(db)

    run_ids = storage.get_snapshot_run_ids(db)   # asc by computed_at

    if not run_ids:
        return ToolResult(
            tool="trend",
            rows=[],
            summary="No gap snapshots found.",
            params_used={"weeks": weeks},
        )

    if len(run_ids) == 1:
        return ToolResult(
            tool="trend",
            rows=[],
            summary=(
                f"Only one snapshot exists (from {run_ids[0]['computed_at'][:10]}). "
                "Trend requires at least two snapshots."
            ),
            params_used={"weeks": weeks},
        )

    latest = run_ids[-1]
    cutoff = (_now_utc() - timedelta(weeks=weeks)).isoformat()

    # Find the oldest snapshot within the requested window.
    in_window = [r for r in run_ids[:-1] if r["computed_at"] >= cutoff]
    earliest  = in_window[0] if in_window else run_ids[0]

    latest_rows   = storage.get_gap_snapshot_by_run(db, latest["run_id"])
    earliest_rows = storage.get_gap_snapshot_by_run(db, earliest["run_id"])

    earliest_by_skill = {r["skill"]: r for r in earliest_rows}

    rows: list[dict[str, Any]] = []
    for r in latest_rows:
        skill    = r["skill"]
        new_cost = r["opportunity_cost"]
        old      = earliest_by_skill.get(skill)
        old_cost = old["opportunity_cost"] if old else None
        delta    = (new_cost - old_cost) if old_cost is not None else None

        rows.append({
            "skill":            skill,
            "cost_earliest":    round(old_cost, 2) if old_cost is not None else None,
            "cost_latest":      round(new_cost, 2),
            "delta":            round(delta, 2) if delta is not None else None,
            "direction":        (
                "rising"   if delta is not None and delta >  0.05 else
                "falling"  if delta is not None and delta < -0.05 else
                "new"      if delta is None else
                "stable"
            ),
            "listings_blocked": r["listings_blocked"],
        })

    earliest_date = earliest["computed_at"][:10]
    latest_date   = latest["computed_at"][:10]
    summary = (
        f"{len(rows)} gap(s) compared from {earliest_date} to {latest_date} "
        f"({weeks}-week window)."
    )
    return ToolResult(
        tool="trend",
        rows=rows,
        summary=summary,
        params_used={"weeks": weeks},
    )


# ---------------------------------------------------------------------------
# Tool 6 — listing_count
# ---------------------------------------------------------------------------

@tool(
    name="listing_count",
    description=(
        "Return total listing counts: all listings, scored, unscored, and "
        "the date of the newest listing. Use when the question asks how many "
        "jobs there are, how many have been scored, how many are waiting to "
        "be scored, or when the last fetch ran."
    ),
    params={},   # no parameters
)
def _listing_count(
    db: str,
    aliases: dict[str, str],
) -> ToolResult:
    _assert_verified(db)

    total    = storage.count_total(db)
    unscored = storage.count_unscored(db)
    scored   = total - unscored
    newest   = storage.last_fetch_time(db)

    rows = [
        {
            "total":    total,
            "scored":   scored,
            "unscored": unscored,
            "coverage_pct": round(100 * scored / total, 1) if total else 0.0,
            "newest_fetched_at": newest or "—",
        }
    ]
    summary = (
        f"{total} total listing(s): {scored} scored ({rows[0]['coverage_pct']}%), "
        f"{unscored} unscored. Newest fetch: {newest or 'unknown'}."
    )
    return ToolResult(
        tool="listing_count",
        rows=rows,
        summary=summary,
        params_used={},
    )


# ---------------------------------------------------------------------------
# Tool 7 — skill_demand
# ---------------------------------------------------------------------------

@tool(
    name="skill_demand",
    description=(
        "Show how often one specific skill appears across all scored listings: "
        "count of listings where it is required, count where it is nice-to-have, "
        "and the mean fit score of those listings. Use when the question asks "
        "how common a skill is, how many jobs require it, or whether it is "
        "required vs optional. Parameter skill must be a skill name."
    ),
    params={
        "skill": ParamSpec(
            type="str",
            description="The skill name to look up.",
            default="",
        ),
    },
)
def _skill_demand(
    skill: str,
    db: str,
    aliases: dict[str, str],
) -> ToolResult:
    _assert_verified(db)

    # Canonicalise before any DB access — never use raw model input (rule 41).
    skill_canon = canonical(skill, aliases)

    if not skill_canon:
        return ToolResult(
            tool="skill_demand",
            rows=[],
            summary="Empty skill name after canonicalisation.",
            params_used={"skill": skill_canon},
        )

    # get_scored_listings_with_cache returns {id, fit_score,
    # required_skills, nice_to_have} for all scored listings.
    scored_with_facts = storage.get_scored_listings_with_cache(db)

    required_scores:   list[int] = []
    nice_scores:       list[int] = []

    for row in scored_with_facts:
        req  = [canonical(s, aliases) for s in (row.get("required_skills") or [])]
        nice = [canonical(s, aliases) for s in (row.get("nice_to_have")    or [])]
        score = row.get("fit_score") or 0

        if skill_canon in req:
            required_scores.append(score)
        elif skill_canon in nice:
            # Only count as nice-to-have when NOT already required.
            nice_scores.append(score)

    req_count  = len(required_scores)
    nice_count = len(nice_scores)
    total_seen = req_count + nice_count

    mean_req  = round(sum(required_scores) / req_count,  1) if req_count  else None
    mean_nice = round(sum(nice_scores)     / nice_count, 1) if nice_count else None

    if total_seen == 0:
        return ToolResult(
            tool="skill_demand",
            rows=[],
            summary=(
                f"Skill '{skill_canon}' not found in any scored listing's "
                "extracted facts. It may not have been extracted, or no listings "
                "require it."
            ),
            params_used={"skill": skill_canon},
        )

    rows = [
        {
            "skill":          skill_canon,
            "required_count": req_count,
            "nice_count":     nice_count,
            "total_seen":     total_seen,
            "mean_score_required":   mean_req,
            "mean_score_nice":       mean_nice,
        }
    ]
    summary = (
        f"Skill '{skill_canon}' appears in {total_seen} scored listing(s): "
        f"{req_count} required (mean score {mean_req}), "
        f"{nice_count} nice-to-have (mean score {mean_nice})."
    )
    return ToolResult(
        tool="skill_demand",
        rows=rows,
        summary=summary,
        params_used={"skill": skill_canon},
    )
