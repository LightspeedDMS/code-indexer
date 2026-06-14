"""Story #1105 — cache-wrap regression matrix for coalesced_query_embedding.

Verifies that the cache integration layer in coalesced_query_embedding routes
correctly through the cache (on / shadow / off modes) while preserving the
existing Story #1079 / #1083 coalescer behaviour on the live path.

The QueryEmbeddingCache is the OUTERMOST layer — it is consulted whenever
installed and the provider is enabled (mode != off), INDEPENDENT of whether
the coalescer registry is present, coalesce_enabled kill-switch, or lane
availability.  On a cache miss the live path is _compute_live() (which itself
decides coalescer-vs-direct).

Control-flow decision tree
--------------------------
1. Cache is None (CLI/solo)      -> _compute_live; cache never touched.
2. Cache present + coalesce_enabled=False -> cache IS consulted (outermost).
   HIT -> cached vec returned; live skipped. MISS -> _compute_live (direct).
3. Cache present + lane absent   -> cache IS consulted (outermost).
   HIT -> cached vec returned; live skipped. MISS -> _compute_live (direct).
4. Cache is None                 -> _compute_live; cache never touched.
5. Cache not enabled for provider-> _compute_live; cache never touched.
6. Mode "off"                    -> _compute_live; cache never touched.
7. Mode "on"  + HIT              -> return cached vec; _compute_live SKIPPED.
8. Mode "on"  + MISS             -> _compute_live; record_miss; return live.
9. Mode "shadow" + HIT           -> _compute_live; touch_last_used; return LIVE.
10. Mode "shadow" + MISS         -> _compute_live; record_miss; return live.
"""

from __future__ import annotations

