"""Bug #2112 (owner ruling): only a CONSULTED cache is a hit or a miss.

A search event row carries a provider's cache mode AND cache hit only when
that provider's query-embedding cache was consulted for the search: not when
the caller bypassed it (no_embedding_cache_shortcut) and not when the query
had no cache key (over the normalized-length cap). Otherwise both columns are
NULL -- exactly as the direct (non-coalesced) path records "no cache" -- so
the row appears under neither "hits only" nor "misses only". What the
coalescer reports to the embed-event table is unchanged: the embedding
metadata keeps the key_found / cache_mode the coalescer resolved.

Drives the REAL EmbeddingCoalescer (Path A of coalesced_query_embedding) over
a real QueryEmbeddingCache, the REAL FilesystemVectorStore recording point,
and a REAL store (SQLite and PostgreSQL).
"""

from pathlib import Path
from typing import Any, Iterator, List, Optional

import pytest

from code_indexer.server.services import governed_call
from code_indexer.server.services import search_event_recording as ser
from code_indexer.server.services.coalescer_registry import clear_coalescer_registry
from code_indexer.server.services.provider_concurrency_governor import (
    ProviderConcurrencyGovernor,
)
from tests.unit.server.services.search_event_real_stores import (
    PG_AVAILABLE,
    PG_SKIP_REASON,
)
from tests.unit.server.services.search_event_recording_harness import (
    Recorder,
    recorder,
)
from tests.unit.server.services.test_coalesced_query_embedding import (
    _FakeVoyageProvider3c,
    _make_real_cache_3c,
    _wire_real_coalescer,
)

_KINDS = [
    "sqlite",
    pytest.param(
        "postgres", marks=pytest.mark.skipif(not PG_AVAILABLE, reason=PG_SKIP_REASON)
    ),
]

_ORIGIN = ser.SearchOrigin(tool="search_code", door="mcp")

# Over the query-embedding cache's normalized-length cap: no cache key.
_OVER_CAP_QUERY = "over cap query " * 40


@pytest.fixture(autouse=True)
def _reset_singletons() -> Iterator[None]:
    from code_indexer.server.services.config_service import reset_config_service
    from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

    ProviderConcurrencyGovernor.reset_instance()
    ProviderHealthMonitor.reset_instance()
    governed_call.clear_query_embedding_cache()
    clear_coalescer_registry()
    reset_config_service()
    yield
    ProviderConcurrencyGovernor.reset_instance()
    ProviderHealthMonitor.reset_instance()
    governed_call.clear_query_embedding_cache()
    clear_coalescer_registry()
    reset_config_service()


@pytest.fixture(params=_KINDS)
def rec(request, tmp_path: Path) -> Iterator[Recorder]:
    with recorder(request.param, tmp_path) as real:
        yield real


def _coalesced_search(
    monkeypatch: Any,
    rec: Recorder,
    text: str,
    bypass: bool,
    mode: str = "on",
    pre_seed: Optional[str] = None,
) -> Any:
    """One coalesced query embedding inside a search event scope, written to
    the context by the FilesystemVectorStore recording point, then recorded.
    Returns the embedding metadata the coalescer produced."""
    from code_indexer.storage.filesystem_vector_store import (
        _write_embed_meta_to_event_ctx,
    )

    cache, _qualifier, _backend = _make_real_cache_3c(mode=mode, pre_seed_text=pre_seed)
    monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)
    provider = _FakeVoyageProvider3c()
    _wire_real_coalescer(monkeypatch, provider)

    with ser.search_event_scope(
        username="alice",
        repo_alias="example-repo-global",
        search_type="semantic",
        query_text=text,
    ) as context:
        _vec, meta = governed_call.coalesced_query_embedding(
            provider, text, no_embedding_cache_shortcut=bypass
        )
        _write_embed_meta_to_event_ctx(meta, provider.get_provider_name())
    ser.record_query_results(
        rec.writer,
        origin=_ORIGIN,
        context=context,
        results=[],
        total_latency_ms=1,
        correlation_id=None,
    )
    return meta


def _filtered(rec: Recorder, cache_hit_filter: str) -> List[str]:
    rec.rows()  # flush the writer
    rows = rec.store.backend.query_for_export(
        {"cache_hit_filter": cache_hit_filter}, limit=10
    )
    return [r["query_text"] for r in rows]


def test_coalesced_bypass_is_not_a_cache_miss(monkeypatch, rec):
    meta = _coalesced_search(monkeypatch, rec, "bypassed query 2112", bypass=True)

    # The embed-event reporting is unchanged.
    assert meta.cache_mode == "on"
    [row] = rec.rows()
    assert (row["voyage_cache_mode"], row["voyage_cache_hit"]) == (None, None)
    assert _filtered(rec, "misses_only") == []


def test_shadow_bypass_of_a_cached_key_is_neither_hit_nor_miss(monkeypatch, rec):
    """Shadow mode (the shipped default) with the key already cached: the
    coalescer reports key_found True even for a bypassed entry. The search
    did not consult the cache, so the row is neither a hit nor a miss."""
    text = "shadow cached query 2112"
    meta = _coalesced_search(
        monkeypatch, rec, text, bypass=True, mode="shadow", pre_seed=text
    )

    assert meta.key_found is True  # embed-event reporting unchanged
    [row] = rec.rows()
    assert (row["voyage_cache_mode"], row["voyage_cache_hit"]) == (None, None)
    assert _filtered(rec, "hits_only") == []
    assert _filtered(rec, "misses_only") == []


def test_over_cap_query_is_neither_hit_nor_miss(monkeypatch, rec):
    """A query over the normalized-length cap has no cache key: no cache was
    consulted for it, whatever mode the coalescer reports."""
    _coalesced_search(monkeypatch, rec, _OVER_CAP_QUERY, bypass=False)

    [row] = rec.rows()
    assert (row["voyage_cache_mode"], row["voyage_cache_hit"]) == (None, None)
    assert _filtered(rec, "hits_only") == []
    assert _filtered(rec, "misses_only") == []


def test_coalesced_consulted_miss_is_a_cache_miss(monkeypatch, rec):
    _coalesced_search(monkeypatch, rec, "consulted query 2112", bypass=False)

    [row] = rec.rows()
    assert (row["voyage_cache_mode"], row["voyage_cache_hit"]) == ("on", False)
    assert _filtered(rec, "misses_only") == ["consulted query 2112"]


def test_coalesced_consulted_hit_is_a_cache_hit(monkeypatch, rec):
    text = "consulted cached query 2112"
    _coalesced_search(monkeypatch, rec, text, bypass=False, pre_seed=text)

    [row] = rec.rows()
    assert (row["voyage_cache_mode"], row["voyage_cache_hit"]) == ("on", True)
    assert _filtered(rec, "hits_only") == [text]
