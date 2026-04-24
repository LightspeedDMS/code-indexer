"""
Tests for Bug #897 mitigation 1: malloc_trim(0) feature flag in HNSW cache cleanup.

Verifies that _cleanup_expired_entries() calls malloc_trim(0) when
enable_malloc_trim is True (via bootstrap config), and correctly skips it in
all other cases: flag off, non-Linux platform, libc load failure, missing symbol.
"""

from unittest.mock import MagicMock, patch

import pytest

# Named constants
SHORT_TTL_MINUTES = 0.001
CLEANUP_INTERVAL_SECONDS = 60
MALLOC_TRIM_PAD = 0  # argument passed to glibc malloc_trim


@pytest.fixture
def cache():
    """HNSWIndexCache with a very short TTL so entries expire immediately."""
    from code_indexer.server.cache.hnsw_index_cache import (
        HNSWIndexCache,
        HNSWIndexCacheConfig,
    )

    return HNSWIndexCache(
        config=HNSWIndexCacheConfig(
            ttl_minutes=SHORT_TTL_MINUTES,
            cleanup_interval_seconds=CLEANUP_INTERVAL_SECONDS,
        )
    )


@pytest.fixture(autouse=True)
def reset_libc_globals(monkeypatch):
    """Reset the lazy-load globals before every test via monkeypatch.

    monkeypatch automatically restores original values after each test,
    preventing cross-test state leakage without ordering dependency.
    """
    import code_indexer.server.cache.hnsw_index_cache as mod

    monkeypatch.setattr(mod, "_LIBC_HANDLE", None)
    monkeypatch.setattr(mod, "_LIBC_LOAD_ATTEMPTED", False)


def _make_server_config(enable_malloc_trim: bool):
    """Build a minimal fake ServerConfig with the malloc_trim bootstrap flag."""
    config = MagicMock()
    config.enable_malloc_trim = enable_malloc_trim
    return config


def _run_cleanup(cache, *, platform, enable_malloc_trim, cdll_side_effect=None):
    """Run _cleanup_expired_entries() with controlled platform, config, and CDLL.

    ServerConfigManager is patched as the external bootstrap config boundary.
    ctypes.CDLL is patched at the stdlib level (lazy import inside _maybe_malloc_trim).

    Args:
        cache: HNSWIndexCache instance under test.
        platform: sys.platform value (e.g. "linux", "darwin").
        enable_malloc_trim: Value of the bootstrap flag.
        cdll_side_effect: Optional MagicMock for libc handle, or Exception to
                          simulate CDLL load failure.

    Returns:
        The fake_libc MagicMock, or None when cdll_side_effect is an Exception.
    """
    fake_config = _make_server_config(enable_malloc_trim)

    if isinstance(cdll_side_effect, Exception):
        cdll_kw = {"side_effect": cdll_side_effect}
        fake_libc = None
    else:
        fake_libc = cdll_side_effect if cdll_side_effect is not None else MagicMock()
        cdll_kw = {"return_value": fake_libc}

    with (
        patch("sys.platform", platform),
        patch("ctypes.CDLL", **cdll_kw),
        patch(
            "code_indexer.server.utils.config_manager.ServerConfigManager"
        ) as mock_mgr_cls,
    ):
        mock_mgr_cls.return_value.load_config.return_value = fake_config
        cache._cleanup_expired_entries()

    return fake_libc


# ---------------------------------------------------------------------------
# Test 1: malloc_trim(0) called once when flag is True on Linux
# ---------------------------------------------------------------------------


def test_cleanup_calls_malloc_trim_when_flag_enabled(cache):
    fake_libc = _run_cleanup(cache, platform="linux", enable_malloc_trim=True)
    fake_libc.malloc_trim.assert_called_once_with(MALLOC_TRIM_PAD)


# ---------------------------------------------------------------------------
# Test 2: malloc_trim(0) NOT called when flag is False
# ---------------------------------------------------------------------------


def test_cleanup_does_not_call_malloc_trim_when_flag_disabled(cache):
    fake_libc = _run_cleanup(cache, platform="linux", enable_malloc_trim=False)
    fake_libc.malloc_trim.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: malloc_trim silently no-ops on non-Linux (macOS dev)
# ---------------------------------------------------------------------------


def test_malloc_trim_silently_noops_on_non_linux(cache):
    fake_libc = _run_cleanup(cache, platform="darwin", enable_malloc_trim=True)
    fake_libc.malloc_trim.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: ctypes.CDLL raises OSError — no exception escapes
# ---------------------------------------------------------------------------


def test_malloc_trim_handles_libc_load_failure(cache):
    result = _run_cleanup(
        cache,
        platform="linux",
        enable_malloc_trim=True,
        cdll_side_effect=OSError("No such file or directory"),
    )
    assert result is None  # confirms CDLL raised — test passes because no exception


# ---------------------------------------------------------------------------
# Test 5: malloc_trim raises AttributeError (musl libc) — no exception escapes
# ---------------------------------------------------------------------------


def test_malloc_trim_handles_musl_missing_symbol(cache):
    musl_libc = MagicMock()
    musl_libc.malloc_trim.side_effect = AttributeError("musl libc has no malloc_trim")

    fake_libc = _run_cleanup(
        cache,
        platform="linux",
        enable_malloc_trim=True,
        cdll_side_effect=musl_libc,
    )
    # Reaching this assertion proves AttributeError was silently absorbed.
    assert fake_libc is musl_libc
