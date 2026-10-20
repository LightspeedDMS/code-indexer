"""Full-text words judged on the text a restricted caller is shown (pure
functions over raw and projected lines; neutral sample text)."""

from __future__ import annotations

from typing import List, Optional

import pytest

from code_indexer.server.services.cidx_meta_access.depmap_projection import (
    RESTRICTED_PLACEHOLDER,
)
from code_indexer.server.services.cidx_meta_access.fulltext_terms import (
    FullTextTerms,
    VisibleMatch,
    effective_edit_distance,
    first_visible_match,
    parse_fulltext_terms,
)


def _terms(query: str, edit_distance: int = 0) -> FullTextTerms:
    terms = parse_fulltext_terms(query, edit_distance)
    assert terms is not None
    return terms


def _match(
    raw: List[str], shown: List[str], query: str, d: int = 0
) -> Optional[VisibleMatch]:
    return first_visible_match(raw, shown, _terms(query, d))


@pytest.mark.parametrize(
    "query",
    [
        "ledger NOT sample",
        "ledger OR sample",
        "ledger AND sample",
        "ledger*",
        "*",
        "ledger^2",
        "ledger~1",
        "ledger ...",
        "x" * 41,
    ],
)
def test_forms_that_cannot_be_judged_on_shown_text_are_refused(query: str) -> None:
    assert parse_fulltext_terms(query, 0) is None


def test_fuzzy_is_judged_for_one_word_only() -> None:
    assert parse_fulltext_terms("ledger sample", 1) is None
    assert _terms("ledgr", 1) == FullTextTerms(("ledgr",), (("ledgr",),), "ledgr", 1)


def test_words_become_token_phrases() -> None:
    assert _terms('"Ledger" example-repo -flag').phrases == (
        ("ledger",),
        ("example", "repo"),
        ("flag",),
    )


def test_effective_edit_distance() -> None:
    assert effective_edit_distance(True, 0) == 1
    assert effective_edit_distance(True, 2) == 2
    assert effective_edit_distance(False, 0) == 0


def test_every_word_must_be_shown() -> None:
    raw = ["ledger entries", "settles with example-repo"]
    shown = ["ledger entries", ""]
    assert _match(raw, shown, "ledger example-repo") is None
    assert _match(raw, raw, "ledger example-repo") == VisibleMatch(1, "ledger", 2)


def test_count_is_shown_occurrences_only() -> None:
    raw = ["alpha alpha", "alpha beta", "alpha"]
    shown = ["alpha alpha", "", "alpha"]
    assert _match(raw, shown, "alpha") == VisibleMatch(1, "alpha", 3)
    assert _match(raw, raw, "alpha") == VisibleMatch(1, "alpha", 4)


def test_anchor_is_the_first_shown_occurrence_of_the_first_word() -> None:
    raw = ["Ledger hidden", "intro", "the LEDGER entries"]
    shown = ["", "intro", "the LEDGER entries"]
    assert _match(raw, shown, "ledger entries") == VisibleMatch(3, "LEDGER", 2)


def test_placeholder_text_never_matches() -> None:
    raw = ["owned by restricted-team"]
    shown = [f"owned by {RESTRICTED_PLACEHOLDER}"]
    assert _match(raw, shown, "restricted") is None
    assert _match(raw, shown, "owned") == VisibleMatch(1, "owned", 1)


def test_text_introduced_by_a_rewrite_never_matches() -> None:
    raw = ["repos_analyzed_count: 4"]
    shown = ["repos_analyzed_count: 1"]
    assert _match(raw, shown, "1") is None
    assert _match(raw, shown, "count") == VisibleMatch(1, "count", 1)


def test_token_cut_by_a_rewrite_never_matches() -> None:
    raw = ["see sampleledger"]
    shown = ["see ledger"]
    assert _match(raw, shown, "ledger") is None


def test_phrase_never_spans_removed_text_or_lines() -> None:
    raw = ["example, hidden-repo | ledger", "example", "repo"]
    shown = ["example | ledger", "example", "repo"]
    assert _match(raw, shown, "example-ledger") is None
    assert _match(raw, shown, "example-repo") is None


def test_fuzzy_counts_a_transposition_as_one_edit() -> None:
    raw = ["ledger entries"]
    assert _match(raw, raw, "legder", 1) == VisibleMatch(1, "ledger", 1)
    assert _match(raw, raw, "lgder", 1) is None
    assert _match(raw, raw, "lgder", 2) == VisibleMatch(1, "ledger", 1)
