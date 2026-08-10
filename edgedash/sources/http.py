"""Shared HTTP helper — the ONLY place in the project that makes network calls.

Rules enforced here (steering rules 11 & 14):
- 10-second timeout on every request.
- 2 retry attempts with exponential back-off (1s, 2s).
- Real User-Agent header on every request.
- 1-second inter-request delay enforced via _rate_limit().
"""

from __future__ import annotations

import time
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USER_AGENT = (
    "EdgeDash/0.1 (career-intelligence-bot; "
    "github.com/BuildsByDhruv/job-scorer)"
)
_TIMEOUT_SECONDS = 10
_MAX_RETRIES = 2
_BACKOFF_BASE = 1.0          # seconds; doubles each retry
_MIN_INTERVAL = 1.0          # seconds between requests to the same host

# Per-host last-request timestamps used for rate limiting.
_last_request: dict[str, float] = {}


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------


class SourceError(RuntimeError):
    """Raised when a network request fails after all retries."""


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


def _rate_limit(host: str) -> None:
    """Block until at least _MIN_INTERVAL has passed since the last call."""
    now = time.monotonic()
    last = _last_request.get(host, 0.0)
    wait = _MIN_INTERVAL - (now - last)
    if wait > 0:
        time.sleep(wait)
    _last_request[host] = time.monotonic()


# ---------------------------------------------------------------------------
# Public helper
# ---------------------------------------------------------------------------


def get_json(
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    """Perform a GET request and return parsed JSON.

    Applies timeout, User-Agent, rate limiting, and retry logic.
    Raises SourceError on final failure.
    """
    from urllib.parse import urlparse

    host = urlparse(url).netloc
    _rate_limit(host)

    merged_headers = {"User-Agent": _USER_AGENT}
    if headers:
        merged_headers.update(headers)

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = requests.get(
                url,
                params=params,
                headers=merged_headers,
                timeout=_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                sleep_for = _BACKOFF_BASE * (2 ** attempt)
                time.sleep(sleep_for)

    raise SourceError(
        f"GET {url} failed after {_MAX_RETRIES + 1} attempts: {last_exc}"
    ) from last_exc
