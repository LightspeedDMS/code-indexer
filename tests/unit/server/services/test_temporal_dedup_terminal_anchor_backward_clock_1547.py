"""Bug #1547 round-2 hardening FIX 4 (RED): _anchor_terminal_observed_at
must fail SAFE on a backward wall-clock step (or a future
completed_at_epoch).

_anchor_terminal_observed_at computes
`time.monotonic() - max(0, time.time() - completed_at_epoch)`. When
completed_at_epoch is in the future (clock stepped backward between real
completion and this observation, or the job's own completed_at is simply
wrong), `time.time() - completed_at_epoch` is negative, and `max(0, ...)`
clamps the computed age to 0 -- which anchors the entry at "now" and grants
it a FRESH FULL terminal TTL, even though the entry may already be older
than its TTL. A stale/evicted PayloadCache snapshot can then be rejoined.
(A FORWARD clock step is the opposite, safe direction -- it only makes the
computed age too large, causing early expiry -- and is explicitly NOT this
fix's concern.)

Required behavior: a negative computed age must fail toward EXPIRY/
recompute, never toward granting a fresh full TTL.

Written BEFORE the fix: this test constructs a completed_at_epoch
comfortably in the FUTURE and asserts the resulting dedup entry does NOT
get rejoined on a second get_or_submit call -- against the CURRENT
(unmodified) code, the max(0, ...) clamp anchors at "now" (age=0), which
DOES let the second call rejoin, so this test must genuinely fail.
"""

import time

from code_indexer.server.services.temporal_dedup_cache import (
    TemporalDedupCache,
    canonical_signature,
)

#: The dedup cache's configured terminal TTL for this test.
_TERMINAL_TTL_SECONDS = 900.0

#: How far into the future completed_at_epoch is set -- comfortably enough
#: that no plausible clock-skew tolerance could explain it away as "not
#: really in the future".
_FUTURE_COMPLETION_OFFSET_SECONDS = 10_000.0

#: Arbitrary result-limit value used only as canonical_signature() payload
#: content -- not itself under test.
_TEST_QUERY_LIMIT = 10

#: Expected submit() call count when the second get_or_submit() call must
#: resubmit (fail-safe: does not rejoin a possibly-already-expired entry).
_EXPECTED_SUBMIT_COUNT_WHEN_RESUBMITTED = 2


def _make_submit(calls):
    def submit():
        job_id = f"job-{len(calls)}"
        calls.append(job_id)
        return job_id

    return submit


class TestBackwardClockStepAnchorsTowardExpiry:
    def test_future_completed_at_epoch_does_not_grant_fresh_ttl_and_does_not_rejoin(
        self,
    ):
        future_completed_at_epoch = time.time() + _FUTURE_COMPLETION_OFFSET_SECONDS

        def status_check(job_id):
            return "completed", future_completed_at_epoch

        submit_calls = []
        cache = TemporalDedupCache(terminal_ttl_seconds=_TERMINAL_TTL_SECONDS)
        sig = canonical_signature(
            {"query_text": "backward-clock-1547", "limit": _TEST_QUERY_LIMIT}
        )

        job_id_1 = cache.get_or_submit(sig, status_check, _make_submit(submit_calls))
        job_id_2 = cache.get_or_submit(sig, status_check, _make_submit(submit_calls))

        assert job_id_1 != job_id_2, (
            "Bug #1547 FIX 4: a completed_at_epoch in the FUTURE (backward "
            "wall-clock step, or a malformed timestamp) must anchor "
            "toward EXPIRY, never toward a fresh full terminal TTL -- got "
            f"the SAME job_id {job_id_1!r} for both calls, meaning the "
            "second call incorrectly rejoined"
        )
        assert len(submit_calls) == _EXPECTED_SUBMIT_COUNT_WHEN_RESUBMITTED
