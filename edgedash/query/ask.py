"""Two-call natural language query pipeline (rules 42-45).

Public API
----------
ask(question, config, db, aliases) -> Answer

Call 1  ROUTE  — model picks a tool and its parameters from the registry.
                 Never sees table names, column names, or SQL.
Call 2  PHRASE — model turns the returned rows into 2-3 sentences.
                 May only use numbers present in the rows; no estimates.

If no tool matches the question (rule 45), the answer is a fixed message
listing what CAN be asked. No model call for phrasing in that case.

Every question is logged to query_log in the storage layer (rule 5 of the
spec: log question, tool chosen, params, answerable, duration).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from edgedash.config import Config
from edgedash.llm import LLMError, complete_json
from edgedash.query.tools import TOOLS, ToolNotFound, ToolResult, call

# ---------------------------------------------------------------------------
# Answer type
# ---------------------------------------------------------------------------


@dataclass
class Answer:
    text: str                         # 2-3 sentence prose (or "can't answer" message)
    rows: list[dict[str, Any]]        # the raw rows that produced the prose (rule 44)
    tool_used: str | None             # None when no tool matched
    params: dict[str, Any]            # clamped params that were executed
    summary: str                      # tool's own summary string
    confidence: str                   # "high" | "low" | "none"
    answerable: bool                  # False when tool is null


# ---------------------------------------------------------------------------
# Routing prompt (shown to user on request above)
# ---------------------------------------------------------------------------

def _build_tool_block() -> str:
    """Render the TOOLS registry as a plain-text block for the routing prompt.

    Format per tool:
        tool_name
          Description: <description>
          Parameters:
            - <name> (int, default N, range M-K): <description>
            - <name> (str, default "x"): <description>

    No SQL, no table names, no column names — only what the model needs
    to decide which tool to call and what to put in the params.
    """
    lines: list[str] = []
    for spec in TOOLS.values():
        lines.append(spec.name)
        lines.append(f"  Description: {spec.description}")
        if spec.params:
            lines.append("  Parameters:")
            for pname, pspec in spec.params.items():
                if pspec.type == "int":
                    range_str = f", range {pspec.min}–{pspec.max}"
                    lines.append(
                        f"    - {pname} (int, default {pspec.default}{range_str}): "
                        f"{pspec.description}"
                    )
                else:
                    lines.append(
                        f"    - {pname} (str, default {pspec.default!r}): "
                        f"{pspec.description}"
                    )
        else:
            lines.append("  Parameters: none")
        lines.append("")   # blank line between tools
    return "\n".join(lines).rstrip()


# This is the routing prompt exactly as sent to the model — the part
# requested for review. It is assembled at module load time from the
# live registry so it is always in sync with the tools.
_TOOL_BLOCK = _build_tool_block()

_ROUTE_PROMPT_TEMPLATE = """\
You are a query router for a job-search intelligence tool.

Your only job is to decide which tool to call, and with what parameters.
You do NOT answer the question. You do NOT write prose. You only route.

AVAILABLE TOOLS
---------------
{tool_block}

QUESTION
--------
{question}

INSTRUCTIONS
------------
1. Read the question carefully and match it to one tool above.
2. Fill in the parameter values from the question. Use each parameter's
   default if the question does not specify a value.
3. If the question cannot be answered by ANY of the tools listed above,
   set "tool" to null. Do NOT pick the closest-sounding tool. Do NOT
   guess. Return null.
4. Set "confidence" to "high" if the match is unambiguous, "low" if you
   are unsure.

Return a JSON object with exactly these fields:
  "tool"       : the tool name as a string, or null
  "params"     : an object of parameter name to value (empty object if tool is null)
  "confidence" : "high" or "low"\
"""

# Schema for the routing response.
# tool is str-or-None: accept both types and validate separately.
_ROUTE_SCHEMA: dict[str, Any] = {
    "tool":       (str, type(None)),
    "params":     dict,
    "confidence": str,
}

# ---------------------------------------------------------------------------
# Phrasing prompt
# ---------------------------------------------------------------------------

_PHRASE_PROMPT_TEMPLATE = """\
You are answering a question about someone's job search.
You have been given data rows retrieved from a job-search database.

QUESTION
--------
{question}

DATA SUMMARY
------------
{summary}

DATA ROWS
---------
{rows_json}

INSTRUCTIONS
------------
Write 2-3 sentences that directly answer the question using the data above.

Rules you MUST follow:
- Use ONLY the numbers present in the data rows above.
- Do NOT estimate, extrapolate, or add information not present in the rows.
- Do NOT use your general knowledge about job markets or salaries.
- If the data rows are empty, say clearly that the data does not contain
  an answer to this question — do not make one up.
