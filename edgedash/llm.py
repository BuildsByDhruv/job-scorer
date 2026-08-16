"""Single door to any language model (steering rule 15).

Public API
----------
complete_json(prompt, schema, *, config, max_retries=1) -> dict

    Sends `prompt` to the configured LLM, demands a JSON reply, strips any
    markdown wrapping, validates the result against `schema`, and returns the
    parsed dict.

    Retry policy  : if parsing or schema validation fails, retry ONCE with
                    the exact error appended to the prompt.
    On 429 / quota: exponential back-off, 3 attempts, then raise LLMError.
    Rate limiting : min 1 s between calls; rolling cap of 15 calls / minute.
                    Both limits are enforced by sleeping, never by erroring.

Providers
---------
"gemini"  google-genai SDK  key = GEMINI_API_KEY env var
"ollama"  local HTTP API    no key required (base_url from OLLAMA_BASE_URL,
                            default http://localhost:11434)

Adding a provider: add a _Provider subclass and register it in _PROVIDERS.
complete_json itself never changes.

CLI check
---------
python -m edgedash.llm --check
"""

from __future__ import annotations

import collections
import json
import os
import re
import time
from abc import ABC, abstractmethod
from typing import Any

from edgedash.config import Config

# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------


class LLMError(RuntimeError):
    """Raised when the LLM cannot produce a valid response after all retries."""


class LLMQuotaExhausted(LLMError):
    """Raised when the daily (or project-level) quota is exhausted.

    Callers must stop the batch immediately — retrying will not help until
    the quota resets (midnight Pacific for Gemini free tier).
    """


# ---------------------------------------------------------------------------
# Rate limiter (shared across all providers)
# ---------------------------------------------------------------------------

_CALL_TIMES: collections.deque[float] = collections.deque()
_MIN_INTERVAL = 1.0      # seconds between consecutive calls
_MAX_PER_MINUTE = 15


def _rate_limit() -> None:
    """Sleep until both rate constraints are satisfied, then record the call."""
    now = time.monotonic()

    # Enforce min interval between consecutive calls.
    if _CALL_TIMES:
        gap = _MIN_INTERVAL - (now - _CALL_TIMES[-1])
        if gap > 0:
            time.sleep(gap)
            now = time.monotonic()

    # Enforce rolling 15-calls-per-minute window.
    cutoff = now - 60.0
    while _CALL_TIMES and _CALL_TIMES[0] < cutoff:
        _CALL_TIMES.popleft()

    if len(_CALL_TIMES) >= _MAX_PER_MINUTE:
        oldest = _CALL_TIMES[0]
        wait = (oldest + 60.0) - now
        if wait > 0:
            time.sleep(wait)
        now = time.monotonic()
        # Prune again after sleeping.
        cutoff = now - 60.0
        while _CALL_TIMES and _CALL_TIMES[0] < cutoff:
            _CALL_TIMES.popleft()

    _CALL_TIMES.append(time.monotonic())


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------

# Matches ```json ... ``` or ``` ... ``` fences, with optional language tag.
_FENCE_RE = re.compile(
    r"```(?:json)?\s*([\s\S]*?)```",
    re.IGNORECASE,
)


def _extract_json(text: str) -> dict[str, Any]:
    """Strip markdown fences and leading/trailing prose, then parse JSON.

    Tries, in order:
    1. Entire text (model was well-behaved).
    2. First ```...``` fence content.
    3. First '{' to last '}' substring (prose wrapper).

    Raises ValueError with a diagnostic message on all failures.
    """
    candidates: list[str] = [text.strip()]

    fence_match = _FENCE_RE.search(text)
    if fence_match:
        candidates.append(fence_match.group(1).strip())

    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        candidates.append(text[brace_start : brace_end + 1])

    last_err: Exception | None = None
    for candidate in candidates:
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError as exc:
            last_err = exc

    raise ValueError(
        f"No valid JSON object found in model response. "
        f"Last parse error: {last_err}. "
        f"Raw text (first 300 chars): {text[:300]!r}"
    )


