"""
Bug #1399 CRITICAL item 2: memory_governor_sample_interval_seconds must be
read LIVE from config_service, not frozen at the MemoryGovernor constructor
default (2.0).

Root cause (per the issue): Story #1213 Story 2's "live hot-reload" of
governor fields deliberately excluded sample_interval_seconds --
_sampler_loop() always slept on the constructor-frozen
self._sample_interval_seconds, while get_snapshot() echoed the *configured*
DB value as if the sampler were honoring it (misleading dashboard signal).

Fix: _sampler_loop() now resolves the effective interval via a new
_resolve_sample_interval_seconds() helper (mirrors the existing
_read_live_config() live-read pattern used by _tick()) on every iteration,
so a Web UI change takes effect on the very next sleep cycle without a
restart -- consistent with how yellow_pct/red_pct/hysteresis_pct/
swap_forces_red/swap_pswpin_threshold/red_min_dwell already behave.
"""

from __future__ import annotations

from unittest.mock import MagicMock


class _FakeReaders:
    """Minimal fake memory readers (not exercised by these tests directly)."""

    def read_cgroup_v2_max(self) -> str:
        raise FileNotFoundError("no cgroup v2")

    def read_cgroup_v1_limit(self) -> int:
        raise FileNotFoundError("no cgroup v1")

    def read_host_memory(self):
        m = MagicMock()
        m.total = 16 * 1024 * 1024 * 1024
        m.used = int(m.total * 0.10)
        return m

    def read_pswpin(self) -> int:
        return 0


class _FakeConfigService:
    """Minimal config-service stub with a mutable CacheConfig.

    ServerConfig requires a server_dir string but this stub never performs
    any filesystem I/O (get_config() just returns the in-memory object), so
    a non-path placeholder is used rather than a real/temp directory.
    """

    def __init__(
        self,
        sample_interval_seconds: float = 2.0,
        server_dir: str = "unused-server-dir-placeholder",
    ):
        from code_indexer.server.utils.config_manager import CacheConfig, ServerConfig

        cache = CacheConfig(
            memory_governor_sample_interval_seconds=sample_interval_seconds,
        )
        self._config = ServerConfig(server_dir=server_dir, cache_config=cache)

    def get_config(self):
        return self._config

    @property
    def cache(self):
        return self._config.cache_config


# ---------------------------------------------------------------------------
# Test 1: _resolve_sample_interval_seconds reads live config
# ---------------------------------------------------------------------------


class TestResolveSampleIntervalSeconds:
    def test_reads_live_value_from_config_service(self):
        from code_indexer.server.services.memory_governor import MemoryGovernor

        cfg = _FakeConfigService(sample_interval_seconds=7.5)
        gov = MemoryGovernor(
            readers=_FakeReaders(),
            start_sampler=False,
            sample_interval_seconds=2.0,  # constructor default -- must be ignored
            config_service=cfg,
        )

        assert gov._resolve_sample_interval_seconds() == 7.5, (
            "Bug #1399: sample_interval_seconds must be read LIVE from "
            "config_service, not frozen at the constructor default."
        )

    def test_falls_back_to_constructor_default_without_config_service(self):
        from code_indexer.server.services.memory_governor import MemoryGovernor

        gov = MemoryGovernor(
            readers=_FakeReaders(),
            start_sampler=False,
            sample_interval_seconds=3.3,
        )

        assert gov._resolve_sample_interval_seconds() == 3.3

    def test_falls_back_to_constructor_default_on_config_read_failure(self):
        from code_indexer.server.services.memory_governor import MemoryGovernor

        class _BrokenCfg:
            def get_config(self):
                raise RuntimeError("DB unavailable")

        gov = MemoryGovernor(
            readers=_FakeReaders(),
            start_sampler=False,
            sample_interval_seconds=4.4,
            config_service=_BrokenCfg(),
        )

        assert gov._resolve_sample_interval_seconds() == 4.4

    def test_hot_reload_reflects_change_between_two_reads(self):
        """No caching: a config change between two calls must be observed
        on the very next call (proves per-call live read, not module/instance
        caching)."""
        from code_indexer.server.services.memory_governor import MemoryGovernor

        cfg = _FakeConfigService(sample_interval_seconds=2.0)
        gov = MemoryGovernor(
            readers=_FakeReaders(),
            start_sampler=False,
            sample_interval_seconds=2.0,
            config_service=cfg,
        )

        assert gov._resolve_sample_interval_seconds() == 2.0

        cfg.cache.memory_governor_sample_interval_seconds = 9.9

        assert gov._resolve_sample_interval_seconds() == 9.9


# ---------------------------------------------------------------------------
# Test 2: _sampler_loop actually uses the live-resolved interval
# ---------------------------------------------------------------------------


class TestSamplerLoopUsesLiveInterval:
    def test_sampler_loop_sleeps_using_live_sample_interval(self):
        """Run _sampler_loop() synchronously (no real thread/sleep) by
        spying on self._stop_event.wait and stopping the loop after the
        first iteration. Asserts the wait() timeout argument equals the
        LIVE config value, not the constructor-frozen default."""
        from code_indexer.server.services.memory_governor import MemoryGovernor

        cfg = _FakeConfigService(sample_interval_seconds=0.05)
        gov = MemoryGovernor(
            readers=_FakeReaders(),
            start_sampler=False,
            sample_interval_seconds=999.0,  # deliberately different sentinel
            config_service=cfg,
        )

        captured_timeouts: list = []

        def _wait_spy(timeout=None):
            captured_timeouts.append(timeout)
            gov._stop_event.set()
            return True

        gov._stop_event.wait = _wait_spy  # type: ignore[method-assign]
        gov._sampler_loop()

        assert captured_timeouts == [0.05], (
            "Bug #1399: _sampler_loop must sleep using the LIVE "
            f"sample_interval_seconds (0.05), not the constructor default "
            f"(999.0). Captured: {captured_timeouts!r}"
        )

    def test_sampler_loop_falls_back_to_constructor_default_without_config_service(
        self,
    ):
        from code_indexer.server.services.memory_governor import MemoryGovernor

        gov = MemoryGovernor(
            readers=_FakeReaders(),
            start_sampler=False,
            sample_interval_seconds=0.07,
        )

        captured_timeouts: list = []

        def _wait_spy(timeout=None):
            captured_timeouts.append(timeout)
            gov._stop_event.set()
            return True

        gov._stop_event.wait = _wait_spy  # type: ignore[method-assign]
        gov._sampler_loop()

        assert captured_timeouts == [0.07]
