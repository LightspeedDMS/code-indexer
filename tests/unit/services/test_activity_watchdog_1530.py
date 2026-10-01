"""Tests for the parent-side ActivityWatchdog primitives (Issue #1530,
Priority 2 parent-side half): reading a child's heartbeat file and
evaluating whether it indicates a genuine wedge.

Bug #1218 invariant under test throughout: staleness is "zero forward
progress for N seconds", NEVER "total elapsed time" -- a job that has been
running (and ticking) for hours must never trip the watchdog merely because
it is old.
"""

import os
import subprocess
import sys
import time

from code_indexer.services.activity_watchdog import (
    evaluate_staleness,
    read_heartbeat_snapshot,
    terminate_process_group,
)


class TestReadHeartbeatSnapshot:
    def test_returns_none_for_missing_file(self, tmp_path) -> None:
        missing_path = str(tmp_path / "does_not_exist.json")
        assert not os.path.exists(missing_path)
        assert read_heartbeat_snapshot(missing_path) is None

    def test_returns_none_for_corrupt_json(self, tmp_path) -> None:
        corrupt_path = tmp_path / "corrupt.json"
        corrupt_path.write_text("{ not valid json ][")
        assert read_heartbeat_snapshot(str(corrupt_path)) is None

    def test_returns_parsed_dict_for_valid_file(self, tmp_path) -> None:
        import json

        valid_path = tmp_path / "valid.json"
        payload = {"pid": 4242, "oldest_in_flight_age_seconds": 1.5}
        valid_path.write_text(json.dumps(payload))

        result = read_heartbeat_snapshot(str(valid_path))
        assert result == payload

    def test_returns_none_for_valid_json_that_is_not_an_object(self, tmp_path) -> None:
        list_path = tmp_path / "a_list_not_an_object.json"
        list_path.write_text("[1, 2, 3]")
        assert read_heartbeat_snapshot(str(list_path)) is None


# Constants for evaluate_staleness tests.
_THRESHOLD = 90.0

# Constants for TestTerminateProcessGroupKillsRealSubprocess.
_SLEEP_SUBPROCESS_DURATION_SECONDS = 30
_TERMINATE_MAX_ELAPSED_SECONDS = 10.0
_CLEANUP_KILL_TIMEOUT_SECONDS = 5.0


class TestEvaluateStalenessAbsenceSignal:
    """Signal 1: the heartbeat file never appeared (snapshot is None)."""

    def test_absent_snapshot_within_grace_period_is_healthy(self) -> None:
        verdict = evaluate_staleness(
            None,
            threshold_seconds=_THRESHOLD,
            elapsed_since_spawn_seconds=_THRESHOLD - 10.0,
        )
        assert verdict is None

    def test_absent_snapshot_past_grace_period_is_stale(self) -> None:
        verdict = evaluate_staleness(
            None,
            threshold_seconds=_THRESHOLD,
            elapsed_since_spawn_seconds=_THRESHOLD + 10.0,
        )
        assert verdict is not None
        assert verdict.reason == "heartbeat_file_never_appeared_or_unreadable"


class TestEvaluateStalenessInFlightSignal:
    """Signal 2: the beacon's own reported oldest-in-flight age."""

    def test_reported_age_under_threshold_is_healthy(self) -> None:
        snapshot = {
            "pid": 111,
            "oldest_in_flight_age_seconds": _THRESHOLD - 1.0,
            "in_flight": [{"label": "chunking", "age_seconds": _THRESHOLD - 1.0}],
        }
        verdict = evaluate_staleness(
            snapshot,
            threshold_seconds=_THRESHOLD,
            elapsed_since_spawn_seconds=_THRESHOLD - 1.0,
        )
        assert verdict is None

    def test_reported_age_over_threshold_is_stale_with_correct_label(self) -> None:
        snapshot = {
            "pid": 222,
            "oldest_in_flight_age_seconds": _THRESHOLD + 5.0,
            "in_flight": [
                {"label": "healthy_op", "age_seconds": 0.5},
                {"label": "wedged_tantivy_write", "age_seconds": _THRESHOLD + 5.0},
            ],
        }
        verdict = evaluate_staleness(
            snapshot,
            threshold_seconds=_THRESHOLD,
            elapsed_since_spawn_seconds=_THRESHOLD + 5.0,
        )
        assert verdict is not None
        assert verdict.reason == "stale_in_flight_operation"
        assert verdict.label == "wedged_tantivy_write"
        assert verdict.pid == 222

    def test_bug_1218_large_elapsed_time_with_small_reported_age_is_healthy(
        self,
    ) -> None:
        """The Bug #1218 invariant, directly: a job that has been running
        (elapsed_since_spawn) for a VERY long time must still read healthy
        as long as the reported in-flight age is small -- total elapsed
        time must never itself be the trigger.
        """
        ten_hours_seconds = 10 * 60 * 60
        snapshot = {
            "pid": 333,
            "oldest_in_flight_age_seconds": 2.0,  # ticking every couple seconds
            "in_flight": [{"label": "chunking", "age_seconds": 2.0}],
        }
        verdict = evaluate_staleness(
            snapshot,
            threshold_seconds=_THRESHOLD,
            elapsed_since_spawn_seconds=float(ten_hours_seconds),
        )
        assert verdict is None, (
            "a job running for 10 hours but ticking every 2s must never "
            "be flagged stale -- Bug #1218 invariant"
        )


