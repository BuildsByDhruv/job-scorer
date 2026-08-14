"""Tests for edgedash/skills.py — canonical() only, no network, no DB."""

from __future__ import annotations

import pytest

from edgedash.skills import canonical

# ---------------------------------------------------------------------------
# Shared alias map used across tests
# ---------------------------------------------------------------------------

ALIASES: dict[str, str] = {
    "k8s": "kubernetes",
    "postgresql": "postgres",
    "psql": "postgres",
    "golang": "go",
    "nodejs": "node",
    "node.js": "node",
    "ml": "machine learning",
    "ci cd": "ci/cd",
    "cicd": "ci/cd",
}


# ---------------------------------------------------------------------------
# Case normalisation
# ---------------------------------------------------------------------------

class TestCase:

    def test_uppercase_lowered(self) -> None:
        assert canonical("Python", ALIASES) == "python"

    def test_mixed_case_lowered(self) -> None:
        assert canonical("PostgreSQL", ALIASES) == "postgres"

    def test_all_caps(self) -> None:
        assert canonical("SQL", ALIASES) == "sql"


# ---------------------------------------------------------------------------
# Whitespace handling
# ---------------------------------------------------------------------------

class TestWhitespace:

    def test_leading_trailing_stripped(self) -> None:
        assert canonical("  python  ", ALIASES) == "python"

    def test_internal_run_collapsed(self) -> None:
        assert canonical("machine   learning", ALIASES) == "machine learning"

    def test_tab_collapsed(self) -> None:
        assert canonical("machine\tlearning", ALIASES) == "machine learning"

    def test_newline_collapsed(self) -> None:
        assert canonical("machine\nlearning", ALIASES) == "machine learning"


# ---------------------------------------------------------------------------
# Parenthetical qualifier removal
# ---------------------------------------------------------------------------

class TestParentheticals:

    def test_simple_qualifier_dropped(self) -> None:
        assert canonical("kubernetes (eks)", ALIASES) == "kubernetes"

    def test_qualifier_with_spaces(self) -> None:
        assert canonical("spark (apache spark)", ALIASES) == "spark"

    def test_no_parens_unchanged(self) -> None:
        assert canonical("docker", ALIASES) == "docker"

    def test_parens_only_content_stripped(self) -> None:
        # "aws (amazon web services)" -> "aws" after parens drop
        assert canonical("aws (amazon web services)", ALIASES) == "aws"


# ---------------------------------------------------------------------------
# Alias map lookup
# ---------------------------------------------------------------------------

class TestAliasLookup:

    def test_known_alias_resolved(self) -> None:
        assert canonical("K8s", ALIASES) == "kubernetes"

    def test_alias_after_normalisation(self) -> None:
        # "Node.JS" -> normalised "node.js" -> alias "node"
        assert canonical("Node.JS", ALIASES) == "node"

    def test_psql_alias(self) -> None:
        assert canonical("PSQL", ALIASES) == "postgres"

    def test_golang_alias(self) -> None:
        assert canonical("Golang", ALIASES) == "go"

    def test_ci_cd_space_variant(self) -> None:
        # "CI CD" -> "ci cd" -> alias "ci/cd"
        assert canonical("CI CD", ALIASES) == "ci/cd"

    def test_cicd_no_separator(self) -> None:
        assert canonical("CICD", ALIASES) == "ci/cd"

    def test_ml_shorthand(self) -> None:
        assert canonical("ML", ALIASES) == "machine learning"


# ---------------------------------------------------------------------------
# No alias — pass-through
# ---------------------------------------------------------------------------

class TestNoAlias:

    def test_unknown_term_returned_as_is(self) -> None:
        assert canonical("dbt", ALIASES) == "dbt"

    def test_already_canonical(self) -> None:
        assert canonical("kubernetes", ALIASES) == "kubernetes"

    def test_empty_alias_map(self) -> None:
        assert canonical("k8s", {}) == "k8s"


# ---------------------------------------------------------------------------
# Empty and edge-case inputs
# ---------------------------------------------------------------------------

class TestEdgeCases:

    def test_empty_string(self) -> None:
        assert canonical("", ALIASES) == ""

    def test_whitespace_only(self) -> None:
        # All whitespace normalises to empty after strip
        assert canonical("   ", ALIASES) == ""

    def test_only_punctuation(self) -> None:
        # Surrounding punctuation stripped, nothing left
        assert canonical("...", ALIASES) == ""

    def test_surrounding_punctuation_stripped(self) -> None:
        assert canonical('"python"', ALIASES) == "python"

    def test_parenthetical_leaves_clean_name(self) -> None:
        # Trailing space after parens removal is cleaned up
        result = canonical("spark (pyspark)", ALIASES)
        assert result == "spark"
        assert not result.endswith(" ")
