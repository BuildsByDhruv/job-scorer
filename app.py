"""EdgeDash — agent activity dashboard.

Read-only. Every data panel reads from the last PASSING cycle only (rule 38).
The activity log is the deliberate exception — it shows all cycles including
failures, because failures are the point of that panel.

Run with:
    python -m streamlit run app.py
"""

from __future__ import annotations

import textwrap
from datetime import datetime, timezone

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from edgedash.config import load_config
import edgedash.storage as storage

# ── Page config ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="EdgeDash",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Custom CSS ───────────────────────────────────────────────────────────────
st.markdown("""
<style>
/* Tighten default padding */
.block-container { padding-top: 1.5rem; padding-bottom: 2rem; }

/* Metric label smaller */
[data-testid="stMetricLabel"] { font-size: 0.75rem; opacity: 0.7; }
[data-testid="stMetricValue"] { font-size: 1.4rem; font-weight: 700; }

/* Section headers */
.section-header {
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    opacity: 0.5;
    margin-bottom: 0.25rem;
    margin-top: 0.5rem;
}

/* Verdict badge */
.verdict-pass    { color: #4ade80; font-weight: 700; }
.verdict-fail    { color: #f87171; font-weight: 700; }
.verdict-degraded{ color: #f87171; font-weight: 700; }
.verdict-partial { color: #fbbf24; font-weight: 700; }

/* Cycle card */
.cycle-card {
    border-radius: 8px;
    padding: 10px 14px;
    margin-bottom: 6px;
    border-left: 3px solid;
}
.cycle-pass     { background: #0d2218; border-color: #4ade80; }
.cycle-fail     { background: #2a0d0d; border-color: #f87171; }
.cycle-degraded { background: #2a0d0d; border-color: #ef4444; }
.cycle-partial  { background: #221a08; border-color: #fbbf24; }
.cycle-other    { background: #111827; border-color: #374151; }

.cycle-card .cycle-time  { font-size: 0.72rem; opacity: 0.55; font-family: monospace; }
.cycle-card .cycle-main  { display: flex; align-items: baseline; gap: 10px; margin: 2px 0; }
.cycle-card .cycle-v     { font-size: 0.9rem; font-weight: 700; min-width: 72px; }
.cycle-card .cycle-agents{ font-size: 0.82rem; opacity: 0.8; }
.cycle-card .cycle-dur   { font-size: 0.72rem; opacity: 0.45; margin-left: auto; }
.cycle-card .cycle-check { font-size: 0.75rem; opacity: 0.7; margin-top: 3px; font-family: monospace; }

/* Score bar */
.score-bar-wrap { width: 100%; background: #1f2937; border-radius: 4px; height: 6px; }
.score-bar-fill { height: 6px; border-radius: 4px; }

/* Skill gap row */
.gap-row { display:flex; align-items:center; gap:8px; padding:6px 0;
           border-bottom: 1px solid #1f2937; font-size:0.85rem; }
.gap-skill{ min-width:130px; font-weight:600; }
.gap-bar-wrap{ flex:1; background:#1f2937; border-radius:3px; height:8px; }
.gap-bar-fill{ height:8px; border-radius:3px; background: #f87171; }
.gap-cost { min-width:40px; text-align:right; font-size:0.75rem; opacity:0.6; }
.gap-n    { min-width:28px; text-align:right; font-size:0.72rem; opacity:0.45; }
</style>
""", unsafe_allow_html=True)

# ── Config ───────────────────────────────────────────────────────────────────
try:
    _cfg = load_config()
    DB   = _cfg.db_path
    storage.init_db(DB)
    _cfg_ok = True
except Exception as _e:
    _cfg_ok     = False
    _cfg_err    = str(_e)
    DB          = "edgedash.db"

_TTL = 30  # cache TTL seconds

# ── Cached data reads ─────────────────────────────────────────────────────────

@st.cache_data(ttl=_TTL, show_spinner=False)
def _verified_cycle() -> dict | None:
    return storage.get_last_verified_cycle(DB)

@st.cache_data(ttl=_TTL, show_spinner=False)
def _latest_orchestrator_cycle() -> dict | None:
    rows = storage.get_recent_orchestrator_cycles(DB, limit=1)
    return rows[0] if rows else None

@st.cache_data(ttl=_TTL, show_spinner=False)
def _recent_cycles(limit: int = 20) -> list[dict]:
    return storage.get_recent_orchestrator_cycles(DB, limit=limit)