class TestEvaluateStalenessMtimeSignal:
    """Signal 3: the heartbeat FILE's own mtime age -- defense-in-depth
    against a fully frozen writer thread that stops updating the file even
    though its last-written content still looks healthy.
    """

    def test_stale_mtime_triggers_even_with_healthy_reported_age(self) -> None:
        snapshot = {
            "pid": 444,
            "oldest_in_flight_age_seconds": 0.1,  # last write looked healthy
            "in_flight": [{"label": "chunking", "age_seconds": 0.1}],
        }
        verdict = evaluate_staleness(
            snapshot,
            threshold_seconds=_THRESHOLD,
            elapsed_since_spawn_seconds=_THRESHOLD + 5.0,
            heartbeat_mtime_age_seconds=_THRESHOLD + 5.0,  # file stopped updating
        )
        assert verdict is not None
        assert verdict.reason == "heartbeat_file_not_updating"
        assert verdict.pid == 444

    def test_healthy_mtime_with_healthy_reported_age_is_healthy(self) -> None:
        snapshot = {
            "pid": 555,
            "oldest_in_flight_age_seconds": 0.1,
            "in_flight": [{"label": "chunking", "age_seconds": 0.1}],
        }
        verdict = evaluate_staleness(
            snapshot,
            threshold_seconds=_THRESHOLD,
            elapsed_since_spawn_seconds=_THRESHOLD + 5.0,
            heartbeat_mtime_age_seconds=1.0,
        )
        assert verdict is None


class TestMalformedInFlightToleratedGracefully:
    """`evaluate_staleness` must never raise, even on a corrupt/malformed
    `in_flight` payload -- it degrades to a None label, not a crash.
    """

    def test_non_list_in_flight_does_not_raise(self) -> None:
        snapshot = {
            "pid": 666,
            "oldest_in_flight_age_seconds": _THRESHOLD + 1.0,
            "in_flight": "not-a-list",
        }
        verdict = evaluate_staleness(
            snapshot, threshold_seconds=_THRESHOLD, elapsed_since_spawn_seconds=0.0
        )
        assert verdict is not None
        assert verdict.label is None

    def test_malformed_entries_are_skipped_not_fatal(self) -> None:
        snapshot = {
            "pid": 777,
            "oldest_in_flight_age_seconds": _THRESHOLD + 1.0,
            "in_flight": [
                "not-a-dict",
                {"label": "no_age_field"},
                {"label": "bad_age_type", "age_seconds": "not-a-number"},
                {"label": "the_real_one", "age_seconds": _THRESHOLD + 1.0},
            ],
        }
        verdict = evaluate_staleness(
            snapshot, threshold_seconds=_THRESHOLD, elapsed_since_spawn_seconds=0.0
        )
        assert verdict is not None
        assert verdict.label == "the_real_one"

    def test_smaller_age_entry_after_larger_one_is_not_selected(self) -> None:
        snapshot = {
            "pid": 888,
            "oldest_in_flight_age_seconds": _THRESHOLD + 1.0,
            "in_flight": [
                {"label": "the_actual_worst", "age_seconds": _THRESHOLD + 1.0},
                {"label": "a_smaller_one", "age_seconds": 0.5},
            ],
        }
        verdict = evaluate_staleness(
            snapshot, threshold_seconds=_THRESHOLD, elapsed_since_spawn_seconds=0.0
        )
        assert verdict is not None
        assert verdict.label == "the_actual_worst"


