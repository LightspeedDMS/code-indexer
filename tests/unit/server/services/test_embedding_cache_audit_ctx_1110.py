"""Story #1110 (S6 Chunk A): audit_ctx plumbing + metrics — deep-fidelity audit.

Tests:
  1. audit_ctx populated on sampled on-mode cache HIT:
       - sampled=True, mode="on", provider=..., cached_blob present, NO live_vec key.
  2. audit_ctx populated on sampled shadow-mode cache HIT:
       - sampled=True, mode="shadow", provider=..., cached_blob present, live_vec present.
  3. audit_ctx NOT populated when rate=0.0 (default off).
  4. audit_ctx NOT populated when random.random() >= rate (monkeypatched to 0.99).
  5. audit_ctx NOT populated on cache MISS (on-mode or shadow-mode).
  6. audit_ctx=None passed to _serve_with_cache: no crash, normal return.
  7. coalesced_query_embedding accepts audit_ctx kwarg (default None) and threads it into
     _serve_with_cache; default (no kwarg) unchanged behavior.
  8. _audit_sample_rate_for: cohere -> cohere field, else -> voyage field; clamp [0,1];
     fail-open on config exception (returns 0.0).
"""

from __future__ import annotations

import struct
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROVIDER_VOYAGE = "voyage-ai"
PROVIDER_COHERE = "cohere"
MODEL = "voyage-code-3"
DIM = 3
TEXT = "test query"

CACHED_VEC: List[float] = [1.0, 0.0, 0.0]
LIVE_VEC: List[float] = [0.6, 0.8, 0.0]


