"""Story #1787 AC16: node-local TTL cache on the K-calibration read path.

Uses a real SqliteKCalibrationBackend (temp file, no mocking) plus an
injectable fake clock -- the same time_fn injection pattern
MemoryGovernor itself uses for deterministic time-based tests.
"""

from __future__ import annotations

from code_indexer.server.services.xray_graph_governor.k_calibration_store import (
    KCalibrationSample,
    SqliteKCalibrationBackend,
    TTLCachedKProvider,
)

CONSERVATIVE_DEFAULT = 19.0
TTL_SECONDS = 300.0


class _FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


def _store_with_java_sample(tmp_path):
    store = SqliteKCalibrationBackend(str(tmp_path / "k.db"))
    store.record_sample(
        KCalibrationSample(
            language="java",
            source_bytes=1000,
            decls=1,
            call_sites=1,
            candidate_edges=1,
            actual_peak_rss=19000,
        )
    )
    return store


class _CountingStore:
    """Wraps a real store, counting get_k() calls to prove the TTL cache
    genuinely avoids re-querying within its window."""

    def __init__(self, real_store):
        self._real_store = real_store
        self.get_k_calls = 0

    def get_k(self, language):
        self.get_k_calls += 1
        return self._real_store.get_k(language)

    def record_sample(self, sample):
        self._real_store.record_sample(sample)


def test_cache_miss_falls_through_to_the_store_and_returns_the_real_value(tmp_path):
    counting_store = _CountingStore(_store_with_java_sample(tmp_path))
    provider = TTLCachedKProvider(
        counting_store, ttl_seconds=TTL_SECONDS, time_fn=_FakeClock()
    )

    assert provider.get_k("java", CONSERVATIVE_DEFAULT) == 19.0
    assert counting_store.get_k_calls == 1


def test_cache_hit_within_ttl_never_re_queries_the_store(tmp_path):
    clock = _FakeClock()
    counting_store = _CountingStore(_store_with_java_sample(tmp_path))
    provider = TTLCachedKProvider(
        counting_store, ttl_seconds=TTL_SECONDS, time_fn=clock
    )

    provider.get_k("java", CONSERVATIVE_DEFAULT)
    clock.now += TTL_SECONDS / 2
    result = provider.get_k("java", CONSERVATIVE_DEFAULT)

    assert result == 19.0
    assert counting_store.get_k_calls == 1, (
        "a hit within the TTL window must not re-query the store"
    )


def test_cache_expiry_after_ttl_re_queries_the_store(tmp_path):
    clock = _FakeClock()
    counting_store = _CountingStore(_store_with_java_sample(tmp_path))
    provider = TTLCachedKProvider(
        counting_store, ttl_seconds=TTL_SECONDS, time_fn=clock
    )

    provider.get_k("java", CONSERVATIVE_DEFAULT)
    clock.now += TTL_SECONDS + 1.0
    provider.get_k("java", CONSERVATIVE_DEFAULT)

    assert counting_store.get_k_calls == 2, (
        "a read past TTL expiry must re-query the store"
    )


def test_a_language_with_no_recorded_samples_degrades_to_the_conservative_default(
    tmp_path,
):
    store = SqliteKCalibrationBackend(str(tmp_path / "k.db"))
    provider = TTLCachedKProvider(store, ttl_seconds=TTL_SECONDS, time_fn=_FakeClock())

    assert provider.get_k("cobol", CONSERVATIVE_DEFAULT) == CONSERVATIVE_DEFAULT


class TestXrayKProviderProcessSingleton:
    """H4/H6 remediation: service_init.py installs ONE TTLCachedKProvider
    at startup (wrapping the storage-mode-appropriate K-calibration
    backend); a future graph-build call site must be able to retrieve
    that SAME instance -- mirroring memory_governor.py's own
    get/set/clear_memory_governor() singleton pattern.
    """

    def teardown_method(self) -> None:
        from code_indexer.server.services.xray_graph_governor.k_calibration_store import (
            clear_xray_k_provider,
        )

        clear_xray_k_provider()

    def test_get_returns_none_before_any_set(self) -> None:
        from code_indexer.server.services.xray_graph_governor.k_calibration_store import (
            clear_xray_k_provider,
            get_xray_k_provider,
        )

        # This is a process-wide singleton: other test modules in the same
        # pytest process (e.g. test_app_lazy_init_repair_1638.py, via
        # initialize_services()) may have already installed an instance
        # before this test runs. Establish the "nothing set" precondition
        # ourselves rather than relying on process history/ordering.
        clear_xray_k_provider()
        assert get_xray_k_provider() is None

    def test_set_then_get_returns_the_same_instance_and_clear_resets_to_none(
        self, tmp_path
    ) -> None:
        from code_indexer.server.services.xray_graph_governor.k_calibration_store import (
            clear_xray_k_provider,
            get_xray_k_provider,
            set_xray_k_provider,
        )

        store = SqliteKCalibrationBackend(str(tmp_path / "k.db"))
        provider = TTLCachedKProvider(store, ttl_seconds=TTL_SECONDS)
        set_xray_k_provider(provider)
        assert get_xray_k_provider() is provider

        clear_xray_k_provider()
        assert get_xray_k_provider() is None
