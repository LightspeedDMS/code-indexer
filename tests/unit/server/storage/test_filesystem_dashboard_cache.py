"""
Unit tests for FilesystemDashboardCacheBackend (Story #1035).

Interface parity with DependencyMapDashboardCacheBackend (SQLite) at
src/code_indexer/server/storage/sqlite_backends.py:6011-6303.

TDD RED PHASE: Tests written before production code.
"""

import json
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.server.storage.filesystem_backends import (
    FilesystemDashboardCacheBackend,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    """Return a temp directory representing the dep-map cache directory."""
    d = tmp_path / "dep-map"
    d.mkdir()
    return d


@pytest.fixture
def backend(cache_dir: Path) -> FilesystemDashboardCacheBackend:
    """Return a FilesystemDashboardCacheBackend backed by tmp dir."""
    return FilesystemDashboardCacheBackend(cache_dir=cache_dir)


# ---------------------------------------------------------------------------
# is_fresh
# ---------------------------------------------------------------------------


def test_is_fresh_returns_false_when_no_cache(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """is_fresh returns False when no cache file exists."""
    assert backend.is_fresh(600) is False


def test_is_fresh_returns_true_within_ttl(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """is_fresh returns True when computed_at is within TTL."""
    backend.set_result('{"data": "test"}')
    assert backend.is_fresh(600) is True


def test_is_fresh_returns_false_when_expired(
    cache_dir: Path,
) -> None:
    """is_fresh returns False when computed_at is older than TTL."""
    old_computed_at = (datetime.now(timezone.utc) - timedelta(seconds=700)).isoformat()
    payload = {
        "computed_at": old_computed_at,
        "job_id": None,
        "result_json": '{"data": "old"}',
        "last_failure_message": None,
        "last_failure_at": None,
    }
    cache_file = cache_dir / "_dashboard_cache.json"
    cache_file.write_text(json.dumps(payload))

    backend = FilesystemDashboardCacheBackend(cache_dir=cache_dir)
    assert backend.is_fresh(600) is False


def test_is_fresh_raises_on_negative_ttl(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """is_fresh raises ValueError on negative TTL (parity with SQLite backend)."""
    with pytest.raises(ValueError):
        backend.is_fresh(-1)


# ---------------------------------------------------------------------------
# get_cached / set_result round-trip
# ---------------------------------------------------------------------------


def test_get_cached_returns_none_when_no_file(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """get_cached returns None when no cache file exists."""
    assert backend.get_cached() is None


def test_set_result_then_get_cached_roundtrip(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """set_result followed by get_cached returns the stored result."""
    result_json = '{"nodes": [], "edges": []}'
    backend.set_result(result_json)

    cached = backend.get_cached()
    assert cached is not None
    assert cached["result_json"] == result_json
    assert cached["computed_at"] is not None
    assert cached["job_id"] is None
    assert cached["last_failure_message"] is None
    assert cached["last_failure_at"] is None


def test_set_result_with_explicit_computed_at(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """set_result accepts an explicit computed_at datetime."""
    fixed_dt = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    backend.set_result('{"data": "x"}', computed_at=fixed_dt)

    cached = backend.get_cached()
    assert cached is not None
    parsed = datetime.fromisoformat(cached["computed_at"])
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    assert abs((parsed - fixed_dt).total_seconds()) < 1


def test_set_result_overwrites_previous(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """Second set_result overwrites first."""
    backend.set_result('{"v": 1}')
    backend.set_result('{"v": 2}')

    cached = backend.get_cached()
    assert cached is not None
    assert cached["result_json"] == '{"v": 2}'


# ---------------------------------------------------------------------------
# claim_job_slot
# ---------------------------------------------------------------------------


def test_claim_job_slot_returns_none_when_slot_empty(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """claim_job_slot returns None on success (slot was empty)."""
    result = backend.claim_job_slot("job-001")
    assert result is None


def test_claim_job_slot_returns_existing_id_when_taken(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """claim_job_slot returns existing job_id when slot already occupied."""
    backend.claim_job_slot("job-001")
    result = backend.claim_job_slot("job-002")
    assert result == "job-001"


def test_claim_job_slot_persists_job_id(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """After successful claim, get_cached shows the claimed job_id."""
    backend.claim_job_slot("job-xyz")
    cached = backend.get_cached()
    assert cached is not None
    assert cached["job_id"] == "job-xyz"


def test_claim_job_slot_after_set_result_returns_none(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """claim_job_slot on a cache with no job_id (after set_result) returns None."""
    backend.set_result('{"data": "existing"}')
    result = backend.claim_job_slot("job-new")
    assert result is None


# ---------------------------------------------------------------------------
# clear_job_slot_for_retry
# ---------------------------------------------------------------------------


def test_clear_job_slot_for_retry_clears_job_id(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """clear_job_slot_for_retry sets job_id to None."""
    backend.claim_job_slot("job-001")
    backend.clear_job_slot_for_retry()

    cached = backend.get_cached()
    assert cached is not None
    assert cached["job_id"] is None


def test_clear_job_slot_for_retry_preserves_result(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """clear_job_slot_for_retry preserves result_json and computed_at."""
    backend.set_result('{"nodes": []}')
    backend.claim_job_slot("job-001")
    backend.clear_job_slot_for_retry()

    cached = backend.get_cached()
    assert cached is not None
    assert cached["result_json"] == '{"nodes": []}'
    assert cached["computed_at"] is not None
    assert cached["job_id"] is None


def test_clear_job_slot_for_retry_clears_failure_fields(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """clear_job_slot_for_retry clears last_failure_message and last_failure_at."""
    # Manually inject a failure state
    payload = {
        "computed_at": None,
        "job_id": "job-001",
        "result_json": None,
        "last_failure_message": "some error",
        "last_failure_at": datetime.now(timezone.utc).isoformat(),
    }
    (backend._cache_file).write_text(json.dumps(payload))

    backend.clear_job_slot_for_retry()

    cached = backend.get_cached()
    assert cached is not None
    assert cached["job_id"] is None
    assert cached["last_failure_message"] is None
    assert cached["last_failure_at"] is None


def test_clear_job_slot_for_retry_no_op_when_no_cache(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """clear_job_slot_for_retry is a no-op when no cache file exists (does not raise)."""
    # Should not raise
    backend.clear_job_slot_for_retry()


# ---------------------------------------------------------------------------
# get_running_job_id
# ---------------------------------------------------------------------------


def test_get_running_job_id_returns_none_when_no_cache(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """get_running_job_id returns None when no cache exists."""
    assert backend.get_running_job_id() is None


def test_get_running_job_id_returns_none_when_no_job_id(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """get_running_job_id returns None when cache has no job_id."""
    backend.set_result('{"data": "x"}')
    assert backend.get_running_job_id() is None


def test_get_running_job_id_returns_job_id_without_tracker(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """get_running_job_id returns job_id when no tracker supplied."""
    backend.claim_job_slot("job-001")
    assert backend.get_running_job_id() == "job-001"


def test_get_running_job_id_with_active_tracker(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """get_running_job_id returns job_id when tracker confirms job is running."""
    backend.claim_job_slot("job-001")

    mock_tracker = MagicMock()
    mock_job = MagicMock()
    mock_job.status = "running"
    mock_tracker.get_job.return_value = mock_job

    assert backend.get_running_job_id(job_tracker=mock_tracker) == "job-001"


def test_get_running_job_id_returns_none_for_zombie_job(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """get_running_job_id returns None when tracker says job is not active."""
    backend.claim_job_slot("job-001")

    mock_tracker = MagicMock()
    mock_job = MagicMock()
    mock_job.status = "failed"
    mock_tracker.get_job.return_value = mock_job

    result = backend.get_running_job_id(job_tracker=mock_tracker)
    assert result is None


def test_get_running_job_id_conservative_on_tracker_exception(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """get_running_job_id returns job_id conservatively when tracker raises."""
    backend.claim_job_slot("job-001")

    mock_tracker = MagicMock()
    mock_tracker.get_job.side_effect = RuntimeError("tracker unavailable")

    result = backend.get_running_job_id(job_tracker=mock_tracker)
    assert result == "job-001"


# ---------------------------------------------------------------------------
# Atomic write safety
# ---------------------------------------------------------------------------


def test_atomic_write_readers_see_complete_payload(
    backend: FilesystemDashboardCacheBackend,
    cache_dir: Path,
) -> None:
    """Concurrent readers never observe a partial write (atomic via tmp+rename)."""
    cache_file = cache_dir / "_dashboard_cache.json"
    errors = []
    stop_flag = threading.Event()

    def reader() -> None:
        while not stop_flag.is_set():
            if not cache_file.exists():
                continue
            try:
                data = cache_file.read_text()
                parsed = json.loads(data)
                # Must have the expected key
                if "result_json" not in parsed:
                    errors.append(f"Incomplete payload: {data!r}")
            except json.JSONDecodeError as exc:
                errors.append(f"Partial write detected: {exc} — raw: {data!r}")

    reader_threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in reader_threads:
        t.start()

    for i in range(20):
        backend.set_result(f'{{"iteration": {i}}}')

    stop_flag.set()
    for t in reader_threads:
        t.join(timeout=5)

    assert not errors, f"Atomic write violations: {errors}"


# ---------------------------------------------------------------------------
# Corrupt JSON tolerance
# ---------------------------------------------------------------------------


def test_get_cached_returns_none_on_corrupt_json(
    cache_dir: Path,
) -> None:
    """get_cached returns None gracefully when cache file contains invalid JSON."""
    cache_file = cache_dir / "_dashboard_cache.json"
    cache_file.write_text("{not valid json at all")

    backend = FilesystemDashboardCacheBackend(cache_dir=cache_dir)
    result = backend.get_cached()
    assert result is None


def test_is_fresh_returns_false_on_corrupt_json(
    cache_dir: Path,
) -> None:
    """is_fresh returns False gracefully when cache file is corrupt."""
    cache_file = cache_dir / "_dashboard_cache.json"
    cache_file.write_text("!!!")

    backend = FilesystemDashboardCacheBackend(cache_dir=cache_dir)
    assert backend.is_fresh(600) is False


# ---------------------------------------------------------------------------
# set_cached (interface-parity with DependencyMapDashboardCacheBackend)
# ---------------------------------------------------------------------------


def test_set_cached_writes_result_and_clears_job_id(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """set_cached writes result_json, sets computed_at, clears job_id and failure fields."""
    backend.claim_job_slot("job-001")
    backend.set_cached('{"nodes": [], "edges": []}')

    cached = backend.get_cached()
    assert cached is not None
    assert cached["result_json"] == '{"nodes": [], "edges": []}'
    assert cached["computed_at"] is not None
    assert cached["job_id"] is None
    assert cached["last_failure_message"] is None
    assert cached["last_failure_at"] is None


def test_set_cached_accepts_optional_job_id_argument_but_stores_null(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """set_cached accepts job_id kwarg for API compatibility but always stores None."""
    backend.set_cached('{"data": "x"}', job_id="job-ignored")

    cached = backend.get_cached()
    assert cached is not None
    assert cached["job_id"] is None


def test_set_cached_raises_on_none_result_json(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """set_cached raises ValueError when result_json is None (parity with SQLite)."""
    with pytest.raises(ValueError, match="result_json"):
        backend.set_cached(None)  # type: ignore[arg-type]


def test_set_cached_sets_computed_at_to_now(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """set_cached sets computed_at to approximately now (within 5 seconds)."""
    before = datetime.now(timezone.utc)
    backend.set_cached('{"v": 1}')
    after = datetime.now(timezone.utc)

    cached = backend.get_cached()
    assert cached is not None
    computed_at_str = cached["computed_at"]
    assert computed_at_str is not None
    computed_at = datetime.fromisoformat(computed_at_str)
    if computed_at.tzinfo is None:
        computed_at = computed_at.replace(tzinfo=timezone.utc)
    assert before <= computed_at <= after


# ---------------------------------------------------------------------------
# clear_job_slot (public version — interface parity)
# ---------------------------------------------------------------------------


def test_clear_job_slot_resets_job_id(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """clear_job_slot sets job_id to None, preserving other fields."""
    backend.set_result('{"data": "x"}')
    backend.claim_job_slot("job-001")

    backend.clear_job_slot()

    cached = backend.get_cached()
    assert cached is not None
    assert cached["job_id"] is None
    assert cached["result_json"] == '{"data": "x"}'


def test_clear_job_slot_no_op_when_no_cache(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """clear_job_slot is a no-op when no cache file exists."""
    backend.clear_job_slot()  # must not raise


# ---------------------------------------------------------------------------
# mark_job_failed
# ---------------------------------------------------------------------------


def test_mark_job_failed_writes_failure_fields_and_clears_job_id(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """mark_job_failed sets last_failure_message, last_failure_at, clears job_id."""
    backend.claim_job_slot("job-001")
    backend.mark_job_failed("something went wrong")

    cached = backend.get_cached()
    assert cached is not None
    assert cached["job_id"] is None
    assert cached["last_failure_message"] == "something went wrong"
    assert cached["last_failure_at"] is not None


def test_mark_job_failed_preserves_existing_result(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """mark_job_failed preserves result_json and computed_at from a previous success."""
    backend.set_result('{"nodes": []}')
    backend.claim_job_slot("job-retry")
    backend.mark_job_failed("transient error")

    cached = backend.get_cached()
    assert cached is not None
    assert cached["result_json"] == '{"nodes": []}'
    assert cached["computed_at"] is not None


def test_mark_job_failed_creates_row_when_none_exists(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """mark_job_failed creates the cache file when none exists."""
    assert backend.get_cached() is None
    backend.mark_job_failed("first failure")

    cached = backend.get_cached()
    assert cached is not None
    assert cached["last_failure_message"] == "first failure"
    assert cached["job_id"] is None


def test_mark_job_failed_raises_on_none_error_message(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """mark_job_failed raises ValueError when error_message is None (parity with SQLite)."""
    with pytest.raises(ValueError, match="error_message"):
        backend.mark_job_failed(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# concurrent set_cached atomic safety
# ---------------------------------------------------------------------------


def test_concurrent_set_cached_atomic(
    backend: FilesystemDashboardCacheBackend,
    cache_dir: Path,
) -> None:
    """Concurrent set_cached calls never produce a partially-written file."""
    cache_file = cache_dir / "_dashboard_cache.json"
    errors: list = []
    stop_flag = threading.Event()

    def reader() -> None:
        while not stop_flag.is_set():
            if not cache_file.exists():
                continue
            try:
                data = cache_file.read_text()
                parsed = json.loads(data)
                if "result_json" not in parsed:
                    errors.append(f"Incomplete payload: {data!r}")
            except json.JSONDecodeError as exc:
                errors.append(f"Partial write detected: {exc}")

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for t in readers:
        t.start()

    for i in range(20):
        backend.set_cached(f'{{"iteration": {i}}}')

    stop_flag.set()
    for t in readers:
        t.join(timeout=5)

    assert not errors, f"Atomic write violations: {errors}"


# ---------------------------------------------------------------------------
# is_fresh — uncovered branches (lines 86, 91, 94-101)
# ---------------------------------------------------------------------------


def test_is_fresh_returns_false_when_computed_at_is_none(
    cache_dir: Path,
) -> None:
    """is_fresh returns False when cache exists but computed_at field is None/absent.

    Covers line 86: `if not computed_at_str: return False`
    """
    payload = {
        "computed_at": None,
        "job_id": "job-001",
        "result_json": '{"data": "x"}',
        "last_failure_message": None,
        "last_failure_at": None,
    }
    cache_file = cache_dir / "_dashboard_cache.json"
    cache_file.write_text(json.dumps(payload))

    backend = FilesystemDashboardCacheBackend(cache_dir=cache_dir)
    assert backend.is_fresh(600) is False


def test_is_fresh_handles_naive_computed_at_datetime(
    cache_dir: Path,
) -> None:
    """is_fresh correctly handles a naive ISO datetime in computed_at (no timezone).

    Covers line 91: `computed_at = computed_at.replace(tzinfo=timezone.utc)`
    """
    # Write a naive ISO datetime (no +00:00 / Z suffix)
    naive_now = datetime.utcnow()
    assert naive_now.tzinfo is None  # confirm naive
    payload = {
        "computed_at": naive_now.isoformat(),
        "job_id": None,
        "result_json": '{"data": "x"}',
        "last_failure_message": None,
        "last_failure_at": None,
    }
    cache_file = cache_dir / "_dashboard_cache.json"
    cache_file.write_text(json.dumps(payload))

    backend = FilesystemDashboardCacheBackend(cache_dir=cache_dir)
    # Written just now — should be fresh within 600s
    assert backend.is_fresh(600) is True


def test_is_fresh_returns_false_on_unparseable_computed_at(
    cache_dir: Path,
) -> None:
    """is_fresh returns False and logs a warning when computed_at is not ISO-parseable.

    Covers lines 94-101: except (ValueError, TypeError) branch in is_fresh.
    """
    payload = {
        "computed_at": "not-a-datetime-at-all",
        "job_id": None,
        "result_json": '{"data": "x"}',
        "last_failure_message": None,
        "last_failure_at": None,
    }
    cache_file = cache_dir / "_dashboard_cache.json"
    cache_file.write_text(json.dumps(payload))

    backend = FilesystemDashboardCacheBackend(cache_dir=cache_dir)
    assert backend.is_fresh(600) is False


# ---------------------------------------------------------------------------
# _write_atomic — exception + tmp cleanup (lines 314-319)
# ---------------------------------------------------------------------------


def test_write_atomic_cleans_up_tmp_file_and_reraises_on_failure(
    backend: FilesystemDashboardCacheBackend,
    cache_dir: Path,
) -> None:
    """_write_atomic cleans up the tmp file and re-raises when os.replace fails.

    Covers lines 314-319: except block in _write_atomic that unlinks tmp and raises.
    """
    from unittest.mock import patch
    import pytest

    with patch("os.replace", side_effect=OSError("replace failed")):
        with pytest.raises(OSError, match="replace failed"):
            backend.set_result('{"data": "test"}')

    # No leftover tmp files must exist in the cache dir
    tmp_files = list(cache_dir.glob("*.tmp.*"))
    assert tmp_files == [], f"Leftover tmp files after failure: {tmp_files}"


# ---------------------------------------------------------------------------
# _clear_job_slot (private) — lines 323-328
# ---------------------------------------------------------------------------


def test_private_clear_job_slot_resets_job_id(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """_clear_job_slot() (private) sets job_id to None while preserving other fields.

    Covers lines 323-328: the private _clear_job_slot method body.
    This method is a lower-level variant that can be called directly
    (e.g. from get_running_job_id via clear_job_slot in future refactors).
    """
    backend.set_result('{"nodes": []}')
    backend.claim_job_slot("job-001")

    # Call the private method directly
    backend._clear_job_slot()

    cached = backend.get_cached()
    assert cached is not None
    assert cached["job_id"] is None
    assert cached["result_json"] == '{"nodes": []}'


def test_private_clear_job_slot_noop_when_no_cache(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """_clear_job_slot() is a no-op when no cache file exists.

    Covers line 324-325: early-return branch in _clear_job_slot.
    """
    # No cache file present — must not raise
    backend._clear_job_slot()


def test_write_atomic_handles_unlink_failure_during_cleanup(
    backend: FilesystemDashboardCacheBackend,
) -> None:
    """_write_atomic swallows OSError from tmp unlink and still re-raises original error.

    Covers lines 317-318: except OSError: pass inside the cleanup try block,
    reached when os.replace fails AND the subsequent os.unlink of the tmp file also fails.
    """
    from unittest.mock import patch
    import pytest

    with patch("os.replace", side_effect=OSError("replace failed")):
        with patch("os.unlink", side_effect=OSError("unlink also failed")):
            with pytest.raises(OSError, match="replace failed"):
                backend.set_result('{"data": "test"}')
