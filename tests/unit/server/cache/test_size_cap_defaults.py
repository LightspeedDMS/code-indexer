"""
Tests for Fix B.1 (Issue #878): Opinionated max_cache_size_mb default.

Fix B.1 applies a default cap of 4096 MB in the server-init path
(``get_global_cache()`` / ``get_global_fts_cache()``) when the loaded
configuration has ``max_cache_size_mb is None``. The dataclass default
remains ``None`` so operators can still opt out explicitly via config.

Contract under test:

1. When ``HNSWIndexCacheConfig.max_cache_size_mb is None`` at init time,
   ``get_global_cache()`` sets it to 4096 before constructing the singleton.
2. When ``HNSWIndexCacheConfig.max_cache_size_mb`` has an explicit value,
   ``get_global_cache()`` preserves that value verbatim.
3. Same invariants apply to ``get_global_fts_cache()`` and
   ``FTSIndexCacheConfig``.
4. An INFO log is emitted on the default-applied path (for both HNSW and
   FTS) so operators can tell they are running on the opinionated default
   rather than an explicit value.

These tests construct real cache instances (no mocks of cache internals) and
redirect ``Path.home()`` to a tmp path so that the tests are hermetic from
any real ``~/.cidx-server/config.json`` on the developer machine.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import ModuleType
from typing import Iterator

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stop_and_clear_singletons(cache_module: ModuleType) -> None:
    """
    Stop any running cleanup thread on the HNSW / FTS cache singletons and
    clear the module-level slots back to ``None``.

    Expected exceptions are narrowed to ``RuntimeError`` (which the threading
    APIs may raise when a thread is not running / already stopped). Anything
    else is a real bug in teardown and is allowed to propagate so the test
    run fails loudly instead of silently leaving threads behind.
    """
    for attr in ("_global_cache_instance", "_global_fts_cache_instance"):
        instance = getattr(cache_module, attr)
        if instance is None:
            continue
        try:
            instance.stop_background_cleanup()
        except RuntimeError:
            # Thread not alive / already stopped: safe to ignore.
            pass
        setattr(cache_module, attr, None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singletons_and_isolate_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    """
    Reset HNSW and FTS global singletons around each test AND redirect
    ``Path.home()`` to a tmp directory so that no developer-machine
    ``~/.cidx-server/config.json`` leaks into the tests.
    """
    # Redirect Path.home() BEFORE using get_global_cache so the config-file
    # loading branch does not find a real config on disk.
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    import code_indexer.server.cache as cache_module

    _stop_and_clear_singletons(cache_module)
    yield
    _stop_and_clear_singletons(cache_module)


# ---------------------------------------------------------------------------
# HNSW cache tests
# ---------------------------------------------------------------------------


class TestHNSWCacheDefaultSizeCap:
    """``get_global_cache()`` must apply 4096 MB default when config says None."""

    def test_hnsw_cache_applies_default_when_config_is_none(self) -> None:
        """
        Given ``~/.cidx-server/config.json`` is absent and no cache-related
        env vars are set, ``HNSWIndexCacheConfig.from_env()`` produces a
        config with ``max_cache_size_mb is None``. ``get_global_cache()``
        must overlay the opinionated default of 4096 MB on top of that
        before constructing the singleton.
        """
        from code_indexer.server.cache import get_global_cache

        cache = get_global_cache()

        assert cache.config.max_cache_size_mb == 4096, (
            "get_global_cache() must apply opinionated default "
            "max_cache_size_mb=4096 when loaded config has None; "
            f"got {cache.config.max_cache_size_mb!r}"
        )

    def test_hnsw_cache_respects_explicit_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        If the loaded config has an explicit ``max_cache_size_mb`` value,
        ``get_global_cache()`` must not overwrite it. We inject an explicit
        value of 2048 MB via the documented environment variable channel,
        then assert the singleton preserves it.
        """
        monkeypatch.setenv("CIDX_INDEX_CACHE_MAX_SIZE_MB", "2048")

        from code_indexer.server.cache import get_global_cache

        cache = get_global_cache()

        assert cache.config.max_cache_size_mb == 2048, (
            "Explicit max_cache_size_mb must be preserved verbatim; "
            f"got {cache.config.max_cache_size_mb!r}"
        )