import struct
from typing import List, Optional
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services import governed_call
from code_indexer.server.services.coalescer_registry import (
    CoalescerRegistry,
    clear_coalescer_registry,
    set_coalescer_registry,
)
from code_indexer.server.services.query_embedding_cache import (
    QueryEmbeddingCache,
    build_key,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LIVE_VEC: List[float] = [1.0, 2.0, 3.0]
CACHED_VEC: List[float] = [9.0, 8.0, 7.0]
PROVIDER_NAME = "voyage-ai"
MODEL_NAME = "voyage-code-3"
DIMENSION = 3
TEST_TEXT = "hello world"


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _make_cached_bytes(vec: List[float]) -> bytes:
    """Encode a float list as float32 LE bytes (matches record_miss_or_shadow)."""
    return struct.pack(f"<{len(vec)}f", *vec)


class _FakeVoyageProvider:
    """Duck-typed voyage-ai provider (not a CohereEmbeddingProvider)."""

    def get_provider_name(self) -> str:
        return PROVIDER_NAME

    def get_current_model(self) -> str:
        return MODEL_NAME

    def get_model_info(self) -> dict:
        return {"dimensions": DIMENSION}


class _FakeConfig:
    def __init__(self, coalesce_enabled: bool = True) -> None:
        self.coalesce_enabled = coalesce_enabled


class _FakeConfigService:
    def __init__(self, cfg: _FakeConfig) -> None:
        self._cfg = cfg

    def get_config(self) -> _FakeConfig:
        return self._cfg


class _FakeCoalescer:
    """Coalescer spy that returns LIVE_VEC (simulating the live path)."""

    def __init__(self) -> None:
        self.submitted: List[str] = []

    def submit(self, text: str, embedding_purpose: str = "query") -> List[float]:
        self.submitted.append(text)
        return LIVE_VEC


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_registry():
    clear_coalescer_registry()
    yield
    clear_coalescer_registry()


def _install_registry_with_coalescer(coalescer):
    """Install a registry that returns *coalescer* for any lane."""
    reg = CoalescerRegistry.__new__(CoalescerRegistry)
    reg._coalescers = {"voyage:embed": coalescer}
    set_coalescer_registry(reg)
    return reg


def _patch_config(monkeypatch, enabled: bool = True):
    monkeypatch.setattr(
        governed_call,
        "get_config_service",
        lambda: _FakeConfigService(_FakeConfig(coalesce_enabled=enabled)),
        raising=False,
    )


def _make_cache(
    *,
    enabled: bool = True,
    voyage_mode: str = "on",
    hit_bytes: Optional[bytes] = None,
) -> MagicMock:
    """Build a MagicMock QueryEmbeddingCache.

    *hit_bytes*: if set, lookup() returns these bytes (HIT).
                 if None, lookup() returns None (MISS).
    """
    cache = MagicMock(spec=QueryEmbeddingCache)
    cache.enabled_for.return_value = enabled
    cache.mode_for.return_value = voyage_mode
    cache.lookup.return_value = hit_bytes
    # governed_call.py routes key-building through build_key_for_provider (N2),
    # so we wire a real callable here instead of setting build_key + anchor_tokens_for.
    cache.build_key_for_provider = lambda text, provider_name: build_key(text, 2)
    cache.qualifier.return_value = MagicMock(
        provider=PROVIDER_NAME, model=MODEL_NAME, dimension=DIMENSION
    )
    return cache


# ---------------------------------------------------------------------------
# Path 1: Cache is None (CLI/solo) — cache never touched, _compute_live called
# ---------------------------------------------------------------------------


class TestNoRegistryBypassesCache:
    """When get_query_embedding_cache() returns None (no cache installed),
    coalesced_query_embedding delegates directly to _compute_live and never
    calls any cache method.  This is the CLI / daemon / solo path."""

    def test_cache_none_calls_compute_live_cache_never_touched(self, monkeypatch):
        # Ensure NO cache is installed
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: None)

        live_calls: list = []

        def _fake_live(provider, text, embedding_purpose=None, acquire_timeout=30.0):
            live_calls.append(text)
            return LIVE_VEC

        monkeypatch.setattr(governed_call, "_compute_live", _fake_live)

        result = governed_call.coalesced_query_embedding(
            _FakeVoyageProvider(), TEST_TEXT
        )
        assert result == LIVE_VEC
        assert live_calls == [TEST_TEXT]
        # No cache object was installed — nothing to assert on cache methods,
        # but the return must be the live vector.


# ---------------------------------------------------------------------------
# Path 2: Cache INDEPENDENT of coalesce_enabled kill-switch
# ---------------------------------------------------------------------------


class TestKillSwitchBypassesCache:
    """The cache is the OUTERMOST layer — it is consulted even when
    coalesce_enabled=False (kill switch off).  The kill switch only affects
    whether _compute_live routes through the coalescer or goes direct; it does
    NOT bypass the cache."""

    def test_coalesce_disabled_cache_hit_returns_cached_vec(self, monkeypatch):
        """HIT: cache returns cached bytes -> skip _compute_live entirely."""
        _patch_config(monkeypatch, enabled=False)

        cached_bytes = _make_cached_bytes(CACHED_VEC)
        cache = _make_cache(enabled=True, voyage_mode="on", hit_bytes=cached_bytes)
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        live_calls: list = []

        def _fake_live(provider, text, embedding_purpose=None, acquire_timeout=30.0):
            live_calls.append(text)
            return LIVE_VEC

        monkeypatch.setattr(governed_call, "_compute_live", _fake_live)

        result = governed_call.coalesced_query_embedding(
            _FakeVoyageProvider(), TEST_TEXT
        )
        # Cache HIT must return the cached vector, not the live one
        assert result == pytest.approx(CACHED_VEC, abs=1e-4)
        # _compute_live must NOT be called on a HIT
        assert live_calls == []
        # Cache lookup must have been called (cache IS outermost)
        cache.lookup.assert_called_once()
        cache.record_hit.assert_called_once()
        cache.record_miss_or_shadow.assert_not_called()

    def test_coalesce_disabled_cache_miss_calls_compute_live_and_records(
        self, monkeypatch
    ):
        """MISS: cache returns None -> _compute_live called (goes direct since
        coalesce is off) and record_miss_or_shadow invoked; live vec returned."""
        _patch_config(monkeypatch, enabled=False)

        cache = _make_cache(enabled=True, voyage_mode="on", hit_bytes=None)
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        live_calls: list = []

        def _fake_live(provider, text, embedding_purpose=None, acquire_timeout=30.0):
            live_calls.append(text)
            return LIVE_VEC

        monkeypatch.setattr(governed_call, "_compute_live", _fake_live)

        result = governed_call.coalesced_query_embedding(
            _FakeVoyageProvider(), TEST_TEXT
        )
        assert result == LIVE_VEC
        # _compute_live must be called on a MISS
        assert live_calls == [TEST_TEXT]
        # Cache lookup IS called (outermost), and miss recorded
        cache.lookup.assert_called_once()
        cache.record_miss_or_shadow.assert_called_once()
        cache.record_hit.assert_not_called()


