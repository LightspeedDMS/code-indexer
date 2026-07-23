"""
Tests for Story #1400 Phase 6: bounded same-node temporal dedup cache.

FINAL LOCKED DESIGN (adjudicated, Codex's stricter design adopted):
- Canonical signature = sha256 of json.dumps(payload, sort_keys=True,
  separators=(",",":")), with list-typed fields pre-sorted/normalized by
  the caller before signature computation (mirrors TemporalWorkerInput's
  diff_type canonicalization).
- A SINGLE global mutex (never a per-signature lock dict), held only around
  lookup -> status-decision -> submit -> publish. Never held during the
  wait loop or worker execution.
- Active (pending/running) entries are NEVER evicted by TTL or LRU.
- Terminal entries get a TTL (3600s) -- a fast-follow identical query within
  that window still joins the same (already-resolved) job_id rather than
  redoing the work; once the TTL passes, a fresh submission occurs.
- Capped at 4096 total entries. If the cap is reached while EVERY entry is
  still active, a new unique submission is rejected with
  TemporalDedupCapacityExhaustedError rather than evicting live work.

TDD: written BEFORE implementation.
"""

import time

import pytest

from code_indexer.server.services.temporal_dedup_cache import (
    TemporalDedupCache,
    TemporalDedupCapacityExhaustedError,
    canonical_signature,
    get_temporal_dedup_cache,
)

_REAL_WORLD_TTL_SECONDS = 3600.0
_SHORT_TTL_SECONDS = 0.01
_PAST_SHORT_TTL_SLEEP_SECONDS = 0.05
_SMALL_CACHE_MAX_ENTRIES = 2


def _make_submit(calls: list):
    def submit():
        job_id = f"job-{len(calls)}"
        calls.append(job_id)
        return job_id

    return submit


class TestCanonicalSignature:
    def test_deterministic_for_same_payload(self):
        payload = {
            "query_text": "auth",
            "limit": 10,
            "diff_types": ["added", "modified"],
        }
        assert canonical_signature(payload) == canonical_signature(dict(payload))

    def test_key_order_does_not_matter(self):
        p1 = {"a": 1, "b": 2}
        p2 = {"b": 2, "a": 1}
        assert canonical_signature(p1) == canonical_signature(p2)

    def test_different_payloads_produce_different_signatures(self):
        p1 = {"query_text": "auth", "limit": 10}
        p2 = {"query_text": "auth", "limit": 20}
        assert canonical_signature(p1) != canonical_signature(p2)


class TestCanonicalSignatureFormat:
    def test_returns_hex_sha256_length(self):
        sig = canonical_signature({"a": 1})
        assert len(sig) == 64
        int(sig, 16)  # must be valid hex


class TestGetOrSubmitActiveJoin:
    def test_identical_signature_joins_same_active_job(self):
        cache = TemporalDedupCache()
        submit_calls: list = []

        def status_check(job_id):
            return "running"

        sig = canonical_signature({"query_text": "auth", "limit": 10})
        job_id_1 = cache.get_or_submit(sig, status_check, _make_submit(submit_calls))
        job_id_2 = cache.get_or_submit(sig, status_check, _make_submit(submit_calls))

        assert job_id_1 == job_id_2
        assert len(submit_calls) == 1

    def test_different_signature_gets_separate_job(self):
        cache = TemporalDedupCache()
        submit_calls: list = []

        def status_check(job_id):
            return "running"

        sig1 = canonical_signature({"query_text": "auth", "limit": 10})
        sig2 = canonical_signature({"query_text": "auth", "limit": 20})
        job_id_1 = cache.get_or_submit(sig1, status_check, _make_submit(submit_calls))
        job_id_2 = cache.get_or_submit(sig2, status_check, _make_submit(submit_calls))

        assert job_id_1 != job_id_2
        assert len(submit_calls) == 2