# ---------------------------------------------------------------------------
# FTS cache tests
# ---------------------------------------------------------------------------


class TestFTSCacheDefaultSizeCap:
    """``get_global_fts_cache()`` must apply 4096 MB default when config says None."""

    def test_fts_cache_applies_default_when_config_is_none(self) -> None:
        """
        Given no ``~/.cidx-server/config.json`` and no FTS cache env vars,
        ``FTSIndexCacheConfig.from_env()`` yields ``max_cache_size_mb is None``.
        ``get_global_fts_cache()`` must overlay the opinionated default of
        4096 MB before constructing the FTS singleton.
        """
        from code_indexer.server.cache import get_global_fts_cache

        cache = get_global_fts_cache()

        assert cache.config.max_cache_size_mb == 4096, (
            "get_global_fts_cache() must apply opinionated default "
            "max_cache_size_mb=4096 when loaded config has None; "
            f"got {cache.config.max_cache_size_mb!r}"
        )

    def test_fts_cache_respects_explicit_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Explicit FTS cache size must be preserved verbatim; the default
        overlay must only activate when the loaded config says ``None``.
        """
        monkeypatch.setenv("CIDX_FTS_CACHE_MAX_SIZE_MB", "2048")

        from code_indexer.server.cache import get_global_fts_cache

        cache = get_global_fts_cache()

        assert cache.config.max_cache_size_mb == 2048, (
            "Explicit FTS max_cache_size_mb must be preserved verbatim; "
            f"got {cache.config.max_cache_size_mb!r}"
        )


# ---------------------------------------------------------------------------
# INFO log tests — HNSW and FTS both get coverage
# ---------------------------------------------------------------------------


class TestDefaultAppliedLogMessage:
    """INFO log must be emitted when the opinionated default is applied."""

    _EXPECTED_MARKER = "Applying default max_cache_size_mb=4096MB"

    def _assert_default_applied_info_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Shared assertion: at least one INFO record carries the marker."""
        matches = [
            rec for rec in caplog.records if self._EXPECTED_MARKER in rec.message
        ]
        assert matches, (
            "Expected an INFO log containing "
            f"'{self._EXPECTED_MARKER}' on the cache init path, "
            "but no such record was emitted. Records seen: "
            f"{[rec.message for rec in caplog.records]}"
        )
        assert any(rec.levelno == logging.INFO for rec in matches), (
            "Default-applied log must be at INFO level, not warning/error."
        )

    def test_default_applied_log_message_emitted_for_hnsw(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        ``get_global_cache()`` must emit the default-applied INFO log on
        the opinionated-default code path.
        """
        from code_indexer.server.cache import get_global_cache

        with caplog.at_level(logging.INFO, logger="code_indexer.server.cache"):
            get_global_cache()

        self._assert_default_applied_info_log(caplog)

    def test_default_applied_log_message_emitted_for_fts(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        ``get_global_fts_cache()`` must emit the default-applied INFO log
        on the opinionated-default code path (parallel to HNSW).
        """
        from code_indexer.server.cache import get_global_fts_cache

        with caplog.at_level(logging.INFO, logger="code_indexer.server.cache"):
            get_global_fts_cache()

        self._assert_default_applied_info_log(caplog)


# ---------------------------------------------------------------------------
# Story #1166 helpers — extract effective cap from initialized singletons
# ---------------------------------------------------------------------------


def _configured_hnsw_cap(cache_module: ModuleType) -> int:
    """Return effective max_cache_size_mb from the HNSW singleton."""
    instance = cache_module._global_cache_instance
    assert instance is not None, "HNSW singleton must be initialized"
    return instance.config.max_cache_size_mb  # type: ignore[no-any-return]


def _configured_fts_cap(cache_module: ModuleType) -> int:
    """Return effective max_cache_size_mb from the FTS singleton."""
    instance = cache_module._global_fts_cache_instance
    assert instance is not None, "FTS singleton must be initialized"
    return instance.config.max_cache_size_mb  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Story #1166: initialize_caches per-worker budget division tests
# ---------------------------------------------------------------------------


class TestInitializeCaches:
    """Story #1166: initialize_caches(worker_count) divides caps by N workers."""

    def test_ac1_four_workers_divides_cap(self) -> None:
        """
        AC1: With 4096 MB default cap and 4 workers, each singleton gets
        4096 // 4 == 1024 MB.  Both HNSW and FTS must reflect the divided cap.
        """
        import code_indexer.server.cache as cache_module
        from code_indexer.server.cache import initialize_caches

        initialize_caches(worker_count=4)

        assert _configured_hnsw_cap(cache_module) == 1024, (
            "HNSW cap should be 4096//4=1024 with 4 workers; "
            f"got {_configured_hnsw_cap(cache_module)}"
        )
        assert _configured_fts_cap(cache_module) == 1024, (
            "FTS cap should be 4096//4=1024 with 4 workers; "
            f"got {_configured_fts_cap(cache_module)}"
        )

    def test_ac2_one_worker_cap_unchanged(self) -> None:
        """
        AC2 regression: With 1 worker, caps remain at 4096 — identical to
        today's lazy-getter behaviour.  No division must occur.
        """
        import code_indexer.server.cache as cache_module
        from code_indexer.server.cache import (
            initialize_caches,
            DEFAULT_MAX_CACHE_SIZE_MB,
        )

        initialize_caches(worker_count=1)

        assert _configured_hnsw_cap(cache_module) == DEFAULT_MAX_CACHE_SIZE_MB, (
            f"1-worker HNSW cap must equal DEFAULT_MAX_CACHE_SIZE_MB="
            f"{DEFAULT_MAX_CACHE_SIZE_MB}; got {_configured_hnsw_cap(cache_module)}"
        )
        assert _configured_fts_cap(cache_module) == DEFAULT_MAX_CACHE_SIZE_MB, (
            f"1-worker FTS cap must equal DEFAULT_MAX_CACHE_SIZE_MB="
            f"{DEFAULT_MAX_CACHE_SIZE_MB}; got {_configured_fts_cap(cache_module)}"
        )

    def test_ac3_over_division_floor(self) -> None:
        """
        AC3: With 32 workers and a 4096 MB cap, 4096 // 32 == 128 which is
        below MIN_CAP_PER_WORKER_MB (256).  The floor must apply: caps == 256.
        """
        import code_indexer.server.cache as cache_module
        from code_indexer.server.cache import initialize_caches, MIN_CAP_PER_WORKER_MB

        initialize_caches(worker_count=32)

        assert _configured_hnsw_cap(cache_module) == MIN_CAP_PER_WORKER_MB, (
            f"HNSW cap must floor at MIN_CAP_PER_WORKER_MB={MIN_CAP_PER_WORKER_MB} "
            f"when division yields a smaller value; got {_configured_hnsw_cap(cache_module)}"
        )
        assert _configured_fts_cap(cache_module) == MIN_CAP_PER_WORKER_MB, (
            f"FTS cap must floor at MIN_CAP_PER_WORKER_MB={MIN_CAP_PER_WORKER_MB} "
            f"when division yields a smaller value; got {_configured_fts_cap(cache_module)}"
        )

    def test_misconfig_zero_worker_count_prevents_div_by_zero(self) -> None:
        """
        worker_count=0 must not raise ZeroDivisionError. The effective divisor
        is max(1, 0) == 1, so the full DEFAULT_MAX_CACHE_SIZE_MB cap is used.
        """
        import code_indexer.server.cache as cache_module
        from code_indexer.server.cache import (
            initialize_caches,
            DEFAULT_MAX_CACHE_SIZE_MB,
        )

        initialize_caches(worker_count=0)

        assert _configured_hnsw_cap(cache_module) == DEFAULT_MAX_CACHE_SIZE_MB, (
            "worker_count=0 must treat divisor as 1 (full cap); "
            f"got {_configured_hnsw_cap(cache_module)}"
        )
        assert _configured_fts_cap(cache_module) == DEFAULT_MAX_CACHE_SIZE_MB, (
            "worker_count=0 must treat divisor as 1 (full cap) for FTS; "
            f"got {_configured_fts_cap(cache_module)}"
        )

    def test_misconfig_negative_worker_count_prevents_div_by_zero(self) -> None:
        """
        Negative worker_count (e.g. -1) must not raise. Effective divisor is
        max(1, -1) == 1 so caps equal DEFAULT_MAX_CACHE_SIZE_MB for both caches.
        """
        import code_indexer.server.cache as cache_module
        from code_indexer.server.cache import (
            initialize_caches,
            DEFAULT_MAX_CACHE_SIZE_MB,
        )

        initialize_caches(worker_count=-1)

        assert _configured_hnsw_cap(cache_module) == DEFAULT_MAX_CACHE_SIZE_MB, (
            "Negative worker_count must treat divisor as 1 (full cap); "
            f"got {_configured_hnsw_cap(cache_module)}"
        )
        assert _configured_fts_cap(cache_module) == DEFAULT_MAX_CACHE_SIZE_MB, (
            "Negative worker_count must treat divisor as 1 (full cap) for FTS; "
            f"got {_configured_fts_cap(cache_module)}"
        )

    def test_no_double_construct_lazy_getter_returns_same_instance(self) -> None:
        """
        After initialize_caches(4), calling get_global_cache() and
        get_global_fts_cache() MUST return the already-built singleton with
        the DIVIDED cap — not reconstruct a new full-cap instance.
        """
        import code_indexer.server.cache as cache_module
        from code_indexer.server.cache import (
            initialize_caches,
            get_global_cache,
            get_global_fts_cache,
        )

        initialize_caches(worker_count=4)

        hnsw_after_init = cache_module._global_cache_instance
        fts_after_init = cache_module._global_fts_cache_instance

        hnsw_via_getter = get_global_cache()
        fts_via_getter = get_global_fts_cache()

        assert hnsw_via_getter is hnsw_after_init, (
            "get_global_cache() must return the already-built singleton; "
            "a new full-cap instance was returned instead"
        )
        assert fts_via_getter is fts_after_init, (
            "get_global_fts_cache() must return the already-built singleton; "
            "a new full-cap instance was returned instead"
        )
        assert hnsw_via_getter.config.max_cache_size_mb == 1024, (
            "Lazy getter must NOT overwrite the divided cap with the full cap; "
            f"got {hnsw_via_getter.config.max_cache_size_mb}"
        )
        assert fts_via_getter.config.max_cache_size_mb == 1024, (
            "Lazy getter must NOT overwrite the FTS divided cap; "
            f"got {fts_via_getter.config.max_cache_size_mb}"
        )

    def test_lazy_fallback_unchanged_when_initialize_not_called(self) -> None:
        """
        When initialize_caches is NOT called, get_global_cache() must build
        at the full DEFAULT_MAX_CACHE_SIZE_MB cap — CLI / single-worker
        behaviour is completely unchanged.
        """
        import code_indexer.server.cache as cache_module
        from code_indexer.server.cache import (
            get_global_cache,
            DEFAULT_MAX_CACHE_SIZE_MB,
        )

        assert cache_module._global_cache_instance is None, (
            "Singleton must be None before lazy getter call"
        )

        cache = get_global_cache()

        assert cache.config.max_cache_size_mb == DEFAULT_MAX_CACHE_SIZE_MB, (
            "Lazy getter (no initialize_caches call) must build at full "
            f"DEFAULT_MAX_CACHE_SIZE_MB={DEFAULT_MAX_CACHE_SIZE_MB}; "
            f"got {cache.config.max_cache_size_mb}"
        )