- Include the DATA SUMMARY phrase (e.g. "across 47 listings from the last
  7 days") to show what the data covers.
- Be concise. No bullet points. Plain prose only.

Return a JSON object with exactly one field:
  "text" : your 2-3 sentence answer as a single string\
"""

_PHRASE_SCHEMA: dict[str, Any] = {"text": str}

# ---------------------------------------------------------------------------
# "Can't answer" message (rule 45 — no model call, fixed text)
# ---------------------------------------------------------------------------

def _cant_answer_text(question: str) -> str:
    """Return a fixed message listing available tools. No model call."""
    tool_list = "\n".join(
        f"  • {spec.name}: {spec.description.splitlines()[0]}"
        for spec in TOOLS.values()
    )
    return (
        f"That question can't be answered with the available data tools.\n\n"
        f"Questions I can answer:\n{tool_list}"
    )

# ---------------------------------------------------------------------------
# query_log storage (written through storage module per rule 2)
# ---------------------------------------------------------------------------

def _log_query(
    db: str,
    question: str,
    tool_used: str | None,
    params: dict[str, Any],
    answerable: bool,
    duration_s: float,
    error: str | None = None,
) -> None:
    """Write one row to query_log through the storage module (rule 2)."""
    import edgedash.storage as _storage

    _storage.log_query(
        path=db,
        asked_at=datetime.now(timezone.utc).isoformat(),
        question=question[:500],
        tool_used=tool_used,
        params_json=json.dumps(params),
        answerable=answerable,
        duration_s=duration_s,
        error=error[:300] if error else None,
    )

# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def ask(
    question: str,
    config: Config,
    db: str,
    aliases: dict[str, str],
) -> Answer:
    """Run the two-call pipeline and return an Answer.

    Never raises — catches LLMError and tool errors and returns them as
    Answer.text so the dashboard always has something to display.

    Logs every question to query_log regardless of outcome.
    """
    t_start    = time.monotonic()
    tool_used  : str | None      = None
    params_used: dict[str, Any]  = {}
    rows       : list[dict]      = []
    summary    = ""
    confidence = "none"
    answerable = False
    error_msg  : str | None      = None

    try:
        # ── Call 1: ROUTE ────────────────────────────────────────────────────
        route_prompt = _ROUTE_PROMPT_TEMPLATE.format(
            tool_block=_TOOL_BLOCK,
            question=question.strip(),
        )

        try:
            route_resp = complete_json(
                route_prompt,
                _ROUTE_SCHEMA,
                config=config,
                max_retries=1,
            )
        except LLMError as exc:
            error_msg = str(exc)
            text = f"Routing failed: {exc}"
            _log_query(db, question, None, {}, False,
                       time.monotonic() - t_start, error_msg)
            return Answer(
                text=text, rows=[], tool_used=None, params={},
                summary="", confidence="none", answerable=False,
            )

        raw_tool    = route_resp.get("tool")
        raw_params  = route_resp.get("params") or {}
        confidence  = route_resp.get("confidence", "low")

        # ── Validate confidence value ─────────────────────────────────────────
        if confidence not in ("high", "low"):
            confidence = "low"

        # ── No tool matched (rule 45) ─────────────────────────────────────────
        if raw_tool is None:
            text = _cant_answer_text(question)
            _log_query(db, question, None, {}, False,
                       time.monotonic() - t_start)
            return Answer(
                text=text, rows=[], tool_used=None, params={},
                summary="", confidence="none", answerable=False,
            )

        # ── Validate tool name — HARD ERROR, not a fallback (rule 45) ─────────
        # The model must name a real tool.  If it hallucinated a name, we do
        # not silently guess; we report the error.
        if raw_tool not in TOOLS:
            available = ", ".join(sorted(TOOLS))
            text = (
                f"The router returned an unrecognised tool name: {raw_tool!r}. "
                f"Available tools: {available}. "
                f"Please rephrase your question."
            )
            _log_query(db, question, raw_tool, raw_params, False,
                       time.monotonic() - t_start, f"unknown tool: {raw_tool}")
            return Answer(
                text=text, rows=[], tool_used=None, params={},
                summary="", confidence=confidence, answerable=False,
            )

        # ── Call 2 (part A): EXECUTE ──────────────────────────────────────────
        # call() validates and clamps all params — model-supplied values are
        # never used raw (rule 41).
        try:
            result: ToolResult = call(raw_tool, raw_params, db, aliases)
        except (ToolNotFound, RuntimeError) as exc:
            error_msg = str(exc)
            text = f"Tool execution failed: {exc}"
            _log_query(db, question, raw_tool, raw_params, False,
                       time.monotonic() - t_start, error_msg)
            return Answer(
                text=text, rows=[], tool_used=raw_tool, params=raw_params,
                summary="", confidence=confidence, answerable=False,
            )

        tool_used   = result.tool
        params_used = result.params_used
        rows        = result.rows
        summary     = result.summary
        answerable  = True

        # ── Call 2 (part B): PHRASE ───────────────────────────────────────────
        # The model may only use numbers from the rows it receives (rule 43).
        # We serialise at most 50 rows to keep the prompt bounded.
        rows_for_prompt = rows[:50]
        rows_json = json.dumps(rows_for_prompt, indent=2, default=str)

        phrase_prompt = _PHRASE_PROMPT_TEMPLATE.format(
            question=question.strip(),
            summary=summary,
            rows_json=rows_json,
        )

        try:
            phrase_resp = complete_json(
                phrase_prompt,
                _PHRASE_SCHEMA,
                config=config,
                max_retries=1,
            )
            text = phrase_resp.get("text", "").strip()
            if not text:
                raise LLMError("Phrasing call returned empty text.")
        except LLMError as exc:
            # Phrasing failed — return rows with a plain fallback text so the
            # data is never withheld from the user (rule 44).
            error_msg = str(exc)
            text = (
                f"Data retrieved but phrasing failed: {exc}\n\n"
                f"{summary}"
            )

    except Exception as exc:  # noqa: BLE001 — catch-all so the dashboard never crashes
        error_msg = str(exc)
        text = f"Unexpected error: {exc}"
        answerable = False

    _log_query(
        db, question, tool_used, params_used, answerable,
        time.monotonic() - t_start, error_msg,
    )

    return Answer(
        text=text,
        rows=rows,
        tool_used=tool_used,
        params=params_used,
        summary=summary,
        confidence=confidence,
        answerable=answerable,
    )