def _validate(data: dict[str, Any], schema: dict[str, Any]) -> None:
    """Minimal structural validation against a flat schema dict.

    Schema format — same shape as the expected output dict:
        { "field_name": expected_type_or_tuple_of_types, ... }

    Example:
        {"keywords": list, "seniority": str, "remote": bool}

    Raises ValueError naming the first failing field.
    """
    for field, expected in schema.items():
        if field not in data:
            raise ValueError(f"Missing required field: {field!r}")
        if not isinstance(data[field], expected):
            actual = type(data[field]).__name__
            exp_name = (
                " | ".join(t.__name__ for t in expected)
                if isinstance(expected, tuple)
                else expected.__name__
            )
            raise ValueError(
                f"Field {field!r}: expected {exp_name}, got {actual}"
            )


# ---------------------------------------------------------------------------
# Provider ABC and concrete implementations
# ---------------------------------------------------------------------------


class _Provider(ABC):
    """Internal contract for LLM providers."""

    @abstractmethod
    def call(self, prompt: str, model: str) -> str:
        """Send `prompt` to `model`, return raw text response.

        Must raise LLMError on quota / auth failures so the caller can
        apply back-off. Other exceptions propagate as-is.
        """


class _GeminiProvider(_Provider):
    """google-genai SDK provider."""

    def __init__(self, api_key: str) -> None:
        try:
            from google import genai  # type: ignore[import]
        except ImportError as exc:
            raise LLMError(
                "google-genai is not installed. Run: pip install google-genai"
            ) from exc
        self._client = genai.Client(api_key=api_key)
        # The SDK emits a noisy AFC advisory on every generate_content call.
        # It is cosmetic — we are not using automatic function calling — so
        # silence it at the logger level rather than polluting every run.
        import logging
        logging.getLogger("google_genai.models").setLevel(logging.ERROR)

    @staticmethod
    def _normalise_model(model: str) -> str:
        """Ensure the model name has the required 'models/' prefix."""
        return model if model.startswith("models/") else f"models/{model}"

    def call(self, prompt: str, model: str) -> str:
        import re as _re
        from google.genai import errors as _gerr  # type: ignore[import]

        full_model = self._normalise_model(model)
        _rate_limit()
        backoff = 5.0
        for attempt in range(3):
            try:
                response = self._client.models.generate_content(
                    model=full_model,
                    contents=prompt,
                )
                return response.text
            except _gerr.ClientError as exc:
                code = getattr(exc, "code", None)
                text = str(exc)

                # 429 / RESOURCE_EXHAUSTED — check whether it's the daily
                # quota (unrecoverable until midnight) or a per-minute rate
                # limit (recoverable with back-off).
                if code == 429 or "RESOURCE_EXHAUSTED" in text:
                    # Daily quota keywords present in the violation details.
                    daily_keywords = (
                        "PerDay",
                        "per_day",
                        "daily",
                        "FreeTier",
                        "free_tier",
                    )
                    is_daily = any(kw.lower() in text.lower() for kw in daily_keywords)

                    if is_daily:
                        raise LLMQuotaExhausted(
                            f"Gemini daily quota exhausted: {exc}. "
                            f"Quota resets at midnight Pacific — stopping batch."
                        ) from exc

                    # Per-minute rate limit — back off and retry.
                    if attempt == 2:
                        raise LLMError(f"Gemini quota exhausted: {exc}") from exc
                    # Honour the retry delay the API returns when present.
                    m = _re.search(r"retry in (\d+(?:\.\d+)?)s", text, _re.I)
                    sleep_for = float(m.group(1)) + 1.0 if m else backoff
                    time.sleep(sleep_for)
                    backoff *= 2
                    continue

                # 401 / 403 UNAUTHENTICATED — no point retrying.
                if code in (401, 403) or "UNAUTHENTICATED" in text:
                    raise LLMError(
                        f"Gemini authentication failed — "
                        f"check GEMINI_API_KEY in .env: {exc}"
                    ) from exc

                # Any other client error (404 bad model name, etc.) — fail fast.
                raise LLMError(f"Gemini API error: {exc}") from exc

            except _gerr.ServerError as exc:
                # 503 / 500 — transient server overload; back off and retry.
                code = getattr(exc, "code", None)
                if attempt == 2:
                    raise LLMError(
                        f"Gemini server error after 3 attempts (code {code}): {exc}"
                    ) from exc
                time.sleep(backoff)
                backoff *= 2
                continue