# ---------------------------------------------------------------------------
# Path 3: Cache INDEPENDENT of lane presence
# ---------------------------------------------------------------------------


class TestLaneAbsentBypassesCache:
    """The cache is the OUTERMOST layer — it is consulted even when no
    coalescer lane exists for the provider.  Lane absence only affects whether
    _compute_live routes through the coalescer or goes direct."""

    def _install_registry_no_lanes(self) -> None:
        """Install a registry with NO coalescers (empty lane map)."""
        reg = CoalescerRegistry.__new__(CoalescerRegistry)
        reg._coalescers = {}
        set_coalescer_registry(reg)

    def test_lane_absent_cache_hit_returns_cached_vec(self, monkeypatch):
        """HIT with no coalescer lane: cache returns cached bytes -> skip live."""
        self._install_registry_no_lanes()
        _patch_config(monkeypatch, enabled=True)

        cached_bytes = _make_cached_bytes(CACHED_VEC)
        cache = _make_cache(enabled=True, voyage_mode="on", hit_bytes=cached_bytes)
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        live_calls: list = []

        def _fake_live(provider, text, embedding_purpose=None, acquire_timeout=30.0):
            live_calls.append(text)
            return LIVE_VEC

        monkeypatch.setattr(governed_call, "_compute_live", _fake_live)

        result = governed_call.coalesced_query_embedding(
            _FakeVoyageProvider(), TEST_TEXT
        )
        # Cache HIT must return the cached vector
        assert result == pytest.approx(CACHED_VEC, abs=1e-4)
        assert live_calls == []
        cache.lookup.assert_called_once()
        cache.record_hit.assert_called_once()
        cache.record_miss_or_shadow.assert_not_called()

    def test_lane_absent_cache_miss_calls_compute_live_and_records(self, monkeypatch):
        """MISS with no coalescer lane: _compute_live called (goes direct) and
        record_miss_or_shadow invoked; live vec returned."""
        self._install_registry_no_lanes()
        _patch_config(monkeypatch, enabled=True)

        cache = _make_cache(enabled=True, voyage_mode="on", hit_bytes=None)
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        live_calls: list = []

        def _fake_live(provider, text, embedding_purpose=None, acquire_timeout=30.0):
            live_calls.append(text)
            return LIVE_VEC

        monkeypatch.setattr(governed_call, "_compute_live", _fake_live)

        result = governed_call.coalesced_query_embedding(
            _FakeVoyageProvider(), TEST_TEXT
        )
        assert result == LIVE_VEC
        assert live_calls == [TEST_TEXT]
        cache.lookup.assert_called_once()
        cache.record_miss_or_shadow.assert_called_once()
        cache.record_hit.assert_not_called()


# ---------------------------------------------------------------------------
# Path 4: Cache is None
# ---------------------------------------------------------------------------


