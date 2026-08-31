"""Bug #1547 defect 2 (RED): TemporalDedupCache's terminal-entry TTL must
track payload_cache_ttl_seconds from the live config service, never stay
pinned to the hardcoded DEFAULT_TERMINAL_TTL_SECONDS=3600 default.

The dedup'd result a terminal entry points at lives in PayloadCache, evicted
after payload_cache_ttl_seconds (900s default). Before this fix, the dedup
entry's own TTL (3600s) could outlive that payload -- a fast-follow query
joining the "still valid" dedup entry would then read a snapshot that
PayloadCache has already evicted, surfacing a spurious
``{"success": false, "error": "result expired -- resubmit"}`` instead of
either the real result or a fresh submission.

Written BEFORE the fix -- this must genuinely fail against the unmodified
TemporalDedupCache (no ``config_service`` constructor parameter exists yet).
"""

import time

from code_indexer.server.services.temporal_dedup_cache import (
    TemporalDedupCache,
    canonical_signature,
)

#: Deliberately far shorter than TemporalDedupCache's hardcoded
#: DEFAULT_TERMINAL_TTL_SECONDS=3600 default, so a sleep past this value
#: stays well within any sane test timeout while still being nowhere near
#: the hardcoded default.
_CONFIGURED_SHORT_TTL_SECONDS = 0.05

#: A second, even-shorter configured TTL for the override test, so the sleep
#: below is unambiguously past it too.
_CONFIGURED_TINY_TTL_SECONDS = 0.01

#: Sleep durations, each comfortably past the configured TTL(s) above but
#: nowhere near the hardcoded 3600s default or any real test timeout.
_SLEEP_PAST_SHORT_TTL_SECONDS = 0.15
_SLEEP_PAST_TINY_TTL_SECONDS = 0.1

#: Explicit constructor override used in the backward-compatibility test --
#: matches this module's hardcoded default so the assertion is unambiguous
#: about which value won.
_EXPLICIT_OVERRIDE_TTL_SECONDS = 3600.0


class _FakeConfigServiceForTTL:
    """Minimal config-service stub exposing a mutable CacheConfig.payload_
    cache_ttl_seconds -- mirrors the established _FakeConfigService pattern
    used throughout tests/unit/server/services/ (e.g.
    test_memory_governor_sample_interval_hot_reload_1399.py). Never performs
    filesystem I/O; server_dir is an unused placeholder."""

    def __init__(self, payload_cache_ttl_seconds: float) -> None:
        from code_indexer.server.utils.config_manager import CacheConfig, ServerConfig

        cache = CacheConfig(payload_cache_ttl_seconds=payload_cache_ttl_seconds)
        self._config = ServerConfig(
            server_dir="unused-server-dir-placeholder", cache_config=cache
        )

    def get_config(self):
        return self._config


def _make_submit(calls: list):
    def submit():
        job_id = f"job-{len(calls)}"
        calls.append(job_id)
        return job_id

    return submit


class TestTerminalTtlTracksPayloadCacheTtl:
    def test_terminal_ttl_aligns_with_payload_cache_ttl_seconds_not_hardcoded_default(
        self,
    ):
        fake_config_service = _FakeConfigServiceForTTL(
            payload_cache_ttl_seconds=_CONFIGURED_SHORT_TTL_SECONDS
        )
        cache = TemporalDedupCache(config_service=fake_config_service)

        submit_calls: list = []
        statuses = {"job-0": "completed"}

        def status_check(job_id):
            return statuses.get(job_id)

        sig = canonical_signature({"query_text": "auth", "limit": 10})
        job_id_1 = cache.get_or_submit(sig, status_check, _make_submit(submit_calls))
        # Observe the terminal status once (starts the TTL clock). The
        # returned job_id is intentionally unused -- only the side effect
        # (recording terminal_observed_at) matters here.
        _ = cache.get_or_submit(sig, status_check, _make_submit(submit_calls))

        # Sleep past the CONFIGURED payload_cache_ttl_seconds but nowhere
        # near the hardcoded 3600s default.
        time.sleep(_SLEEP_PAST_SHORT_TTL_SECONDS)

        job_id_2 = cache.get_or_submit(sig, status_check, _make_submit(submit_calls))

        assert job_id_1 == "job-0"
        assert job_id_2 == "job-1", (
            "Bug #1547: a dedup entry must expire in line with "
            "payload_cache_ttl_seconds (read live from config_service), "
            "not the hardcoded 3600s default -- otherwise a fast-follow "
            "query can join a terminal entry whose PayloadCache snapshot "
            "has already been evicted"
        )
        assert len(submit_calls) == 2

    def test_explicit_terminal_ttl_override_still_wins_over_config_service(self):
        """Backward compatibility: an explicit terminal_ttl_seconds
        constructor argument (used throughout the existing dedup-cache test
        suite) must still be authoritative even when a config_service is
        also supplied -- mirrors MemoryGovernor's own config_service-wins-
        when-set / constructor-value-otherwise precedent, applied the other
        way for an explicit non-None override."""
        fake_config_service = _FakeConfigServiceForTTL(
            payload_cache_ttl_seconds=_CONFIGURED_TINY_TTL_SECONDS
        )
        cache = TemporalDedupCache(
            terminal_ttl_seconds=_EXPLICIT_OVERRIDE_TTL_SECONDS,
            config_service=fake_config_service,
        )

        submit_calls: list = []
        statuses = {"job-0": "completed"}

        def status_check(job_id):
            return statuses.get(job_id)

        sig = canonical_signature({"query_text": "auth", "limit": 10})
        job_id_1 = cache.get_or_submit(sig, status_check, _make_submit(submit_calls))
        _ = cache.get_or_submit(sig, status_check, _make_submit(submit_calls))

        # Past the config's tiny TTL, well under the explicit override.
        time.sleep(_SLEEP_PAST_TINY_TTL_SECONDS)

        job_id_2 = cache.get_or_submit(sig, status_check, _make_submit(submit_calls))

        assert job_id_1 == job_id_2 == "job-0", (
            "an explicit terminal_ttl_seconds override must take precedence "
            "over config_service, matching this codebase's established "
            "config_service pattern"
        )
        assert len(submit_calls) == 1
