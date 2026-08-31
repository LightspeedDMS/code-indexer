"""Tests for ActivityHeartbeatWriter (Issue #1530, Priority 2 child-side half).

The child (a `cidx index --progress-json` subprocess) uses this writer to
periodically serialize its ActivityBeacon's in-flight state to a node-local
file the parent can read. Real threads, real filesystem, no mocking.
"""

import json
import os
import threading
import time

import pytest

from code_indexer.services.activity_beacon import ActivityBeacon
from code_indexer.services.activity_heartbeat_writer import ActivityHeartbeatWriter


class TestBootstrapRecordWrittenImmediately:
    """The heartbeat file must exist with a bootstrap record BEFORE any
    real indexing work begins -- the original production hang was observed
    at "semantic: starting...", before a single tick could have fired.
    """

    def test_start_writes_bootstrap_record_before_any_tick(self, tmp_path) -> None:
        heartbeat_path = str(tmp_path / "heartbeat.json")
        beacon = ActivityBeacon()  # nothing has ticked yet
        writer = ActivityHeartbeatWriter(beacon=beacon, heartbeat_path=heartbeat_path)

        writer.start()
        try:
            assert os.path.exists(heartbeat_path), (
                "bootstrap record must exist immediately after start(), "
                "before any tick has occurred"
            )
            with open(heartbeat_path) as f:
                payload = json.load(f)

            assert payload["pid"] == os.getpid()
            assert payload["bootstrap"] is True
            assert payload["oldest_in_flight_age_seconds"] is None
            assert payload["in_flight_count"] == 0
        finally:
            writer.stop()


# Constants for TestPeriodicUpdatesReflectLiveActivity.
_PERIODIC_WRITE_INTERVAL = 0.05
_PERIODIC_POLL_TIMEOUT = 5.0
_PERIODIC_POLL_SLEEP = 0.02
_PERIODIC_POST_STOP_WAIT = 0.3


def _read_json(path: str) -> dict:
    with open(path) as f:
        payload = json.load(f)
    assert isinstance(payload, dict), (
        f"expected a JSON object in {path}, got {payload!r}"
    )
    return payload