class TestCacheNoneBypassesCache:
    def test_cache_none_uses_coalescer_path(self, monkeypatch):
        """When cache is None, the coalescer path is used (cache never touched)."""
        coalescer = _FakeCoalescer()
        _install_registry_with_coalescer(coalescer)
        _patch_config(monkeypatch, enabled=True)

        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: None)

        result = governed_call.coalesced_query_embedding(
            _FakeVoyageProvider(), TEST_TEXT
        )
        assert result == LIVE_VEC
        # Coalescer submit was called (live path via coalescer, not _compute_live)
        assert coalescer.submitted == [TEST_TEXT]


# ---------------------------------------------------------------------------
# Path 5 & 6: Cache present but not enabled for provider / mode == off
# ---------------------------------------------------------------------------


class TestCacheDisabledForProviderBypassesCache:
    def test_not_enabled_for_provider_calls_compute_live(self, monkeypatch):
        coalescer = _FakeCoalescer()
        _install_registry_with_coalescer(coalescer)
        _patch_config(monkeypatch, enabled=True)

        cache = _make_cache(enabled=False)
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        live_calls: list = []

        def _fake_live(provider, text, embedding_purpose=None, acquire_timeout=30.0):
            live_calls.append(text)
            return LIVE_VEC

        monkeypatch.setattr(governed_call, "_compute_live", _fake_live)

        result = governed_call.coalesced_query_embedding(
            _FakeVoyageProvider(), TEST_TEXT
        )
        assert result == LIVE_VEC
        assert live_calls == [TEST_TEXT]
        cache.lookup.assert_not_called()

    def test_mode_off_calls_compute_live(self, monkeypatch):
        coalescer = _FakeCoalescer()
        _install_registry_with_coalescer(coalescer)
        _patch_config(monkeypatch, enabled=True)

        # enabled_for=True but mode="off"
        cache = _make_cache(enabled=True, voyage_mode="off")
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        live_calls: list = []

        def _fake_live(provider, text, embedding_purpose=None, acquire_timeout=30.0):
            live_calls.append(text)
            return LIVE_VEC

        monkeypatch.setattr(governed_call, "_compute_live", _fake_live)

        result = governed_call.coalesced_query_embedding(
            _FakeVoyageProvider(), TEST_TEXT
        )
        assert result == LIVE_VEC
        assert live_calls == [TEST_TEXT]
        cache.lookup.assert_not_called()


# ---------------------------------------------------------------------------
# Path 7: on-mode HIT — skip _compute_live entirely
# ---------------------------------------------------------------------------


class TestOnModeHit:
    def test_hit_returns_cached_vec_compute_live_not_called(self, monkeypatch):
        coalescer = _FakeCoalescer()
        _install_registry_with_coalescer(coalescer)
        _patch_config(monkeypatch, enabled=True)

        cached_bytes = _make_cached_bytes(CACHED_VEC)
        cache = _make_cache(enabled=True, voyage_mode="on", hit_bytes=cached_bytes)
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        live_calls: list = []

        def _fake_live(provider, text, embedding_purpose=None, acquire_timeout=30.0):
            live_calls.append(text)
            return LIVE_VEC

        monkeypatch.setattr(governed_call, "_compute_live", _fake_live)

        result = governed_call.coalesced_query_embedding(
            _FakeVoyageProvider(), TEST_TEXT
        )

        # Must return the cached vector, not the live one
        assert result == pytest.approx(CACHED_VEC, abs=1e-4)
        # _compute_live and coalescer.submit must NOT be called
        assert live_calls == []
        assert coalescer.submitted == []
        # record_hit (touch_last_used) must be called
        cache.record_hit.assert_called_once()
        # record_miss_or_shadow must NOT be called
        cache.record_miss_or_shadow.assert_not_called()


# ---------------------------------------------------------------------------
# Path 8: on-mode MISS — call _compute_live then record_miss
# ---------------------------------------------------------------------------


