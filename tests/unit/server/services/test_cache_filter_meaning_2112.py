"""Bug #2112: the export cache filter means what it says.

`voyage_cache_mode` / `cohere_cache_mode` carry the cache mode ("on" or
"shadow") only when the embedding cache was CONSULTED for the search; they
are NULL when no cache applies (fts, regex, xray, scip, git_history,
ci_logs), when the cache is absent, disabled or off, and when the caller
bypassed it (the direct path reports `cache_mode=None` for a bypass).

- hits_only: any provider recorded a hit.
- misses_only: some provider consulted its cache and none recorded a hit.
- Rows that consulted no cache are in neither, and stay in "all".

Driven against the REAL stores (SQLite and PostgreSQL) through
query_for_export, the read the export uses.
"""

from pathlib import Path
from typing import Iterator, List

import pytest

from tests.unit.server.services.search_event_real_stores import (
    PG_AVAILABLE,
    PG_SKIP_REASON,
    RealStore,
    make_record,
    open_store,
)

_KINDS = [
    "sqlite",
    pytest.param(
        "postgres", marks=pytest.mark.skipif(not PG_AVAILABLE, reason=PG_SKIP_REASON)
    ),
]


@pytest.fixture(params=_KINDS)
def store(request, tmp_path: Path) -> Iterator[RealStore]:
    with open_store(request.param, tmp_path) as real_store:
        real_store.bulk_insert(
            [
                make_record(query_text="fts", search_type="fts", timestamp=1.0),
                make_record(query_text="regex", search_type="regex", timestamp=2.0),
                make_record(
                    query_text="voyage hit",
                    voyage_cache_hit=True,
                    voyage_cache_mode="on",
                    timestamp=3.0,
                ),
                make_record(
                    query_text="voyage miss",
                    voyage_cache_hit=False,
                    voyage_cache_mode="on",
                    timestamp=4.0,
                ),
                make_record(
                    query_text="cohere shadow miss",
                    cohere_cache_hit=False,
                    cohere_cache_mode="shadow",
                    timestamp=5.0,
                ),
                make_record(
                    query_text="bypassed",
                    voyage_cache_hit=False,
                    voyage_cache_mode=None,
                    timestamp=6.0,
                ),
                make_record(
                    query_text="cache off", search_type="semantic", timestamp=7.0
                ),
                make_record(
                    query_text="cohere hit voyage miss",
                    voyage_cache_hit=False,
                    voyage_cache_mode="on",
                    cohere_cache_hit=True,
                    cohere_cache_mode="on",
                    timestamp=8.0,
                ),
                # A hit flag with no consulted mode (written before #2112):
                # not a hit of a consulted cache.
                make_record(
                    query_text="hit without mode",
                    voyage_cache_hit=True,
                    voyage_cache_mode=None,
                    timestamp=9.0,
                ),
            ]
        )
        yield real_store


def _texts(store: RealStore, cache_hit_filter: str) -> List[str]:
    rows = store.backend.query_for_export(
        {"cache_hit_filter": cache_hit_filter}, limit=100
    )
    return [row["query_text"] for row in rows]


def test_misses_only_returns_only_searches_that_consulted_a_cache_and_missed(store):
    assert _texts(store, "misses_only") == ["voyage miss", "cohere shadow miss"]


def test_hits_only_returns_only_searches_with_a_hit(store):
    assert _texts(store, "hits_only") == ["voyage hit", "cohere hit voyage miss"]


def test_all_returns_every_row(store):
    assert len(_texts(store, "all")) == 9


def test_hits_only_read_stays_an_index_seek_after_the_keyset(store):
    from code_indexer.server.services.search_event_log_writer import _export_select

    ph, true_lit, false_lit = (
        ("?", "1", "0") if store.kind == "sqlite" else ("%s", "TRUE", "FALSE")
    )
    sql, params = _export_select(
        {"cache_hit_filter": "hits_only"}, (4.0, 4), 10, ph, true_lit, false_lit
    )
    plan = " ".join(store.explain(sql, params))
    assert "idx_sel_timestamp" in plan, plan


def test_misses_only_read_stays_an_index_seek_after_the_keyset(store):
    """The cache predicate never becomes a top-level OR next to the keyset
    condition: the batched read keeps a timestamp range seek."""
    from code_indexer.server.services.search_event_log_writer import _export_select

    ph, true_lit, false_lit = (
        ("?", "1", "0") if store.kind == "sqlite" else ("%s", "TRUE", "FALSE")
    )
    sql, params = _export_select(
        {"cache_hit_filter": "misses_only"}, (4.0, 4), 10, ph, true_lit, false_lit
    )
    plan = " ".join(store.explain(sql, params))
    assert "idx_sel_timestamp" in plan, plan
