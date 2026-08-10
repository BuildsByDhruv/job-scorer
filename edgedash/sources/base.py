"""Base contract for all job-board sources.

Every source returns a list of normalised dicts with EXACTLY these keys
(missing values are None, never empty string, never "N/A"):

    source       str   – stable source identifier, e.g. "arbeitnow"
    external_id  str   – source-assigned stable slug or id
    title        str
    company      str
    location     str
    url          str
    description  str | None
    posted_at    str | None  – ISO-8601 date string or None
    raw          dict  – the original API payload, unchanged
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from edgedash.config import Config

# ---------------------------------------------------------------------------
# Normalised row type (documentation only — not enforced at runtime)
# ---------------------------------------------------------------------------

NormalisedRow = dict[str, Any]

# ---------------------------------------------------------------------------
# Source protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Source(Protocol):
    """Contract every source class must satisfy."""

    name: str

    def fetch(self, config: Config) -> list[NormalisedRow]:
        """Fetch listings and return them normalised.

        A source MUST NOT write to storage directly.
        Missing field values are None, never "" or "N/A".
        """
        ...


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

SOURCES: dict[str, type[Source]] = {}


def register(cls: type[Source]) -> type[Source]:
    """Class decorator that adds a Source to the registry.

    Usage::

        @register
        class MySource:
            name = "mysource"
            ...
    """
    SOURCES[cls.name] = cls
    return cls