class TestOnModeMiss:
    def test_miss_calls_live_path_and_record_miss(self, monkeypatch):
        coalescer = _FakeCoalescer()
        _install_registry_with_coalescer(coalescer)
        _patch_config(monkeypatch, enabled=True)

        cache = _make_cache(enabled=True, voyage_mode="on", hit_bytes=None)
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        result = governed_call.coalesced_query_embedding(
            _FakeVoyageProvider(), TEST_TEXT
        )

        assert result == LIVE_VEC
        # Live path goes through coalescer.submit on a cache-active path
        assert coalescer.submitted == [TEST_TEXT]
        # record_miss_or_shadow must be called (upsert)
        cache.record_miss_or_shadow.assert_called_once()
        # record_hit must NOT be called
        cache.record_hit.assert_not_called()


# ---------------------------------------------------------------------------
# Path 9: shadow-mode HIT — _compute_live still called; touch_last_used; return LIVE
# ---------------------------------------------------------------------------


class TestShadowModeHit:
    def test_hit_still_calls_live_path_returns_live_vec(self, monkeypatch):
        coalescer = _FakeCoalescer()
        _install_registry_with_coalescer(coalescer)
        _patch_config(monkeypatch, enabled=True)

        cached_bytes = _make_cached_bytes(CACHED_VEC)
        cache = _make_cache(enabled=True, voyage_mode="shadow", hit_bytes=cached_bytes)
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        result = governed_call.coalesced_query_embedding(
            _FakeVoyageProvider(), TEST_TEXT
        )

        # In shadow mode: returns the LIVE vector, not the cached one
        assert result == LIVE_VEC
        # Live path goes through coalescer.submit
        assert coalescer.submitted == [TEST_TEXT]
        # touch_last_used called (it's a HIT for shadow bookkeeping)
        cache.record_hit.assert_called_once()
        # record_miss_or_shadow must NOT be called on a HIT
        cache.record_miss_or_shadow.assert_not_called()


# ---------------------------------------------------------------------------
# Path 10: shadow-mode MISS — _compute_live called, record_miss called, return LIVE
# ---------------------------------------------------------------------------


class TestShadowModeMiss:
    def test_miss_calls_live_path_and_record_miss_returns_live(self, monkeypatch):
        coalescer = _FakeCoalescer()
        _install_registry_with_coalescer(coalescer)
        _patch_config(monkeypatch, enabled=True)

        cache = _make_cache(enabled=True, voyage_mode="shadow", hit_bytes=None)
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        result = governed_call.coalesced_query_embedding(
            _FakeVoyageProvider(), TEST_TEXT
        )

        assert result == LIVE_VEC
        # Live path goes through coalescer.submit
        assert coalescer.submitted == [TEST_TEXT]
        cache.record_miss_or_shadow.assert_called_once()
        cache.record_hit.assert_not_called()


# ---------------------------------------------------------------------------
# Story #1106 wiring: anchor_tokens dial takes effect THROUGH the wrap
# ---------------------------------------------------------------------------