def _enc(vec: List[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


# ---------------------------------------------------------------------------
# Fake backend (real in-memory, no DB)
# ---------------------------------------------------------------------------


class _FakeBackend:
    def __init__(self) -> None:
        self._store: dict = {}
        self._count = 0

    def lookup(self, key, provider, model, dimension) -> Optional[bytes]:
        return self._store.get((key, provider, model, dimension))

    def upsert(self, key, provider, model, dimension, blob, last_used, created_at):
        self._store[(key, provider, model, dimension)] = blob
        self._count = len(self._store)

    def touch_last_used(self, key, provider, model, dimension, ts):
        pass

    def prune_to_max(self, max_entries):
        pass

    def total_entries(self) -> int:
        return self._count


def _make_cache(
    mode: str = "on", pre_seed: bool = False, provider: str = PROVIDER_VOYAGE
):
    from code_indexer.server.services.query_embedding_cache import (
        CacheQualifier,
        QueryEmbeddingCache,
        build_key,
    )

    backend = _FakeBackend()
    cache = QueryEmbeddingCache(
        backend, enabled=True, voyage_mode=mode, cohere_mode=mode
    )
    # Pin mode so tests are deterministic
    cache.mode_for = lambda pname: mode  # type: ignore[method-assign]
    qualifier = CacheQualifier(provider, MODEL, DIM)
    key = build_key(TEXT)
    if pre_seed:
        backend._store[(key, provider, MODEL, DIM)] = _enc(CACHED_VEC)
        backend._count = 1
    return cache, qualifier, key


# ---------------------------------------------------------------------------
# Helper: build a patched _serve_with_cache call with audit_ctx support
# ---------------------------------------------------------------------------


def _call_serve_with_cache(
    mode: str,
    pre_seed: bool,
    audit_ctx: Optional[Dict[str, Any]],
    provider: str = PROVIDER_VOYAGE,
    audit_sample_rate: float = 1.0,  # force sample by default
    random_val: float = 0.0,  # below 1.0 -> will sample
) -> Optional[Dict[str, Any]]:
    """
    Calls _serve_with_cache with the given params.
    Patches _audit_sample_rate_for to return audit_sample_rate.
    Patches random.random to return random_val.
    Returns the audit_ctx dict after the call (or None if audit_ctx was None).
    """
    from code_indexer.server.services import governed_call

    cache, qualifier, key = _make_cache(mode=mode, pre_seed=pre_seed, provider=provider)

    with (
        patch.object(
            governed_call, "_audit_sample_rate_for", return_value=audit_sample_rate
        ),
        patch("code_indexer.server.services.governed_call.random") as mock_random,
    ):
        mock_random.random.return_value = random_val
        governed_call._serve_with_cache(
            cache,
            provider,
            key,
            qualifier,
            lambda: LIVE_VEC,
            audit_ctx=audit_ctx,
        )

    return audit_ctx


# ===========================================================================
# 1. audit_ctx populated on sampled on-mode HIT
# ===========================================================================


def test_audit_ctx_populated_on_mode_hit_sampled():
    """On-mode HIT + rate>0 + random < rate: audit_ctx gets sampled=True, cached_blob."""
    audit_ctx: Dict[str, Any] = {}
    result = _call_serve_with_cache(
        mode="on",
        pre_seed=True,
        audit_ctx=audit_ctx,
        provider=PROVIDER_VOYAGE,
        audit_sample_rate=1.0,
        random_val=0.0,
    )
    assert result is not None
    assert result.get("sampled") is True
    assert result.get("mode") == "on"
    assert result.get("provider") == PROVIDER_VOYAGE
    assert isinstance(result.get("cached_blob"), bytes)
    # on-mode HIT must NOT have live_vec (Chunk B re-embeds)
    assert "live_vec" not in result, "on-mode audit_ctx must NOT include live_vec"


def test_audit_ctx_cached_blob_correct_bytes_on_mode():
    """The cached_blob in audit_ctx for on-mode must be the exact bytes from the cache."""
    audit_ctx: Dict[str, Any] = {}
    _call_serve_with_cache(
        mode="on",
        pre_seed=True,
        audit_ctx=audit_ctx,
        audit_sample_rate=1.0,
        random_val=0.0,
    )
    expected = _enc(CACHED_VEC)
    assert audit_ctx["cached_blob"] == expected


# ===========================================================================
# 2. audit_ctx populated on sampled shadow-mode HIT
# ===========================================================================


def test_audit_ctx_populated_shadow_mode_hit_sampled():
    """Shadow-mode HIT + rate>0 + random < rate: audit_ctx gets sampled=True,
    cached_blob AND live_vec."""
    audit_ctx: Dict[str, Any] = {}
    result = _call_serve_with_cache(
        mode="shadow",
        pre_seed=True,
        audit_ctx=audit_ctx,
        provider=PROVIDER_VOYAGE,
        audit_sample_rate=1.0,
        random_val=0.0,
    )
    assert result is not None
    assert result.get("sampled") is True
    assert result.get("mode") == "shadow"
    assert result.get("provider") == PROVIDER_VOYAGE
    assert isinstance(result.get("cached_blob"), bytes)
    # Shadow-mode HIT: live_vec IS present (already computed by live_fn)
    assert "live_vec" in result, "shadow-mode audit_ctx must include live_vec"
    assert result["live_vec"] == LIVE_VEC


def test_audit_ctx_shadow_cached_blob_correct():
    """Shadow-mode audit_ctx cached_blob == the pre-seeded bytes."""
    audit_ctx: Dict[str, Any] = {}
    _call_serve_with_cache(
        mode="shadow",
        pre_seed=True,
        audit_ctx=audit_ctx,
        audit_sample_rate=1.0,
        random_val=0.0,
    )
    assert audit_ctx["cached_blob"] == _enc(CACHED_VEC)


# ===========================================================================
# 3. audit_ctx NOT populated when rate=0.0
# ===========================================================================


def test_audit_ctx_not_populated_rate_zero_on_mode():
    """rate=0.0 means never sample; audit_ctx stays empty after on-mode HIT."""
    audit_ctx: Dict[str, Any] = {}
    _call_serve_with_cache(
        mode="on",
        pre_seed=True,
        audit_ctx=audit_ctx,
        audit_sample_rate=0.0,
        random_val=0.0,
    )
    assert audit_ctx == {}, f"audit_ctx should be empty when rate=0.0, got: {audit_ctx}"


def test_audit_ctx_not_populated_rate_zero_shadow_mode():
    """rate=0.0 means never sample; audit_ctx stays empty after shadow-mode HIT."""
    audit_ctx: Dict[str, Any] = {}
    _call_serve_with_cache(
        mode="shadow",
        pre_seed=True,
        audit_ctx=audit_ctx,
        audit_sample_rate=0.0,
        random_val=0.0,
    )
    assert audit_ctx == {}, f"audit_ctx should be empty when rate=0.0, got: {audit_ctx}"


# ===========================================================================
# 4. audit_ctx NOT populated when random.random() >= rate
# ===========================================================================


def test_audit_ctx_not_sampled_when_random_gte_rate():
    """random.random() = 0.99 >= rate=0.5: audit_ctx stays empty (not sampled)."""
    audit_ctx: Dict[str, Any] = {}
    _call_serve_with_cache(
        mode="on",
        pre_seed=True,
        audit_ctx=audit_ctx,
        audit_sample_rate=0.5,
        random_val=0.99,
    )
    assert audit_ctx == {}, (
        f"audit_ctx should be empty when random.random() >= rate, got: {audit_ctx}"
    )


def test_audit_ctx_sampled_when_random_lt_rate():
    """random.random() = 0.1 < rate=0.5: audit_ctx IS populated."""
    audit_ctx: Dict[str, Any] = {}
    _call_serve_with_cache(
        mode="on",
        pre_seed=True,
        audit_ctx=audit_ctx,
        audit_sample_rate=0.5,
        random_val=0.1,
    )
    assert audit_ctx.get("sampled") is True


# ===========================================================================
# 5. audit_ctx NOT populated on cache MISS (on-mode or shadow-mode)
# ===========================================================================


def test_audit_ctx_not_populated_on_miss_on_mode():
    """On-mode MISS: no cached blob, so audit_ctx stays empty."""
    audit_ctx: Dict[str, Any] = {}
    _call_serve_with_cache(
        mode="on",
        pre_seed=False,  # no cached entry -> MISS
        audit_ctx=audit_ctx,
        audit_sample_rate=1.0,
        random_val=0.0,
    )
    assert audit_ctx == {}, (
        f"audit_ctx should be empty on cache MISS (on-mode), got: {audit_ctx}"
    )


def test_audit_ctx_not_populated_on_miss_shadow_mode():
    """Shadow-mode MISS: no prior cached entry, so audit_ctx stays empty."""
    audit_ctx: Dict[str, Any] = {}
    _call_serve_with_cache(
        mode="shadow",
        pre_seed=False,  # no cached entry -> MISS
        audit_ctx=audit_ctx,
        audit_sample_rate=1.0,
        random_val=0.0,
    )
    assert audit_ctx == {}, (
        f"audit_ctx should be empty on shadow MISS, got: {audit_ctx}"
    )


# ===========================================================================
# 6. audit_ctx=None: no crash, normal return
# ===========================================================================


def test_audit_ctx_none_no_crash_on_mode_hit():
    """audit_ctx=None must never crash; on-mode HIT returns cached vec normally."""
    from code_indexer.server.services.governed_call import _serve_with_cache

    cache, qualifier, key = _make_cache(mode="on", pre_seed=True)
    result = _serve_with_cache(
        cache, PROVIDER_VOYAGE, key, qualifier, lambda: LIVE_VEC, audit_ctx=None
    )
    # Should return cached vec (decoded from CACHED_VEC bytes)
    assert len(result) == 3


def test_audit_ctx_none_no_crash_shadow_hit():
    """audit_ctx=None must never crash; shadow-mode HIT returns live vec normally."""
    from code_indexer.server.services.governed_call import _serve_with_cache

    cache, qualifier, key = _make_cache(mode="shadow", pre_seed=True)
    result = _serve_with_cache(
        cache, PROVIDER_VOYAGE, key, qualifier, lambda: LIVE_VEC, audit_ctx=None
    )
    assert result == LIVE_VEC


def test_audit_ctx_none_no_crash_on_mode_miss():
    """audit_ctx=None must never crash on a MISS."""
    from code_indexer.server.services.governed_call import _serve_with_cache

    cache, qualifier, key = _make_cache(mode="on", pre_seed=False)
    result = _serve_with_cache(
        cache, PROVIDER_VOYAGE, key, qualifier, lambda: LIVE_VEC, audit_ctx=None
    )
    assert result == LIVE_VEC


# ===========================================================================
# 7. coalesced_query_embedding accepts audit_ctx kwarg and threads it
# ===========================================================================


def test_coalesced_query_embedding_accepts_audit_ctx_kwarg():
    """coalesced_query_embedding must accept audit_ctx=None (default) without error."""
    from code_indexer.server.services import governed_call

    # Clear cache so we go through the live path (no cache wired)
    governed_call.clear_query_embedding_cache()

    fake_provider = MagicMock()
    fake_provider.get_provider_name.return_value = PROVIDER_VOYAGE

    with patch.object(governed_call, "_compute_live", return_value=LIVE_VEC):
        # Default: audit_ctx not passed
        result = governed_call.coalesced_query_embedding(fake_provider, TEXT)
        assert result == LIVE_VEC

        # Explicit audit_ctx=None
        result2 = governed_call.coalesced_query_embedding(
            fake_provider, TEXT, audit_ctx=None
        )
        assert result2 == LIVE_VEC


def test_coalesced_query_embedding_threads_audit_ctx_into_serve_with_cache():
    """When cache is wired, coalesced_query_embedding passes audit_ctx to _serve_with_cache."""
    from code_indexer.server.services import governed_call

    # Wire a fake cache
    fake_cache = MagicMock()
    fake_cache.enabled_for.return_value = True
    fake_cache.mode_for.return_value = "on"
    fake_cache.build_key_for_provider.return_value = "testkey"
    fake_cache.qualifier.return_value = MagicMock()

    governed_call.set_query_embedding_cache(fake_cache)
    try:
        fake_provider = MagicMock()
        fake_provider.get_provider_name.return_value = PROVIDER_VOYAGE

        audit_ctx: Dict[str, Any] = {}
        captured_audit_ctx: list = []

        def fake_serve(
            cache,
            provider_name,
            cache_key,
            qualifier,
            live_fn,
            *,
            metrics=None,
            audit_ctx=None,
        ):
            captured_audit_ctx.append(audit_ctx)
            return LIVE_VEC

        with patch.object(governed_call, "_serve_with_cache", side_effect=fake_serve):
            governed_call.coalesced_query_embedding(
                fake_provider, TEXT, audit_ctx=audit_ctx
            )

        assert len(captured_audit_ctx) == 1
        assert captured_audit_ctx[0] is audit_ctx
    finally:
        governed_call.clear_query_embedding_cache()


def test_coalesced_query_embedding_default_audit_ctx_is_none():
    """When audit_ctx is not passed, _serve_with_cache receives audit_ctx=None."""
    from code_indexer.server.services import governed_call

    fake_cache = MagicMock()
    fake_cache.enabled_for.return_value = True
    fake_cache.mode_for.return_value = "on"
    fake_cache.build_key_for_provider.return_value = "testkey"
    fake_cache.qualifier.return_value = MagicMock()

    governed_call.set_query_embedding_cache(fake_cache)
    try:
        fake_provider = MagicMock()
        fake_provider.get_provider_name.return_value = PROVIDER_VOYAGE

        captured_audit_ctx: list = []

        def fake_serve(
            cache,
            provider_name,
            cache_key,
            qualifier,
            live_fn,
            *,
            metrics=None,
            audit_ctx=None,
        ):
            captured_audit_ctx.append(audit_ctx)
            return LIVE_VEC

        with patch.object(governed_call, "_serve_with_cache", side_effect=fake_serve):
            governed_call.coalesced_query_embedding(fake_provider, TEXT)

        assert captured_audit_ctx[0] is None
    finally:
        governed_call.clear_query_embedding_cache()


# ===========================================================================
# 8. _audit_sample_rate_for: provider routing, clamping, fail-open
# ===========================================================================


def _mock_config(voyage_rate: float = 0.0, cohere_rate: float = 0.0):
    """Return a MagicMock config with the given per-provider audit rates."""
    qec_cfg = MagicMock()
    qec_cfg.query_embedding_cache_voyage_audit_sample_rate = voyage_rate
    qec_cfg.query_embedding_cache_cohere_audit_sample_rate = cohere_rate
    cfg = MagicMock()
    cfg.query_embedding_cache_config = qec_cfg
    return cfg


def test_audit_sample_rate_for_voyage_reads_voyage_field():
    """voyage-ai provider reads query_embedding_cache_voyage_audit_sample_rate."""
    from code_indexer.server.services import governed_call
    from code_indexer.server.services.config_service import get_config_service

    cfg = _mock_config(voyage_rate=0.3, cohere_rate=0.7)

    with patch.object(get_config_service().__class__, "get_config", return_value=cfg):
        with patch(
            "code_indexer.server.services.governed_call.get_config_service"
        ) as mock_gcs:
            mock_gcs.return_value.get_config.return_value = cfg
            rate = governed_call._audit_sample_rate_for("voyage-ai")

    assert abs(rate - 0.3) < 1e-9, f"Expected 0.3, got {rate}"


def test_audit_sample_rate_for_cohere_reads_cohere_field():
    """cohere provider reads query_embedding_cache_cohere_audit_sample_rate."""
    from code_indexer.server.services import governed_call

    cfg = _mock_config(voyage_rate=0.3, cohere_rate=0.7)

    with patch(
        "code_indexer.server.services.governed_call.get_config_service"
    ) as mock_gcs:
        mock_gcs.return_value.get_config.return_value = cfg
        rate = governed_call._audit_sample_rate_for("cohere")

    assert abs(rate - 0.7) < 1e-9, f"Expected 0.7, got {rate}"


def test_audit_sample_rate_for_unknown_provider_reads_voyage_field():
    """Unknown provider names fall back to the voyage field."""
    from code_indexer.server.services import governed_call

    cfg = _mock_config(voyage_rate=0.4, cohere_rate=0.9)

    with patch(
        "code_indexer.server.services.governed_call.get_config_service"
    ) as mock_gcs:
        mock_gcs.return_value.get_config.return_value = cfg
        rate = governed_call._audit_sample_rate_for("some-other-provider")

    assert abs(rate - 0.4) < 1e-9, f"Expected voyage fallback 0.4, got {rate}"


def test_audit_sample_rate_for_clamped_above_one():
    """Rate values > 1.0 must be clamped to 1.0."""
    from code_indexer.server.services import governed_call

    cfg = _mock_config(voyage_rate=2.5)

    with patch(
        "code_indexer.server.services.governed_call.get_config_service"
    ) as mock_gcs:
        mock_gcs.return_value.get_config.return_value = cfg
        rate = governed_call._audit_sample_rate_for("voyage-ai")

    assert rate == 1.0, f"Expected clamped 1.0, got {rate}"


def test_audit_sample_rate_for_clamped_below_zero():
    """Rate values < 0.0 must be clamped to 0.0."""
    from code_indexer.server.services import governed_call

    cfg = _mock_config(voyage_rate=-0.5)

    with patch(
        "code_indexer.server.services.governed_call.get_config_service"
    ) as mock_gcs:
        mock_gcs.return_value.get_config.return_value = cfg
        rate = governed_call._audit_sample_rate_for("voyage-ai")

    assert rate == 0.0, f"Expected clamped 0.0, got {rate}"


def test_audit_sample_rate_for_fail_open_on_exception():
    """Config read exception must cause fail-open: return 0.0."""
    from code_indexer.server.services import governed_call

    with patch(
        "code_indexer.server.services.governed_call.get_config_service"
    ) as mock_gcs:
        mock_gcs.side_effect = RuntimeError("config unavailable")
        rate = governed_call._audit_sample_rate_for("voyage-ai")

    assert rate == 0.0, f"Expected 0.0 on exception (fail-open), got {rate}"


def test_audit_sample_rate_for_fail_open_on_none_config():
    """None query_embedding_cache_config causes fail-open: return 0.0."""
    from code_indexer.server.services import governed_call

    cfg = MagicMock()
    cfg.query_embedding_cache_config = None

    with patch(
        "code_indexer.server.services.governed_call.get_config_service"
    ) as mock_gcs:
        mock_gcs.return_value.get_config.return_value = cfg
        rate = governed_call._audit_sample_rate_for("voyage-ai")

    assert rate == 0.0, f"Expected 0.0 when config is None (fail-open), got {rate}"


# ===========================================================================
# 9. Audit does NOT break existing serve_with_cache behavior
# ===========================================================================


def test_serve_with_cache_on_mode_hit_still_returns_cached_vec():
    """audit_ctx population must NOT change the returned cached vector."""
    from code_indexer.server.services import governed_call

    cache, qualifier, key = _make_cache(mode="on", pre_seed=True)
    audit_ctx: Dict[str, Any] = {}

    with (
        patch.object(governed_call, "_audit_sample_rate_for", return_value=1.0),
        patch("code_indexer.server.services.governed_call.random") as mock_random,
    ):
        mock_random.random.return_value = 0.0
        result = governed_call._serve_with_cache(
            cache,
            PROVIDER_VOYAGE,
            key,
            qualifier,
            lambda: LIVE_VEC,
            audit_ctx=audit_ctx,
        )

    # Must return decoded CACHED_VEC, not LIVE_VEC
    import struct

    expected = list(struct.unpack(f"<{len(CACHED_VEC)}f", _enc(CACHED_VEC)))
    assert result == expected, f"Expected cached vec {expected}, got {result}"
    # audit_ctx populated
    assert audit_ctx.get("sampled") is True


def test_serve_with_cache_shadow_hit_still_returns_live_vec():
    """audit_ctx population must NOT change shadow-mode returned live vector."""
    from code_indexer.server.services import governed_call

    cache, qualifier, key = _make_cache(mode="shadow", pre_seed=True)
    audit_ctx: Dict[str, Any] = {}

    with (
        patch.object(governed_call, "_audit_sample_rate_for", return_value=1.0),
        patch("code_indexer.server.services.governed_call.random") as mock_random,
    ):
        mock_random.random.return_value = 0.0
        result = governed_call._serve_with_cache(
            cache,
            PROVIDER_VOYAGE,
            key,
            qualifier,
            lambda: LIVE_VEC,
            audit_ctx=audit_ctx,
        )

    assert result == LIVE_VEC
    assert audit_ctx.get("sampled") is True
