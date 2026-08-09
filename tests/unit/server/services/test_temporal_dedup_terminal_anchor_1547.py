"""Bug #1547 Finding 3 (RED): a terminal TemporalDedupCache entry can
outlive its own PayloadCache snapshot.

terminal_observed_at was stamped when a LATER request first OBSERVED the
job as terminal, not when the job actually completed. The payload's own
TTL clock starts at the snapshot WRITE (real completion time). Codex's
confirmed case: payload TTL 900s, job completes at t=0, no duplicate
arrives until t=901 -- PayloadCache has already evicted the snapshot, but
the FIRST terminal observation (at t=901) stamps terminal_observed_at at
"now", computing elapsed=0 and incorrectly rejoining a job whose result no
longer exists.

Separately, the config-read-failure fail-safe fell back to
DEFAULT_TERMINAL_TTL_SECONDS (3600s), which is LONGER than the payload
cache's own compiled default (900s) and re-introduces this exact bug
whenever a config_service read fails.

NOTE on the RED failure mode: fixing this requires TemporalDedupCache to
learn the job's REAL completion time somehow -- there is no channel for
that today. The fix (see temporal_dedup_cache.py) extends status_check's
contract so it may optionally return a (status, completed_at_epoch) tuple
instead of a bare status string; this test exercises that NEW contract
directly. Against the CURRENT (unmodified) code, a tuple is not a member
of _TERMINAL_STATUSES at all, so get_or_submit treats it as "still active"
and unconditionally rejoins.

The two PRIMARY tests in this module
(test_late_first_observation_of_an_already_expired_terminal_job_does_not_rejoin
and test_config_read_failure_uses_conservative_fallback_not_hardcoded_3600)
must genuinely fail against the unmodified code.
test_late_first_observation_of_a_recently_completed_job_still_rejoins is a
POSITIVE CONTROL only -- it asserts "rejoin happens", which the current
(pre-fix) code already does too, coincidentally, via the tuple-not-
recognized code path. It is not RED evidence on its own; it exists solely
to prove the eventual fix does not overcorrect into forcing a resubmit on
every first observation.
"""

import time

from code_indexer.server.services.temporal_dedup_cache import (
    TemporalDedupCache,
    canonical_signature,
)

#: The dedup cache's configured terminal TTL for this test, matching
#: CacheConfig.payload_cache_ttl_seconds' own compiled default.
_TERMINAL_TTL_SECONDS = 900.0

#: How long ago (real, wall-clock seconds) the job actually completed --
#: deliberately PAST _TERMINAL_TTL_SECONDS, so a correctly-anchored dedup
#: entry must treat it as expired.
_REAL_COMPLETION_AGE_SECONDS = 901.0

#: How long ago (real, wall-clock seconds) the job completed in the
#: positive-control test -- well within _TERMINAL_TTL_SECONDS.
_RECENT_COMPLETION_AGE_SECONDS = 1.0

#: The conservative ceiling a config-read-failure fallback must never
#: exceed -- CacheConfig.payload_cache_ttl_seconds' own compiled default.
_CONSERVATIVE_FALLBACK_CEILING_SECONDS = 900.0

#: Arbitrary result-limit value used only as canonical_signature() payload
#: content -- not itself under test.
_TEST_QUERY_LIMIT = 10

#: Expected submit() call count when the second get_or_submit() call must
#: resubmit (the stale-anchor case) vs. correctly rejoin (the positive
#: control / recent-completion case).
_EXPECTED_SUBMIT_COUNT_WHEN_RESUBMITTED = 2
_EXPECTED_SUBMIT_COUNT_WHEN_REJOINED = 1


def _make_submit(calls):
    def submit():
        job_id = f"job-{len(calls)}"
        calls.append(job_id)
        return job_id

    return submit


class TestTerminalWindowAnchoredToRealCompletionTime:
    def test_late_first_observation_of_an_already_expired_terminal_job_does_not_rejoin(
        self,
    ):
        real_completed_at_epoch = time.time() - _REAL_COMPLETION_AGE_SECONDS

        def status_check(job_id):
            return "completed", real_completed_at_epoch

        submit_calls = []
        cache = TemporalDedupCache(terminal_ttl_seconds=_TERMINAL_TTL_SECONDS)
        sig = canonical_signature({"query_text": "auth", "limit": _TEST_QUERY_LIMIT})

        job_id_1 = cache.get_or_submit(sig, status_check, _make_submit(submit_calls))
        job_id_2 = cache.get_or_submit(sig, status_check, _make_submit(submit_calls))

        assert job_id_1 != job_id_2, (
            "Bug #1547 Finding 3: a terminal entry whose REAL completion "
            f"was {_REAL_COMPLETION_AGE_SECONDS:.0f}s ago (past the "
            f"{_TERMINAL_TTL_SECONDS:.0f}s terminal TTL) must not be "
            "rejoined merely because THIS was the first observation of "
            f"its terminal status -- got the SAME job_id {job_id_1!r} for "
            "both calls"
        )
        assert len(submit_calls) == _EXPECTED_SUBMIT_COUNT_WHEN_RESUBMITTED

    def test_late_first_observation_of_a_recently_completed_job_still_rejoins(
        self,
    ):
        """Positive control (not RED evidence -- see module docstring): a
        job that genuinely completed recently (well within the terminal
        TTL) must still be rejoined on its first observation, proving the
        eventual fix does not overcorrect into forcing a resubmit on
        every first observation."""
        recent_completed_at_epoch = time.time() - _RECENT_COMPLETION_AGE_SECONDS

        def status_check(job_id):
            return "completed", recent_completed_at_epoch

        submit_calls = []
        cache = TemporalDedupCache(terminal_ttl_seconds=_TERMINAL_TTL_SECONDS)
        sig = canonical_signature({"query_text": "auth", "limit": _TEST_QUERY_LIMIT})

        job_id_1 = cache.get_or_submit(sig, status_check, _make_submit(submit_calls))
        job_id_2 = cache.get_or_submit(sig, status_check, _make_submit(submit_calls))

        assert job_id_1 == job_id_2
        assert len(submit_calls) == _EXPECTED_SUBMIT_COUNT_WHEN_REJOINED


class TestConfigReadFailureFallbackIsConservative:
    def test_config_read_failure_uses_conservative_fallback_not_hardcoded_3600(
        self,
    ):
        class _RaisingConfigService:
            def get_config(self):
                raise RuntimeError("simulated config read failure")

        cache = TemporalDedupCache(config_service=_RaisingConfigService())
        effective_ttl = cache._effective_terminal_ttl_seconds()

        assert effective_ttl <= _CONSERVATIVE_FALLBACK_CEILING_SECONDS, (
            "Bug #1547 Finding 3: a config_service read failure must fall "
            "back to a CONSERVATIVE terminal TTL (<= "
            f"{_CONSERVATIVE_FALLBACK_CEILING_SECONDS:.0f}s, the payload "
            "cache's own compiled default), not "
            f"DEFAULT_TERMINAL_TTL_SECONDS=3600.0 -- got {effective_ttl}"
        )