def _wait_until(predicate, timeout: float, poll_sleep: float) -> bool:
    """Bounded poll: returns True as soon as predicate() is truthy, False
    if the timeout elapses first. Never an unbounded loop.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_sleep)
    return False


class TestPeriodicUpdatesReflectLiveActivity:
    """The background thread must actually re-write the file periodically
    (reflecting a live, growing tick age), and `stop()` must actually halt
    it -- proven by observing the file stop changing afterward.
    """

    def test_file_content_updates_to_reflect_growing_tick_age_and_stops_after_stop(
        self, tmp_path
    ) -> None:
        heartbeat_path = str(tmp_path / "heartbeat.json")
        beacon = ActivityBeacon()
        writer = ActivityHeartbeatWriter(
            beacon=beacon,
            heartbeat_path=heartbeat_path,
            write_interval_seconds=_PERIODIC_WRITE_INTERVAL,
        )
        writer.start()
        try:
            # Defensive: start() writes the bootstrap record synchronously
            # before returning, but poll (bounded) rather than assume, in
            # case that guarantee is ever weakened.
            assert _wait_until(
                lambda: os.path.exists(heartbeat_path),
                timeout=_PERIODIC_POLL_TIMEOUT,
                poll_sleep=_PERIODIC_POLL_SLEEP,
            ), "bootstrap heartbeat file never appeared"

            entered = threading.Event()
            release = threading.Event()

            def ticking_worker() -> None:
                with beacon.tick("periodic_test_operation"):
                    entered.set()
                    release.wait(timeout=_PERIODIC_POLL_TIMEOUT)

            worker_thread = threading.Thread(target=ticking_worker, daemon=True)
            worker_thread.start()
            try:
                assert entered.wait(timeout=_PERIODIC_POLL_TIMEOUT)

                initial_age = _read_json(heartbeat_path)["oldest_in_flight_age_seconds"]

                def _age_grew() -> bool:
                    current = _read_json(heartbeat_path)["oldest_in_flight_age_seconds"]
                    return current is not None and current > (initial_age or 0.0)

                grew = _wait_until(
                    _age_grew,
                    timeout=_PERIODIC_POLL_TIMEOUT,
                    poll_sleep=_PERIODIC_POLL_SLEEP,
                )
                assert grew, (
                    "heartbeat file must be periodically re-written with a growing age"
                )
            finally:
                release.set()
                worker_thread.join(timeout=_PERIODIC_POLL_TIMEOUT)
        finally:
            writer.stop()

        content_after_stop = _read_json(heartbeat_path)
        time.sleep(_PERIODIC_POST_STOP_WAIT)
        content_later = _read_json(heartbeat_path)
        assert content_after_stop == content_later, (
            "the heartbeat file must not change after stop() -- the "
            "background thread must have actually halted"
        )


class TestConstructorValidation:
    """Every caller-supplied constructor argument must be validated up
    front -- a bad value here would otherwise surface much later as a
    confusing failure deep inside a background thread.
    """

    def test_rejects_none_beacon(self, tmp_path) -> None:
        import pytest

        with pytest.raises(ValueError):
            ActivityHeartbeatWriter(
                beacon=None, heartbeat_path=str(tmp_path / "h.json")
            )

    def test_rejects_relative_heartbeat_path(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            ActivityHeartbeatWriter(
                beacon=ActivityBeacon(), heartbeat_path="relative.json"
            )

    def test_rejects_non_string_heartbeat_path(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            ActivityHeartbeatWriter(
                beacon=ActivityBeacon(),
                heartbeat_path=12345,  # type: ignore[arg-type]
            )


class TestWriteIntervalValidation:
    """write_interval_seconds must be a finite, positive number -- covers
    zero, negative, NaN, infinity, and the bool-is-not-a-number trap.
    """

    def test_rejects_every_invalid_write_interval_value(self, tmp_path) -> None:
        import pytest

        heartbeat_path = str(tmp_path / "h.json")
        for bad_value in (0, -1.0, float("nan"), float("inf"), True):
            with pytest.raises(ValueError):
                ActivityHeartbeatWriter(
                    beacon=ActivityBeacon(),
                    heartbeat_path=heartbeat_path,
                    write_interval_seconds=bad_value,  # type: ignore[arg-type]
                )


class TestAtomicWriteErrorPathLogsAndReraises:
    """The atomic-write helper's error path (log + best-effort temp-file
    cleanup + re-raise) must fire on a genuine write failure -- exercised
    here via a REAL filesystem condition (target_path is an existing
    directory, so `os.replace` genuinely raises `OSError`), never a mock.
    """

    def test_replace_onto_existing_directory_logs_and_reraises(
        self, tmp_path, caplog
    ) -> None:
        import logging

        from code_indexer.services.activity_heartbeat_writer import (
            _atomic_write_json_file,
        )

        target_dir_as_path = tmp_path / "this_is_a_real_directory"
        target_dir_as_path.mkdir()

        with caplog.at_level(logging.WARNING):
            with pytest.raises(OSError):
                _atomic_write_json_file(str(target_dir_as_path), {"x": 1})

        assert any(
            "failed to write heartbeat file" in record.message
            for record in caplog.records
        )


_TRANSIENT_FAULT_WINDOW_SECONDS = 0.3
_TRANSIENT_WRITE_INTERVAL = 0.05


class TestPeriodicWriterSurvivesTransientWriteFailure:
    """A transient write failure must NOT kill the background writer.

    If the thread dies on the first OSError, the heartbeat file simply
    stops being updated -- and the parent watchdog's file-mtime signal
    then reads that as a wedged process and kills a perfectly healthy,
    possibly multi-hour indexing job. One disk/permission hiccup must
    never have that consequence.

    Real fault injection: the heartbeat directory is made non-writable so
    `mkstemp` genuinely fails for several write cycles, then restored.
    """

    @pytest.mark.skipif(
        os.geteuid() == 0, reason="root bypasses directory write permissions"
    )
    def test_writer_recovers_after_directory_becomes_writable_again(
        self, tmp_path
    ) -> None:
        heartbeat_dir = tmp_path / "hb"
        heartbeat_dir.mkdir()
        heartbeat_path = str(heartbeat_dir / "heartbeat.json")
        writer = ActivityHeartbeatWriter(
            beacon=ActivityBeacon(),
            heartbeat_path=heartbeat_path,
            write_interval_seconds=_TRANSIENT_WRITE_INTERVAL,
        )
        writer.start()
        try:
            assert os.path.exists(heartbeat_path)

            os.chmod(heartbeat_dir, 0o500)  # r-x: temp-file creation fails
            time.sleep(_TRANSIENT_FAULT_WINDOW_SECONDS)
            os.chmod(heartbeat_dir, 0o700)

            mtime_before = os.path.getmtime(heartbeat_path)
            recovered = _wait_until(
                lambda: os.path.getmtime(heartbeat_path) > mtime_before,
                timeout=_PERIODIC_POLL_TIMEOUT,
                poll_sleep=_PERIODIC_POLL_SLEEP,
            )
            assert recovered, (
                "the heartbeat file stopped updating after a transient write "
                "failure -- the background writer thread died instead of "
                "retrying on the next interval"
            )
        finally:
            os.chmod(heartbeat_dir, 0o700)
            writer.stop()


class TestStopWarnsWhenThreadOutlivesJoinTimeout:
    """stop()'s bounded join must be followed by an explicit is_alive()
    check, and a WARNING must be logged if the thread is still running --
    exercised via a REAL, deliberately slow standalone thread substituted
    for the writer's actual background thread (whitebox on private state,
    but genuine threading throughout, not a mock of thread behavior).
    """

    def test_stop_logs_warning_when_background_thread_outlives_join(
        self, tmp_path, caplog
    ) -> None:
        import logging

        heartbeat_path = str(tmp_path / "h.json")
        writer = ActivityHeartbeatWriter(
            beacon=ActivityBeacon(),
            heartbeat_path=heartbeat_path,
            write_interval_seconds=0.01,
        )
        writer.start()

        never_finishes = threading.Event()
        blocking_thread = threading.Thread(
            target=lambda: never_finishes.wait(timeout=_PERIODIC_POLL_TIMEOUT),
            daemon=True,
        )
        blocking_thread.start()
        # Whitebox substitution: stand in a real, genuinely slow thread for
        # the actual background writer thread, so stop()'s bounded join
        # (write_interval_seconds + grace) genuinely expires before this
        # thread finishes -- without needing to force a real wedged
        # filesystem write to achieve the same timing.
        writer._thread = blocking_thread
        try:
            with caplog.at_level(logging.WARNING):
                writer.stop()
            assert any(
                "did not terminate" in record.message for record in caplog.records
            )
        finally:
            never_finishes.set()
            blocking_thread.join(timeout=_PERIODIC_POLL_TIMEOUT)