class TestAnchorTokenDialThroughWrap:
    """Prove the per-provider anchor_tokens config knob is actually applied in
    coalesced_query_embedding (Story #1106 wiring gap fix).

    We use a REAL QueryEmbeddingCache backed by a real SQLite backend so the
    lookup/record cycle is genuine (no MagicMock short-circuits).

    Scenario A — anchor_tokens=0 (sort ALL tokens):
      Two queries that are reorderings of the same token bag must produce the
      SAME cache key so the second query is a HIT and no second live embed is
      issued.

    Scenario B — anchor_tokens=3 (exact-match for 3-token queries):
      Two queries with the same token bag but different orderings produce
      DISTINCT cache keys so both are MISSes and two live embeds are issued.
    """

    def _make_real_cache(
        self, tmp_path, *, anchor_tokens: int, mode: str = "on"
    ) -> object:
        """Build a QueryEmbeddingCache with a real SQLite backend.

        The real lookup/record cycle runs against real SQLite so hit/miss
        behaviour is genuine.  Two instance methods are patched to isolate
        the test from the live config service:

        - ``anchor_tokens_for`` -> returns *anchor_tokens* (an int)
        - ``mode_for`` -> returns *mode* (bypasses live-config default of
          "shadow" which would prevent the HIT short-circuit in on-mode tests)
        """
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )
        from code_indexer.server.storage.sqlite_backends import (
            QueryEmbeddingCacheSqliteBackend,
        )

        backend = QueryEmbeddingCacheSqliteBackend(str(tmp_path / "qec.db"))
        cache = QueryEmbeddingCache(backend=backend, enabled=True, voyage_mode=mode)
        # Patch anchor_tokens_for so it returns the desired int value live,
        # bypassing the live config service (which may not be wired in unit tests).
        cache.anchor_tokens_for = lambda provider_name: anchor_tokens  # type: ignore[method-assign]
        # Patch mode_for for the same reason: the live config service defaults
        # voyage_mode to "shadow", which overrides the constructor argument and
        # would prevent on-mode HIT short-circuiting in test_anchor0_reorderings_are_hit.
        cache.mode_for = lambda provider_name: mode  # type: ignore[method-assign]
        return cache

    def test_anchor0_reorderings_are_hit(self, monkeypatch, tmp_path):
        """anchor_tokens=0 -> sort ALL tokens -> same-bag reorderings share key.

        First call: MISS -> live embed called, result cached.
        Second call (tail-reordered): same cache key -> HIT, no live embed.
        """
        cache = self._make_real_cache(tmp_path, anchor_tokens=0)
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        live_call_count: List[int] = [0]

        def _fake_live(provider, text, embedding_purpose=None, acquire_timeout=30.0):
            live_call_count[0] += 1
            return LIVE_VEC

        monkeypatch.setattr(governed_call, "_compute_live", _fake_live)

        q1 = "find authentication middleware"
        q2 = "authentication find middleware"  # same tokens, different order

        result1 = governed_call.coalesced_query_embedding(_FakeVoyageProvider(), q1)
        assert result1 == LIVE_VEC
        assert live_call_count[0] == 1, "First call must go live (MISS)"

        result2 = governed_call.coalesced_query_embedding(_FakeVoyageProvider(), q2)
        # anchor=0 collapses both to the same key -> HIT -> cached vec returned
        assert result2 == pytest.approx(LIVE_VEC, abs=1e-4)
        assert live_call_count[0] == 1, (
            "Second call must be a cache HIT (anchor_tokens=0 sorts all tokens)"
        )

    def test_anchor3_distinct_orderings_are_both_miss(self, monkeypatch, tmp_path):
        """anchor_tokens=3 with 3-token queries -> exact-match semantics.

        Different orderings produce different keys, so both calls are MISSes
        and two live embeds are issued.
        """
        cache = self._make_real_cache(tmp_path, anchor_tokens=3)
        monkeypatch.setattr(governed_call, "get_query_embedding_cache", lambda: cache)

        live_call_count: List[int] = [0]

        def _fake_live(provider, text, embedding_purpose=None, acquire_timeout=30.0):
            live_call_count[0] += 1
            return LIVE_VEC

        monkeypatch.setattr(governed_call, "_compute_live", _fake_live)

        q1 = "find authentication middleware"
        q2 = "authentication find middleware"  # same 3 tokens, different order

        governed_call.coalesced_query_embedding(_FakeVoyageProvider(), q1)
        assert live_call_count[0] == 1, "First call must go live (MISS)"

        governed_call.coalesced_query_embedding(_FakeVoyageProvider(), q2)
        assert live_call_count[0] == 2, (
            "Second call must also be a MISS (anchor_tokens=3 -> exact-match for 3-token query)"
        )