@st.cache_data(ttl=_TTL, show_spinner=False)
def _counts() -> tuple[int, int]:
    total   = storage.count_total(DB)
    unscored = storage.count_unscored(DB)
    return total, unscored

@st.cache_data(ttl=_TTL, show_spinner=False)
def _top_listings(limit: int = 200) -> list[dict]:
    return storage.get_listings(DB, limit=limit, min_score=0)

@st.cache_data(ttl=_TTL, show_spinner=False)
def _top_gaps(limit: int = 10) -> list[dict]:
    return storage.get_latest_gap_snapshot(DB)[:limit]

@st.cache_data(ttl=_TTL, show_spinner=False)
def _source_counts() -> list[dict]:
    return storage.count_by_source(DB)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_ts(iso: str | None, short: bool = False, fallback: str = "—") -> str:
    if not iso:
        return fallback
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if short:
            return dt.strftime("%-d %b %H:%M:%S UTC")   # "25 Aug 17:24:00 UTC"
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, AttributeError):
        # %-d is Linux-only; fall back on Windows
        try:
            return dt.strftime("%d %b %H:%M:%S UTC").lstrip("0") if short else dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception:
            return iso


def _fmt_ts_short(iso: str | None) -> str:
    """E.g. '25 Aug 17:24:00'  — no UTC suffix for tight spaces."""
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%d %b %H:%M:%S")
    except Exception:
        return iso


def _parse_notes(notes: str | None) -> dict[str, str]:
    if not notes:
        return {}
    out: dict[str, str] = {}
    for seg in notes.split("|"):
        seg = seg.strip()
        if ": " in seg:
            k, _, v = seg.partition(": ")
            out[k.strip().lower()] = v.strip()

    if "VERDICT: pass" in notes:
        out["verdict"] = "pass"
    elif "VERDICT: degraded" in notes:
        out["verdict"] = "degraded"
        if "VERDICT: degraded — " in notes:
            out["failed_checks"] = _extract_check_names(notes, "VERDICT: degraded — ")
    elif "VERDICT: fail" in notes:
        out["verdict"] = "fail"
        out["failed_checks"] = _extract_check_names(notes, "VERDICT: fail — ")
    elif "VERDICT: n/a" in notes:
        out["verdict"] = "n/a"
    return out


def _extract_check_names(notes: str, prefix: str) -> str:
    try:
        tail = notes.split(prefix, 1)[1].split(" | ")[0]
        names = []
        for seg in tail.split(";"):
            seg = seg.strip()
            if " observed" in seg:
                names.append(seg.split(" observed")[0].strip())
        return ", ".join(names) if names else tail[:60]
    except (IndexError, ValueError):
        return ""


def _verdict_css(verdict: str, status: str) -> str:
    v = verdict or status
    if v == "pass":       return "cycle-pass"
    if v == "degraded":   return "cycle-degraded"
    if v in ("fail","failed"): return "cycle-fail"
    if v == "partial":    return "cycle-partial"
    return "cycle-other"


def _verdict_label(verdict: str, status: str) -> str:
    v = verdict or status
    mapping = {
        "pass":     '<span class="verdict-pass">✓ pass</span>',
        "fail":     '<span class="verdict-fail">✗ fail</span>',
        "failed":   '<span class="verdict-fail">✗ fail</span>',
        "degraded": '<span class="verdict-degraded">⚡ degraded</span>',
        "partial":  '<span class="verdict-partial">△ partial</span>',
        "complete": '<span class="verdict-pass">✓ complete</span>',
        "n/a":      '<span style="opacity:.4">– n/a</span>',
        "nothing_to_do": '<span style="opacity:.4">– idle</span>',
    }
    return mapping.get(v, f'<span style="opacity:.5">{v}</span>')


def _score_colour(score: int) -> str:
    if score >= 80: return "#4ade80"
    if score >= 65: return "#86efac"
    if score >= 50: return "#fbbf24"
    if score >= 35: return "#fb923c"
    return "#f87171"


# ── Section 1: Header ─────────────────────────────────────────────────────────