class TestTerminalEntryJoinsWithinTtl:
    def test_terminal_entry_within_ttl_still_joins_same_job(self):
        """FINAL LOCKED DESIGN: a fast-follow identical query within the
        terminal TTL window still joins the just-finished result."""
        cache = TemporalDedupCache(terminal_ttl_seconds=_REAL_WORLD_TTL_SECONDS)
        submit_calls: list = []
        statuses = {"job-0": "completed"}

        def status_check(job_id):
            return statuses.get(job_id)

        sig = canonical_signature({"query_text": "auth", "limit": 10})
        job_id_1 = cache.get_or_submit(sig, status_check, _make_submit(submit_calls))
        job_id_2 = cache.get_or_submit(sig, status_check, _make_submit(submit_calls))

        assert job_id_1 == job_id_2 == "job-0"
        assert len(submit_calls) == 1

    def test_absent_status_treated_as_terminal(self):
        """status_check returning None (job not found/unauthorized) is
        treated as terminal -- a fresh submission occurs immediately
        (absent is not "kept briefly", only genuinely-terminal is)."""
        cache = TemporalDedupCache(terminal_ttl_seconds=_REAL_WORLD_TTL_SECONDS)
        submit_calls: list = []

        def status_check(job_id):
            return None

        sig = canonical_signature({"query_text": "auth", "limit": 10})
        job_id_1 = cache.get_or_submit(sig, status_check, _make_submit(submit_calls))
        job_id_2 = cache.get_or_submit(sig, status_check, _make_submit(submit_calls))

        assert job_id_1 == "job-0"
        assert job_id_2 == "job-1"
        assert len(submit_calls) == 2


class TestTerminalEntryExpiresAfterTtl:
    def test_terminal_entry_past_ttl_submits_fresh_job(self):
        cache = TemporalDedupCache(terminal_ttl_seconds=_SHORT_TTL_SECONDS)
        submit_calls: list = []
        statuses = {"job-0": "completed"}

        def status_check(job_id):
            return statuses.get(job_id)

        sig = canonical_signature({"query_text": "auth", "limit": 10})
        job_id_1 = cache.get_or_submit(sig, status_check, _make_submit(submit_calls))
        # The cache only starts the terminal TTL clock once it OBSERVES the
        # terminal status -- this call makes that observation (and still
        # joins, since it's within the TTL immediately after observing).
        cache.get_or_submit(sig, status_check, _make_submit(submit_calls))
        time.sleep(_PAST_SHORT_TTL_SLEEP_SECONDS)
        job_id_2 = cache.get_or_submit(sig, status_check, _make_submit(submit_calls))

        assert job_id_1 == "job-0"
        assert job_id_2 == "job-1"
        assert len(submit_calls) == 2


class TestCapacityExhaustion:
    def test_active_entries_never_evicted_even_at_capacity(self):
        cache = TemporalDedupCache(max_entries=_SMALL_CACHE_MAX_ENTRIES)
        submit_calls: list = []

        def status_check(job_id):
            return "running"  # always active -- never terminal

        sig1 = canonical_signature({"limit": 1})
        sig2 = canonical_signature({"limit": 2})
        sig3 = canonical_signature({"limit": 3})

        cache.get_or_submit(sig1, status_check, _make_submit(submit_calls))
        cache.get_or_submit(sig2, status_check, _make_submit(submit_calls))

        with pytest.raises(TemporalDedupCapacityExhaustedError):
            cache.get_or_submit(sig3, status_check, _make_submit(submit_calls))

    def test_terminal_entries_evicted_to_make_room(self):
        cache = TemporalDedupCache(
            max_entries=_SMALL_CACHE_MAX_ENTRIES,
            terminal_ttl_seconds=_SHORT_TTL_SECONDS,
        )
        submit_calls: list = []
        statuses = {"job-0": "completed", "job-1": "running"}

        def status_check(job_id):
            return statuses.get(job_id)

        sig1 = canonical_signature({"limit": 1})
        sig2 = canonical_signature({"limit": 2})
        sig3 = canonical_signature({"limit": 3})

        cache.get_or_submit(sig1, status_check, _make_submit(submit_calls))  # terminal
        cache.get_or_submit(sig2, status_check, _make_submit(submit_calls))  # active
        # Observe sig1 as terminal now, starting its TTL clock.
        cache.get_or_submit(sig1, status_check, _make_submit(submit_calls))
        time.sleep(_PAST_SHORT_TTL_SLEEP_SECONDS)  # past sig1's terminal TTL

        # sig1's expired terminal entry should be evicted to make room for sig3.
        job_id_3 = cache.get_or_submit(sig3, status_check, _make_submit(submit_calls))
        assert job_id_3 == "job-2"


class TestGetTemporalDedupCacheSingleton:
    def test_returns_same_instance_on_repeated_calls(self):
        first = get_temporal_dedup_cache()
        second = get_temporal_dedup_cache()
        assert first is second


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