class TestTerminateProcessGroupKillsRealSubprocess:
    """`terminate_process_group` is a real, race-safe SIGTERM->grace->
    SIGKILL sequence -- proven here against a REAL, genuinely long-running
    subprocess (never mocked), confirming it actually dies well before its
    own sleep duration would have elapsed naturally.
    """

    def test_terminates_a_real_long_running_subprocess(self) -> None:
        proc = subprocess.Popen(
            ["sleep", str(_SLEEP_SUBPROCESS_DURATION_SECONDS)],
            start_new_session=True,
        )
        try:
            assert proc.poll() is None, "process should still be running"

            start = time.monotonic()
            terminate_process_group(proc)
            elapsed = time.monotonic() - start

            assert proc.returncode is not None, "process must be reaped"
            assert elapsed < _TERMINATE_MAX_ELAPSED_SECONDS, (
                f"terminate_process_group took {elapsed}s -- should be "
                f"well under the full {_SLEEP_SUBPROCESS_DURATION_SECONDS}s "
                f"sleep duration"
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=_CLEANUP_KILL_TIMEOUT_SECONDS)


# Constants for the remaining real-subprocess/real-filesystem tests below.
_QUICK_EXIT_WAIT_TIMEOUT_SECONDS = 5.0
_SIGTERM_IGNORING_SLEEP_SECONDS = 30
_ESCALATION_MAX_ELAPSED_SECONDS = 15.0
_COMPOSITE_HEALTHY_THRESHOLD_SECONDS = 90.0
_COMPOSITE_STALE_THRESHOLD_SECONDS = 0.01
_COMPOSITE_STALE_WAIT_SECONDS = 0.2


class TestTerminateProcessGroupAlreadyExited:
    """A process that has already exited and been reaped before
    terminate_process_group is called must be handled gracefully (the
    `os.getpgid` ProcessLookupError branch) -- real subprocess, no mock.
    """

    def test_terminate_on_already_reaped_process_does_not_raise(self) -> None:
        proc = subprocess.Popen(["true"], start_new_session=True)
        proc.wait(timeout=_QUICK_EXIT_WAIT_TIMEOUT_SECONDS)
        assert proc.returncode is not None

        # Must not raise, even though the process (and its process group)
        # are already gone.
        terminate_process_group(proc)


class TestTerminateProcessGroupEscalatesToSigkill:
    """A subprocess that ignores SIGTERM must still be killed -- via the
    real SIGKILL escalation path after the grace period. Real subprocess,
    never mocked.
    """

    def test_sigterm_ignoring_process_is_escalated_to_sigkill(self) -> None:
        # A pure-Python child that installs SIG_IGN for SIGTERM on ITSELF
        # (a single process, no shell/child hierarchy) so killpg's SIGTERM
        # genuinely cannot terminate it -- only SIGKILL (unignorable) can.
        # A `bash -c "trap '' TERM; sleep N"` approach was tried first and
        # rejected: sleep runs as bash's child in the SAME process group,
        # and sleep does NOT ignore SIGTERM by default, so killpg's SIGTERM
        # would have terminated sleep (and let bash exit) without ever
        # reaching the SIGKILL escalation path this test exists to prove.
        #
        # A first version of THIS test raced: it sent SIGTERM immediately
        # after Popen, before the child had necessarily finished executing
        # `signal.signal(...)` -- so plain SIGTERM sometimes still killed
        # it, and coverage evidence confirmed the escalation branch was
        # never actually exercised. Fixed by having the child print
        # "ready" to stdout immediately AFTER installing the handler, and
        # blocking on that line here before calling terminate_process_group
        # -- a real synchronization point, not a timing assumption.
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print('ready', flush=True); "
                f"time.sleep({_SIGTERM_IGNORING_SLEEP_SECONDS})",
            ],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            assert proc.poll() is None
            assert proc.stdout is not None
            ready_line = proc.stdout.readline()
            assert ready_line.strip() == "ready", (
                f"child did not signal readiness as expected: {ready_line!r}"
            )

            start = time.monotonic()
            terminate_process_group(proc)
            elapsed = time.monotonic() - start

            assert proc.returncode is not None
            assert elapsed < _ESCALATION_MAX_ELAPSED_SECONDS, (
                f"escalation to SIGKILL took {elapsed}s -- should complete "
                f"within one grace period plus overhead"
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=_CLEANUP_KILL_TIMEOUT_SECONDS)