class _OllamaProvider(_Provider):
    """Ollama local HTTP provider (no API key required)."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    def call(self, prompt: str, model: str) -> str:
        import requests  # noqa: PLC0415

        _rate_limit()
        url = f"{self._base_url}/api/generate"
        backoff = 5.0
        for attempt in range(3):
            try:
                resp = requests.post(
                    url,
                    json={"model": model, "prompt": prompt, "stream": False},
                    timeout=120,
                )
                if resp.status_code == 429:
                    if attempt == 2:
                        raise LLMError("Ollama rate-limited (429) after 3 attempts.")
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                resp.raise_for_status()
                return resp.json()["response"]
            except requests.RequestException as exc:
                if attempt == 2:
                    raise LLMError(f"Ollama request failed: {exc}") from exc
                time.sleep(backoff)
                backoff *= 2
        raise LLMError("Ollama: unreachable after 3 attempts.")  # pragma: no cover


# ---------------------------------------------------------------------------
# Provider registry — add new providers here only, complete_json never changes
# ---------------------------------------------------------------------------

def _build_provider(config: Config) -> _Provider:
    """Instantiate the correct provider from config. Raises LLMError on bad config."""
    provider = config.llm_provider.lower()

    if provider == "gemini":
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise LLMError(
                "GEMINI_API_KEY is not set. "
                "Add it to your .env file (see .env.example) and re-run."
            )
        return _GeminiProvider(api_key=key)

    if provider == "ollama":
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        return _OllamaProvider(base_url=base_url)

    raise LLMError(
        f"Unknown llm_provider {config.llm_provider!r} in config.yaml. "
        f"Supported values: 'gemini', 'ollama'."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def complete_json(
    prompt: str,
    schema: dict[str, Any],
    *,
    config: Config,
    max_retries: int = 1,
) -> dict[str, Any]:
    """Send `prompt`, validate the JSON reply against `schema`, return the dict.

    On parse or validation failure: retry up to `max_retries` times, appending
    the exact error and a strict JSON-only instruction to the prompt.

    Raises LLMError if all attempts fail — callers handle per rule 17.
    """
    provider = _build_provider(config)
    model = config.llm_model

    _JSON_INSTRUCTION = (
        "\n\nYou must reply with a single JSON object and nothing else. "
        "No markdown, no code fences, no explanation."
    )

    current_prompt = prompt + _JSON_INSTRUCTION
    last_error: str = ""

    for attempt in range(max_retries + 1):
        if attempt > 0:
            current_prompt = (
                prompt
                + _JSON_INSTRUCTION
                + f"\n\nYour previous reply failed validation with this error:\n"
                f"{last_error}\n"
                f"Fix it. Reply with a JSON object only — no prose, no fences."
            )

        raw = provider.call(current_prompt, model)

        try:
            data = _extract_json(raw)
        except ValueError as exc:
            last_error = str(exc)
            continue

        try:
            _validate(data, schema)
            return data
        except ValueError as exc:
            last_error = str(exc)
            continue

    raise LLMError(
        f"LLM failed to return a valid response after {max_retries + 1} attempt(s). "
        f"Last error: {last_error}"
    )


# ---------------------------------------------------------------------------
# CLI check
# ---------------------------------------------------------------------------


def _cli_check() -> None:
    """Send one trivial prompt and report the outcome."""
    from dotenv import load_dotenv
    load_dotenv()
    from edgedash.config import load_config

    config = load_config()
    print(f"  provider : {config.llm_provider}")
    print(f"  model    : {config.llm_model}")
    print("  sending  : trivial JSON extraction prompt …")

    test_schema: dict[str, Any] = {"ok": bool, "value": int}
    test_prompt = (
        'Return a JSON object with two fields: "ok" (boolean true) '
        'and "value" (integer 42).'
    )

    try:
        result = complete_json(test_prompt, test_schema, config=config)
        print(f"  result   : {result}")
        print("  status   : ✓ OK")
    except LLMError as exc:
        print(f"  status   : ✗ FAILED — {exc}")


if __name__ == "__main__":
    import sys
    if "--check" in sys.argv:
        _cli_check()
    else:
        print("Usage: python -m edgedash.llm --check")