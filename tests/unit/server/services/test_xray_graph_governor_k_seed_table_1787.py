"""Story #1787 AC12: seed table for the per-language memory multiplier K."""

from __future__ import annotations

from code_indexer.server.services.xray_graph_governor.k_seed_table import (
    CONSERVATIVE_DEFAULT_K,
    MEASURED_K_SEED_TABLE,
    k_for_language,
)


def test_k_for_language_returns_seeded_value_for_java():
    assert k_for_language("java") == 19.0


def test_k_for_language_is_case_insensitive():
    assert k_for_language("Java") == 19.0
    assert k_for_language("JAVA") == 19.0


def test_k_for_language_returns_conservative_default_for_unseen_language():
    assert k_for_language("cobol") == CONSERVATIVE_DEFAULT_K


def test_conservative_default_is_the_max_of_the_seed_table():
    assert CONSERVATIVE_DEFAULT_K == max(MEASURED_K_SEED_TABLE.values())


def test_k_for_language_degrades_to_conservative_default_for_falsy_or_non_string_input():
    assert k_for_language("") == CONSERVATIVE_DEFAULT_K
    assert k_for_language(None) == CONSERVATIVE_DEFAULT_K  # type: ignore[arg-type]
