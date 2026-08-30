"""EdgeDash — agent activity dashboard.

Read-only (rule 49). Every data panel reads from the last PASSING cycle only
(rule 38, rule 50). The activity log is the deliberate exception.

Hostile-startup rules (rule 50):
  - DATABASE_URL missing → clear status card, no traceback.
  - Tables empty         → "no cycles yet" message, not an exception.
  - One panel crashing   → that panel shows an error card; others still render.
  - No traceback is ever shown to a visitor.

Run with:
    python -m streamlit run app.py
"""

from __future__ import annotations

import logging
import os
import textwrap
from datetime import datetime, timezone

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

# ── Page config — must be first Streamlit call ────────────────────────────────
st.set_page_config(
    page_title="EdgeDash",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Silence the backend-detection print in Streamlit context (rule 48) ────────
# storage.py calls print() at module import time to log which backend is active.
# That message goes to the server log (fine) but should not surface in the
# Streamlit UI as a stray stdout fragment on some hosting environments.
# We suppress it by redirecting stdout briefly during import.
import io, sys as _sys
_buf = io.StringIO()
_sys.stdout = _buf
try:
    import edgedash.storage as storage
    from edgedash.config import load_config
finally:
    _sys.stdout = _sys.__stdout__
    _backend_log = _buf.getvalue().strip()
    if _backend_log:
        logging.getLogger("edgedash.storage").info(_backend_log)

# ── Custom CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
.block-container { padding-top: 1.5rem; padding-bottom: 2rem; }
[data-testid="stMetricLabel"] { font-size: 0.75rem; opacity: 0.7; }
[data-testid="stMetricValue"] { font-size: 1.4rem; font-weight: 700; }
.section-header {
    font-size: 0.7rem; font-weight: 700; letter-spacing: 0.12em;
    text-transform: uppercase; opacity: 0.5;
    margin-bottom: 0.25rem; margin-top: 0.5rem;
}
.verdict-pass    { color: #4ade80; font-weight: 700; }
.verdict-fail    { color: #f87171; font-weight: 700; }
.verdict-degraded{ color: #f87171; font-weight: 700; }
.verdict-partial { color: #fbbf24; font-weight: 700; }
.cycle-card { border-radius: 8px; padding: 10px 14px; margin-bottom: 6px; border-left: 3px solid; }
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
.score-bar-wrap { width: 100%; background: #1f2937; border-radius: 4px; height: 6px; }
.score-bar-fill { height: 6px; border-radius: 4px; }
.gap-row { display:flex; align-items:center; gap:8px; padding:6px 0; border-bottom: 1px solid #1f2937; font-size:0.85rem; }
.gap-skill{ min-width:130px; font-weight:600; }
.gap-bar-wrap{ flex:1; background:#1f2937; border-radius:3px; height:8px; }
.gap-bar-fill{ height:8px; border-radius:3px; background: #f87171; }
.gap-cost { min-width:40px; text-align:right; font-size:0.75rem; opacity:0.6; }
.gap-n    { min-width:28px; text-align:right; font-size:0.72rem; opacity:0.45; }
.ask-box { background: #0d1117; border: 1px solid #30363d; border-radius: 12px; padding: 20px 24px 16px; margin-bottom: 8px; }
.ask-title { font-size: 1.05rem; font-weight: 700; letter-spacing: .01em; margin-bottom: 2px; }
.ask-sub { font-size: .75rem; opacity: .45; margin-bottom: 14px; }
.answer-card { background: #0d2218; border: 1px solid #1a4731; border-radius: 10px; padding: 16px 20px; margin: 10px 0 6px; font-size: .95rem; line-height: 1.65; }
.answer-card.unanswerable { background: #1a1a1a; border-color: #30363d; color: #8b949e; }
.answer-meta { font-size: .7rem; opacity: .4; margin-top: 8px; display: flex; gap: 16px; }
.db-error-card { background: #1a0a0a; border: 1px solid #7f1d1d; border-radius: 10px; padding: 20px 24px; text-align: center; }
.panel-error { background: #1a1a2e; border: 1px solid #374151; border-radius: 8px; padding: 12px 16px; font-size: .8rem; opacity: .7; }
.footer { text-align: center; font-size: .72rem; opacity: .35; padding: 24px 0 8px; border-top: 1px solid #1f2937; margin-top: 32px; }
</style>
""", unsafe_allow_html=True)

# ── Database / config init ─────────────────────────────────────────────────────
# Never show a traceback. Capture every error and surface it as a status card.

_DB_URL_PRESENT = bool(os.environ.get("DATABASE_URL"))
_cfg_ok = False
_db_ok  = False
_cfg    = None
DB      = "edgedash.db"
_init_error: str | None = None

try:
    _cfg   = load_config()
    DB     = _cfg.db_path
    _cfg_ok = True
except Exception as exc:
    logging.getLogger("edgedash.app").error("Config load failed: %s", exc)
    _init_error = "Configuration file could not be loaded."

if _cfg_ok:
    try:
        storage.init_db(DB)
        _db_ok = True
    except Exception as exc:
        logging.getLogger("edgedash.app").error("DB init failed: %s", exc)
        _init_error = (
            "Database is not reachable."
            if _DB_URL_PRESENT
            else "DATABASE_URL is not set — database not configured."
        )

_TTL = 30  # seconds

# ── Safe data-fetch wrappers ──────────────────────────────────────────────────
# Each returns a safe default on any exception so one failing query
# cannot take down the whole page (rule 50).

def _safe(fn, default, *args, **kwargs):
    """Call fn(*args, **kwargs); return default on any exception."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        logging.getLogger("edgedash.app").warning(
            "Data fetch failed (%s): %s", fn.__name__, exc
        )
        return default


@st.cache_data(ttl=_TTL, show_spinner=False)
def _verified_cycle() -> dict | None:
    return _safe(storage.get_last_verified_cycle, None, DB)

@st.cache_data(ttl=_TTL, show_spinner=False)
def _latest_orchestrator_cycle() -> dict | None:
    rows = _safe(storage.get_recent_orchestrator_cycles, [], DB, limit=1)
    return rows[0] if rows else None

@st.cache_data(ttl=_TTL, show_spinner=False)
def _recent_cycles(limit: int = 20) -> list[dict]:
    return _safe(storage.get_recent_orchestrator_cycles, [], DB, limit=limit)

@st.cache_data(ttl=_TTL, show_spinner=False)
def _counts() -> tuple[int, int]:
    total    = _safe(storage.count_total,    0, DB)
    unscored = _safe(storage.count_unscored, 0, DB)
    return total, unscored

@st.cache_data(ttl=_TTL, show_spinner=False)
def _top_listings(limit: int = 200) -> list[dict]:
    return _safe(storage.get_listings, [], DB, limit=limit, min_score=0)

@st.cache_data(ttl=_TTL, show_spinner=False)
def _top_gaps(limit: int = 10) -> list[dict]:
    rows = _safe(storage.get_latest_gap_snapshot, [], DB)
    return rows[:limit]

@st.cache_data(ttl=_TTL, show_spinner=False)
def _source_counts() -> list[dict]:
    return _safe(storage.count_by_source, [], DB)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_ts(iso: str | None, fallback: str = "—") -> str:
    if not iso:
        return fallback
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return iso


def _fmt_ts_short(iso: str | None) -> str:
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
        names = [
            seg.strip().split(" observed")[0].strip()
            for seg in tail.split(";")
            if " observed" in seg.strip()
        ]
        return ", ".join(names) if names else tail[:60]
    except (IndexError, ValueError):
        return ""


def _verdict_css(verdict: str, status: str) -> str:
    v = verdict or status
    if v == "pass":                  return "cycle-pass"
    if v == "degraded":              return "cycle-degraded"
    if v in ("fail", "failed"):      return "cycle-fail"
    if v == "partial":               return "cycle-partial"
    return "cycle-other"


def _verdict_label(verdict: str, status: str) -> str:
    v = verdict or status
    mapping = {
        "pass":          '<span class="verdict-pass">✓ pass</span>',
        "fail":          '<span class="verdict-fail">✗ fail</span>',
        "failed":        '<span class="verdict-fail">✗ fail</span>',
        "degraded":      '<span class="verdict-degraded">⚡ degraded</span>',
        "partial":       '<span class="verdict-partial">△ partial</span>',
        "complete":      '<span class="verdict-pass">✓ complete</span>',
        "n/a":           '<span style="opacity:.4">– n/a</span>',
        "nothing_to_do": '<span style="opacity:.4">– idle</span>',
    }
    return mapping.get(v, f'<span style="opacity:.5">{v}</span>')


def _score_colour(score: int) -> str:
    if score >= 80: return "#4ade80"
    if score >= 65: return "#86efac"
    if score >= 50: return "#fbbf24"
    if score >= 35: return "#fb923c"
    return "#f87171"


def _panel(fn, *args, **kwargs) -> None:
    """Render a panel function; catch all exceptions and show a tidy error card.

    A stranger must never see a traceback (rule 50). The real exception is
    logged server-side only.
    """
    try:
        fn(*args, **kwargs)
    except Exception as exc:
        logging.getLogger("edgedash.app").error(
            "Panel %s failed: %s", fn.__name__, exc, exc_info=True
        )
        st.markdown(
            f'<div class="panel-error">⚠ This panel could not load. '
            f'Check server logs for details.</div>',
            unsafe_allow_html=True,
        )


# ── DB-not-ready guard ────────────────────────────────────────────────────────

def _render_db_error() -> None:
    """Full-page status card when the database is missing or unreachable."""
    st.markdown("## ⚡ EdgeDash")
    if not _DB_URL_PRESENT:
        st.markdown(
            '<div class="db-error-card">'
            '<h3 style="color:#f87171;margin-bottom:8px">Database not configured</h3>'
            '<p style="opacity:.7">Set the <code>DATABASE_URL</code> environment variable '
            'to your Postgres connection string and redeploy.</p>'
            '<p style="opacity:.5;font-size:.8rem;margin-top:12px">'
            'In Streamlit Community Cloud: App settings → Secrets → add '
            '<code>DATABASE_URL = "postgresql://..."</code></p>'
            '</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<div class="db-error-card">'
            '<h3 style="color:#f87171;margin-bottom:8px">Database unreachable</h3>'
            '<p style="opacity:.7">The database could not be reached at startup. '
            'Check your connection string and database status.</p>'
            '</div>',
            unsafe_allow_html=True,
        )


# ── Section 1: Header ─────────────────────────────────────────────────────────

def _render_header() -> None:
    verified       = _verified_cycle()
    latest         = _latest_orchestrator_cycle()
    total, unscored = _counts()
    scored          = total - unscored

    col_title, col_meta = st.columns([3, 2])
    with col_title:
        st.markdown("## ⚡ EdgeDash")
    with col_meta:
        if _cfg_ok:
            st.markdown(
                f"<div style='text-align:right;opacity:.5;font-size:.75rem;padding-top:.6rem'>"
                f"{_cfg.target_role} · {_cfg.target_city} · refreshes every {_TTL}s"
                f"</div>",
                unsafe_allow_html=True,
            )

    latest_notes   = _parse_notes(latest["notes"] if latest else None)
    latest_verdict = latest_notes.get("verdict", latest["status"] if latest else "")
    is_fresh_pass  = latest_verdict == "pass"

    if not is_fresh_pass and verified and latest:
        st.warning(
            f"Most recent cycle ({_fmt_ts(latest.get('finished_at'))}) ended "
            f"**{latest_verdict}**. Data below is from last verified cycle: "
            f"**{_fmt_ts(verified.get('finished_at'))}**",
            icon="⚠️",
        )
    elif not is_fresh_pass and not verified:
        if total == 0:
            st.info(
                "No data yet. Run `python run_cycle.py` to fetch and score listings.",
                icon="ℹ️",
            )
        else:
            st.info(
                f"{total:,} listings fetched, none scored yet. "
                "Run another cycle to score them.",
                icon="ℹ️",
            )

    ref          = verified or latest
    ref_ts_short = _fmt_ts_short(ref["finished_at"] if ref else None)

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    with c1:
        st.metric("Last verified", ref_ts_short)
    with c2:
        st.metric("Total listings", f"{total:,}")
    with c3:
        st.metric("Scored", f"{scored:,}")
    with c4:
        pct = f"{100 * scored // total}%" if total else "0%"
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

    sources = _source_counts()
    if sources:
        pills = "  ".join(
            f"<span style='background:#1f2937;padding:2px 8px;border-radius:12px;"
            f"font-size:.72rem;opacity:.7'>{s['source']} {s['count']}</span>"
            for s in sources
        )
        st.markdown(pills, unsafe_allow_html=True)

    st.divider()


# ── Section 2: Ask your data ──────────────────────────────────────────────────

_EXAMPLE_QUESTIONS = [
    "What are my top 5 best-matching jobs right now?",
    "Which skills should I learn to unblock the most listings?",
    "Which companies are actively hiring this week?",
]


def _render_ask() -> None:
    st.markdown(
        '<div class="ask-box">'
        '<div class="ask-title">⚡ Ask your data</div>'
        '<div class="ask-sub">Routes to the right tool · phrases from your rows only · '
        'no estimates, no outside knowledge.</div>',
        unsafe_allow_html=True,
    )

    c1, c2, c3 = st.columns(3, gap="small")
    pressed: str | None = None
    for col, q in zip([c1, c2, c3], _EXAMPLE_QUESTIONS):
        with col:
            if st.button(q, use_container_width=True, type="secondary",
                         key=f"ex_{q[:20]}"):
                pressed = q

    question = st.text_input(
        "question",
        value=pressed or st.session_state.get("_ask_q", ""),
        placeholder="Ask anything about your job data…",
        label_visibility="collapsed",
        key="_ask_input",
    )
    if pressed:
        st.session_state["_ask_q"] = pressed

    st.markdown("</div>", unsafe_allow_html=True)

    if not question or not question.strip():
        return

    if not _cfg_ok or not _db_ok:
        st.markdown(
            '<div class="answer-card unanswerable">'
            'Database not available — queries cannot run.</div>',
            unsafe_allow_html=True,
        )
        return

    with st.spinner(""):
        try:
            from edgedash.query.ask import ask as _ask
            answer = _ask(
                question=question.strip(),
                config=_cfg,
                db=DB,
                aliases=_cfg.skill_aliases,
            )
        except Exception as exc:
            # Log detail server-side; show a generic message to the visitor (rule 50)
            logging.getLogger("edgedash.app").error("ask() failed: %s", exc, exc_info=True)
            st.markdown(
                '<div class="answer-card unanswerable">'
                'Query pipeline unavailable. Check server logs.</div>',
                unsafe_allow_html=True,
            )
            return

    if answer.answerable:
        parts = filter(None, [
            f"tool: {answer.tool_used}" if answer.tool_used else "",
            f"confidence: {answer.confidence}" if answer.confidence in ("high", "low") else "",
            answer.summary or "",
        ])
        meta_html = (
            f'<div class="answer-meta">{"  ·  ".join(parts)}</div>'
        )
        st.markdown(
            f'<div class="answer-card">{answer.text}{meta_html}</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f'<div class="answer-card unanswerable">{answer.text}</div>',
            unsafe_allow_html=True,
        )
        return

    if answer.rows:
        import pandas as pd
        with st.expander(
            f"Underlying data — {len(answer.rows)} row(s)", expanded=True
        ):
            df = pd.DataFrame(answer.rows)
            st.dataframe(
                df,
                use_container_width=True,
                height=min(42 * len(answer.rows) + 42, 380),
                hide_index=True,
            )


# ── Section 3: Activity log ───────────────────────────────────────────────────

def _render_activity_log() -> None:
    st.markdown(
        '<div class="section-header">Agent activity log</div>',
        unsafe_allow_html=True,
    )

    cycles = _recent_cycles(20)

    if not cycles:
        st.info(
            "No cycles recorded yet. Run `python run_cycle.py` to start.",
            icon="ℹ️",
        )
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
        agents_html = (
            f'<span class="cycle-agents">{agents}</span>'
            if agents and agents != "—" else ""
        )
        dur_html    = f'<span class="cycle-dur">{dur}</span>' if dur else ""
        checks_html = f'<div class="cycle-check">⚠ {checks}</div>' if checks else ""
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

    total_cy = len(cycles)
    pct      = 100 * pass_count // total_cy if total_cy else 0
    top_fail = max(check_freq, key=check_freq.get) if check_freq else None
    summary_parts = [f"**{pass_count}/{total_cy}** passed ({pct}%)"]
    if top_fail:
        summary_parts.append(
            f"most failing check: **{top_fail}** ({check_freq[top_fail]}×)"
        )
    st.caption("  ·  ".join(summary_parts))
    st.divider()


# ── Section 4a: Top listings ──────────────────────────────────────────────────

def _render_top_listings() -> None:
    st.markdown(
        '<div class="section-header">Top scored listings</div>',
        unsafe_allow_html=True,
    )

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
        score      = r["fit_score"]
        colour     = _score_colour(score)
        reason     = textwrap.shorten(r.get("fit_reason") or "", width=90, placeholder="…")
        reason_html = (
            f'<div style="font-size:.75rem;opacity:.55;margin-top:3px;'
            f'padding-left:46px">{reason}</div>'
        ) if reason else ""

        st.markdown(
            f'<div style="padding:8px 0;border-bottom:1px solid #1f2937">'
            f'  <div style="display:flex;align-items:center;gap:10px">'
            f'    <span style="font-size:1.1rem;font-weight:700;color:{colour};'
            f'          min-width:36px;text-align:right">{score}</span>'
            f'    <div style="flex:1;min-width:0">'
            f'      <div style="font-weight:600;white-space:nowrap;overflow:hidden;'
            f'           text-overflow:ellipsis">'
            f'        <a href="{r["url"]}" target="_blank" '
            f'           style="color:inherit;text-decoration:none">{r["title"]}</a>'
            f'      </div>'
            f'      <div style="font-size:.78rem;opacity:.6">{r["company"]}'
            f'        {(" · " + r.get("location", "")) if r.get("location") else ""}'
            f'      </div>'
            f'      <div class="score-bar-wrap" style="margin-top:4px">'
            f'        <div class="score-bar-fill" '
            f'             style="width:{score}%;background:{colour}"></div>'
            f'      </div>'
            f'    </div>'
            f'  </div>'
            f'  {reason_html}'
            f'</div>',
            unsafe_allow_html=True,
        )


# ── Section 4b: Skill gaps ────────────────────────────────────────────────────

def _render_top_gaps() -> None:
    st.markdown(
        '<div class="section-header">Skill gaps</div>',
        unsafe_allow_html=True,
    )

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


# ── Footer ────────────────────────────────────────────────────────────────────

_GITHUB_REPO = "https://github.com/BuildsByDhruv/job-scorer"


def _render_footer() -> None:
    verified = _verified_cycle()
    last_ts  = _fmt_ts(verified["finished_at"] if verified else None,
                        fallback="no verified cycle yet")
    st.markdown(
        f'<div class="footer">'
        f'Last successful cycle: {last_ts}'
        f'  ·  '
        f'<a href="{_GITHUB_REPO}" target="_blank" '
        f'   style="color:inherit;opacity:.6">GitHub</a>'
        f'</div>',
        unsafe_allow_html=True,
    )


# ── Layout ────────────────────────────────────────────────────────────────────

# Hard gate: if the database is not configured or reachable, show the
# status card and stop rendering. A stranger must never see a traceback.
if not _db_ok:
    _render_db_error()
    st.stop()

_panel(_render_header)
_panel(_render_ask)
_panel(_render_activity_log)

col_l, col_r = st.columns([3, 2], gap="large")
with col_l:
    _panel(_render_top_listings)
with col_r:
    _panel(_render_top_gaps)

_render_footer()