def _render_header() -> None:
    verified = _verified_cycle()
    latest   = _latest_orchestrator_cycle()
    total, unscored = _counts()
    scored   = total - unscored

    # Title row
    col_title, col_meta = st.columns([3, 2])
    with col_title:
        st.markdown("## ⚡ EdgeDash")
    with col_meta:
        if _cfg_ok:
            st.markdown(
                f"<div style='text-align:right;opacity:.5;font-size:.75rem;padding-top:.6rem'>"
                f"{_cfg.target_role} · {_cfg.target_city} · "
                f"refreshes every {_TTL}s"
                f"</div>",
                unsafe_allow_html=True,
            )

    # Stale-data warning
    latest_notes   = _parse_notes(latest["notes"] if latest else None)
    latest_verdict = latest_notes.get("verdict", latest["status"] if latest else "")
    is_fresh_pass  = latest_verdict == "pass"

    if not is_fresh_pass and verified and latest:
        v_ts = _fmt_ts(verified.get("finished_at"))
        l_ts = _fmt_ts(latest.get("finished_at"))
        st.warning(
            f"Most recent cycle ({l_ts}) ended **{latest_verdict}**. "
            f"Data below is from last verified cycle: **{v_ts}**",
            icon="⚠️",
        )
    elif not is_fresh_pass and not verified:
        st.info("No verified cycle yet. Run `python run_cycle.py` to populate.", icon="ℹ️")

    # Metrics row
    ref    = verified or latest
    ref_ts = _fmt_ts(ref["finished_at"] if ref else None, fallback="Never")
    # Shorten timestamp for metric — avoid truncation
    ref_ts_short = _fmt_ts_short(ref["finished_at"] if ref else None)

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    with c1:
        st.metric("Last verified", ref_ts_short)
    with c2:
        st.metric("Total listings", f"{total:,}")
    with c3:
        st.metric("Scored", f"{scored:,}")
    with c4:
        pct = f"{100*scored//total}%" if total else "0%"
        st.metric("Coverage", pct)
    with c5:
        st.metric("Unscored", f"{unscored:,}")
    with c6:
        if is_fresh_pass:
            st.metric("Verdict", "✅  pass")
        elif latest_verdict in ("degraded", "fail", "failed"):
            st.metric("Verdict", "❌  " + latest_verdict)
        elif not verified:
            st.metric("Verdict", "⏳  no data")
        else:
            st.metric("Verdict", f"⚠️  {latest_verdict}")

    # Source breakdown — compact inline pills
    sources = _source_counts()
    if sources:
        pills = "  ".join(
            f"<span style='background:#1f2937;padding:2px 8px;border-radius:12px;"
            f"font-size:.72rem;opacity:.7'>{s['source']} {s['count']}</span>"
            for s in sources
        )
        st.markdown(pills, unsafe_allow_html=True)

    st.divider()


# ── Section 2: Activity log ───────────────────────────────────────────────────

def _render_activity_log() -> None:
    st.markdown('<div class="section-header">Agent activity log</div>', unsafe_allow_html=True)

    cycles = _recent_cycles(20)

    if not cycles:
        st.info("No cycles yet. Run `python run_cycle.py`.", icon="ℹ️")
        return

    pass_count = 0
    check_freq: dict[str, int] = {}

    for cy in cycles:
        notes   = _parse_notes(cy.get("notes"))
        verdict = notes.get("verdict", cy.get("status", ""))
        status  = cy.get("status", "")
        css     = _verdict_css(verdict, status)
        v_html  = _verdict_label(verdict, status)
        ts      = _fmt_ts_short(cy.get("finished_at") or cy.get("started_at"))
        agents  = notes.get("ran", "—")
        dur     = notes.get("total", "")
        retries = notes.get("retries", "0").strip()
        checks  = notes.get("failed_checks", "")

        if verdict == "pass":
            pass_count += 1
        for ch in (checks or "").split(","):
            ch = ch.strip()
            if ch:
                check_freq[ch] = check_freq.get(ch, 0) + 1

        retry_badge = (
            f'<span style="font-size:.7rem;background:#7c2d12;color:#fed7aa;'
            f'padding:1px 6px;border-radius:10px;margin-left:6px">↺ {retries}</span>'
            if retries and retries != "0" else ""
        )
        dur_html = f'<span class="cycle-dur">{dur}</span>' if dur else ""
        agents_html = (
            f'<span class="cycle-agents">{agents}</span>' if agents and agents != "—" else ""
        )
        checks_html = (
            f'<div class="cycle-check">⚠ {checks}</div>' if checks else ""
        )

        st.markdown(
            f'<div class="cycle-card {css}">'
            f'  <div class="cycle-time">{ts}</div>'
            f'  <div class="cycle-main">'
            f'    <span class="cycle-v">{v_html}</span>'
            f'    {agents_html}'
            f'    {retry_badge}'
            f'    {dur_html}'
            f'  </div>'
            f'  {checks_html}'
            f'</div>',
            unsafe_allow_html=True,
        )

    # Summary bar
    total_cy = len(cycles)
    pct      = 100 * pass_count // total_cy if total_cy else 0
    top_fail = max(check_freq, key=check_freq.get) if check_freq else None

    summary_parts = [f"**{pass_count}/{total_cy}** passed ({pct}%)"]
    if top_fail:
        summary_parts.append(f"most failing check: **{top_fail}** ({check_freq[top_fail]}×)")
    st.caption("  ·  ".join(summary_parts))

    st.divider()