class TestReadHeartbeatMtimeAge:
    def test_returns_small_age_for_a_freshly_written_file(self, tmp_path) -> None:
        from code_indexer.services.activity_watchdog import _read_heartbeat_mtime_age

        fresh_path = tmp_path / "fresh.json"
        fresh_path.write_text("{}")

        age = _read_heartbeat_mtime_age(str(fresh_path))
        assert age is not None
        assert 0.0 <= age < _QUICK_EXIT_WAIT_TIMEOUT_SECONDS

    def test_returns_none_for_a_missing_file(self, tmp_path) -> None:
        from code_indexer.services.activity_watchdog import _read_heartbeat_mtime_age

        missing_path = tmp_path / "missing.json"
        assert _read_heartbeat_mtime_age(str(missing_path)) is None


class TestCheckAndTerminateIfStaleComposite:
    """End-to-end (real subprocess + real filesystem) proof of the full
    read+evaluate+terminate pipeline `run_with_popen_progress` will call.
    """

    def test_healthy_snapshot_is_never_killed(self, tmp_path) -> None:
        import json

        from code_indexer.services.activity_watchdog import (
            check_and_terminate_if_stale,
        )

        heartbeat_path = tmp_path / "healthy_heartbeat.json"
        heartbeat_path.write_text(
            json.dumps({"pid": os.getpid(), "oldest_in_flight_age_seconds": None})
        )

        proc = subprocess.Popen(["sleep", str(_SLEEP_SUBPROCESS_DURATION_SECONDS)])
        try:
            verdict = check_and_terminate_if_stale(
                proc,
                str(heartbeat_path),
                threshold_seconds=_COMPOSITE_HEALTHY_THRESHOLD_SECONDS,
                spawn_monotonic=time.monotonic(),
            )
            assert verdict is None
            assert proc.poll() is None, "a healthy subprocess must not be killed"
        finally:
            proc.kill()
            proc.wait(timeout=_CLEANUP_KILL_TIMEOUT_SECONDS)

    def test_already_exited_process_is_never_reported_as_wedged(self, tmp_path) -> None:
        """A child that exited on its own is by definition not wedged.

        The staleness check runs after `process.poll()` returned None in
        the caller's loop, but reading the heartbeat file and stat'ing it
        take time -- a child that finishes inside that window would
        otherwise be reported as watchdog-killed, hiding its real exit
        code behind a bogus "no forward progress" failure. Simulated
        deterministically here by evaluating an already-reaped REAL
        process.
        """
        from code_indexer.services.activity_watchdog import (
            check_and_terminate_if_stale,
        )

        never_created_path = tmp_path / "never_created.json"
        proc = subprocess.Popen(["true"], start_new_session=True)
        proc.wait(timeout=_QUICK_EXIT_WAIT_TIMEOUT_SECONDS)
        assert proc.returncode is not None

        spawn_monotonic = time.monotonic()
        time.sleep(_COMPOSITE_STALE_WAIT_SECONDS)  # well past the tiny threshold

        verdict = check_and_terminate_if_stale(
            proc,
            str(never_created_path),
            threshold_seconds=_COMPOSITE_STALE_THRESHOLD_SECONDS,
            spawn_monotonic=spawn_monotonic,
        )
        assert verdict is None, (
            "an already-exited process must never produce a staleness "
            f"verdict, got {verdict}"
        )

    def test_stale_absent_heartbeat_kills_the_real_subprocess(self, tmp_path) -> None:
        from code_indexer.services.activity_watchdog import (
            check_and_terminate_if_stale,
        )

        never_created_path = tmp_path / "never_created.json"
        proc = subprocess.Popen(
            ["sleep", str(_SLEEP_SUBPROCESS_DURATION_SECONDS)],
            start_new_session=True,
        )
        try:
            spawn_monotonic = time.monotonic()
            time.sleep(_COMPOSITE_STALE_WAIT_SECONDS)  # exceed the tiny threshold

            verdict = check_and_terminate_if_stale(
                proc,
                str(never_created_path),
                threshold_seconds=_COMPOSITE_STALE_THRESHOLD_SECONDS,
                spawn_monotonic=spawn_monotonic,
            )
            assert verdict is not None
            assert verdict.reason == "heartbeat_file_never_appeared_or_unreadable"
            assert proc.returncode is not None, "the stale subprocess must be reaped"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=_CLEANUP_KILL_TIMEOUT_SECONDS)
