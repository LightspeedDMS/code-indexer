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