# ── Section 3a: Top listings ──────────────────────────────────────────────────

def _render_top_listings() -> None:
    st.markdown('<div class="section-header">Top scored listings</div>', unsafe_allow_html=True)

    all_l  = _top_listings()
    scored = sorted(
        [r for r in all_l if r.get("fit_score") is not None],
        key=lambda r: r["fit_score"],
        reverse=True,
    )[:10]

    if not scored:
        st.info("No scored listings yet.", icon="ℹ️")
        return

    for r in scored:
        score  = r["fit_score"]
        colour = _score_colour(score)
        reason = textwrap.shorten(r.get("fit_reason") or "", width=90, placeholder="…")
        title  = r["title"]
        co     = r["company"]
        loc    = r.get("location") or ""
        url    = r["url"]
        bar_w  = score  # score is 0-100

        reason_html = (
            f'<div style="font-size:.75rem;opacity:.55;margin-top:3px;padding-left:46px">'
            f'{reason}</div>'
        ) if reason else ""

        st.markdown(
            f'<div style="padding:8px 0;border-bottom:1px solid #1f2937">'
            f'  <div style="display:flex;align-items:center;gap:10px">'
            f'    <span style="font-size:1.1rem;font-weight:700;color:{colour};'
            f'          min-width:36px;text-align:right">{score}</span>'
            f'    <div style="flex:1;min-width:0">'
            f'      <div style="font-weight:600;white-space:nowrap;overflow:hidden;'
            f'           text-overflow:ellipsis">'
            f'        <a href="{url}" target="_blank" style="color:inherit;text-decoration:none">'
            f'          {title}'
            f'        </a>'
            f'      </div>'
            f'      <div style="font-size:.78rem;opacity:.6">{co}'
            f'        {(" · " + loc) if loc else ""}'
            f'      </div>'
            f'      <div class="score-bar-wrap" style="margin-top:4px">'
            f'        <div class="score-bar-fill" style="width:{bar_w}%;background:{colour}"></div>'
            f'      </div>'
            f'    </div>'
            f'  </div>'
            f'  {reason_html}'
            f'</div>',
            unsafe_allow_html=True,
        )


# ── Section 3b: Skill gaps ────────────────────────────────────────────────────

def _render_top_gaps() -> None:
    st.markdown('<div class="section-header">Skill gaps</div>', unsafe_allow_html=True)

    gaps = _top_gaps(10)

    if not gaps:
        st.info("No gap snapshot yet.", icon="ℹ️")
        return

    computed_at = gaps[0].get("computed_at") if gaps else None
    if computed_at:
        st.caption(f"Snapshot: {_fmt_ts_short(computed_at)}")

    max_cost = max((g["opportunity_cost"] for g in gaps), default=1) or 1

    for g in gaps:
        skill    = g["skill"]
        n        = g["listings_blocked"]
        cost     = g["opportunity_cost"]
        ms       = g["mean_score"]
        low_conf = g.get("low_confidence", False)
        pct      = int(100 * cost / max_cost)
        conf_tag = (
            '<span style="font-size:.65rem;opacity:.5;margin-left:4px">low conf</span>'
            if low_conf else ""
        )

        st.markdown(
            f'<div class="gap-row">'
            f'  <span class="gap-skill">{skill}{conf_tag}</span>'
            f'  <div class="gap-bar-wrap">'
            f'    <div class="gap-bar-fill" style="width:{pct}%"></div>'
            f'  </div>'
            f'  <span class="gap-cost">{cost:.1f}</span>'
            f'  <span class="gap-n">{n} listings</span>'
            f'  <span style="font-size:.72rem;opacity:.4;min-width:50px;text-align:right">'
            f'    avg {ms:.0f}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )


# ── Layout ────────────────────────────────────────────────────────────────────

_render_header()
_render_activity_log()

col_l, col_r = st.columns([3, 2], gap="large")
with col_l:
    _render_top_listings()
with col_r:
    _render_top_gaps()
