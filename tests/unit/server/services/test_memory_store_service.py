"""
Tests for MemoryStoreService — Story #877 Phase 1b.

Real filesystem (tmp_path), real MemoryFileLockManager, real memory_io,
real MemoryRateLimiter, real memory_schema. MagicMock for RefreshScheduler
and RefreshDebouncer only (external service boundaries).

Setup pattern: infrastructure fixtures (`memories_dir`, `lock_manager`,
`rate_limiter`) are pytest fixtures; service construction uses the
`_build_service()` builder helper so each test can vary scheduler/debouncer
configuration without parametrising the fixture.
"""

from pathlib import Path
from typing import Any, Dict, Tuple
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.memory_file_lock_manager import MemoryFileLockManager
from code_indexer.server.services.memory_io import read_memory_file
from code_indexer.server.services.memory_rate_limiter import (
    MemoryRateLimiter,
    RateLimitConfig,
)
from code_indexer.server.services.memory_schema import MemorySchemaValidationError
from code_indexer.server.services.memory_store_service import (
    MemoryStoreConfig,
    MemoryStoreService,
    RateLimitError,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FIXED_UUID = "aaaabbbbccccddddeeeeffff00001111"


# ---------------------------------------------------------------------------
# Payload factory
# ---------------------------------------------------------------------------

def _valid_create_payload() -> Dict[str, Any]:
    """Minimal valid user-supplied create payload (no server-filled fields)."""
    return {
        "type": "architectural-fact",
        "scope": "global",
        "summary": "short",
        "evidence": [{"commit": "abc123"}],
    }


# ---------------------------------------------------------------------------
# Pytest infrastructure fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def memories_dir(tmp_path: Path) -> Path:
    return tmp_path / "memories"


@pytest.fixture
def lock_manager(tmp_path: Path) -> MemoryFileLockManager:
    return MemoryFileLockManager(tmp_path / "locks")


@pytest.fixture
def rate_limiter() -> MemoryRateLimiter:
    return MemoryRateLimiter(RateLimitConfig(capacity=100, refill_per_second=100.0))


# ---------------------------------------------------------------------------
# Builder helper
# ---------------------------------------------------------------------------

def _build_service(
    memories_dir: Path,
    lock_manager: MemoryFileLockManager,
    rate_limiter: MemoryRateLimiter,
    *,
    max_summary_chars: int = 1000,
    is_held: bool = False,
    acquire_ok: bool = True,
    hostname: str = "testhost",
    id_factory=None,
) -> Tuple[MemoryStoreService, MagicMock, MagicMock]:
    """Return (service, scheduler_mock, debouncer_mock).

    Tests call this directly to configure the scheduler/debouncer variant
    needed for each scenario.
    """
    config = MemoryStoreConfig(
        memories_dir=memories_dir,
        max_summary_chars=max_summary_chars,
        per_memory_lock_ttl_seconds=30,
        coarse_lock_ttl_seconds=60,
    )
    scheduler = MagicMock()
    scheduler.is_write_lock_held.return_value = is_held
    scheduler.acquire_write_lock.return_value = acquire_ok
    scheduler.release_write_lock.return_value = True
    debouncer = MagicMock()
    svc = MemoryStoreService(
        config=config,
        lock_manager=lock_manager,
        refresh_scheduler=scheduler,
        refresh_debouncer=debouncer,
        rate_limiter=rate_limiter,
        hostname=hostname,
        id_factory=id_factory,
    )
    return svc, scheduler, debouncer


# ---------------------------------------------------------------------------
# Assertion helpers used by tests 1-8
# ---------------------------------------------------------------------------

def _assert_refresh_direct(scheduler: MagicMock, debouncer: MagicMock) -> None:
    scheduler.trigger_refresh_for_repo.assert_called_once_with("cidx-meta-global")
    scheduler.release_write_lock.assert_called_once()
    debouncer.signal_dirty.assert_not_called()


def _assert_refresh_piggyback(scheduler: MagicMock, debouncer: MagicMock) -> None:
    scheduler.trigger_refresh_for_repo.assert_not_called()
    scheduler.acquire_write_lock.assert_not_called()
    scheduler.release_write_lock.assert_not_called()
    debouncer.signal_dirty.assert_called_once()


# ---------------------------------------------------------------------------
# Tests 1-8: create_memory
# ---------------------------------------------------------------------------

def test_create_memory_writes_file_with_server_filled_fields(
    memories_dir, lock_manager, rate_limiter
):
    svc, _s, _d = _build_service(
        memories_dir, lock_manager, rate_limiter, id_factory=lambda: FIXED_UUID
    )

    svc.create_memory(_valid_create_payload(), username="alice")

    fm, _body, _hash = read_memory_file(memories_dir / f"{FIXED_UUID}.md")
    assert fm["id"] == FIXED_UUID
    assert fm["created_by"] == "alice"
    assert fm["created_at"] is not None
    assert fm.get("edited_by") is None
    assert fm.get("edited_at") is None


def test_create_memory_returns_id_and_content_hash(memories_dir, lock_manager, rate_limiter):
    svc, _s, _d = _build_service(
        memories_dir, lock_manager, rate_limiter, id_factory=lambda: FIXED_UUID
    )

    result = svc.create_memory(_valid_create_payload(), username="alice")

    assert result["id"] == FIXED_UUID
    assert len(result["content_hash"]) == 64
    assert "path" in result


def test_create_memory_respects_max_summary_chars(memories_dir, lock_manager, rate_limiter):
    svc, _s, _d = _build_service(
        memories_dir, lock_manager, rate_limiter, max_summary_chars=10
    )
    payload = _valid_create_payload()
    payload["summary"] = "x" * 20

    with pytest.raises(MemorySchemaValidationError):
        svc.create_memory(payload, username="alice")


def test_create_memory_rate_limit_throttles(memories_dir, lock_manager):
    frozen = [0.0]
    rl = MemoryRateLimiter(
        RateLimitConfig(capacity=1, refill_per_second=0.001),
        clock=lambda: frozen[0],
    )
    counter = [0]

    def _id_factory():
        counter[0] += 1
        return f"id{counter[0]:08x}"

    svc, _s, _d = _build_service(memories_dir, lock_manager, rl, id_factory=_id_factory)

    svc.create_memory(_valid_create_payload(), username="alice")

    with pytest.raises(RateLimitError):
        svc.create_memory(_valid_create_payload(), username="alice")


def test_create_memory_triggers_refresh_direct_acquire(memories_dir, lock_manager, rate_limiter):
    svc, scheduler, debouncer = _build_service(
        memories_dir, lock_manager, rate_limiter,
        is_held=False, acquire_ok=True, id_factory=lambda: FIXED_UUID,
    )

    svc.create_memory(_valid_create_payload(), username="alice")

    _assert_refresh_direct(scheduler, debouncer)


def test_create_memory_piggyback_signals_debouncer_only(memories_dir, lock_manager, rate_limiter):
    svc, scheduler, debouncer = _build_service(
        memories_dir, lock_manager, rate_limiter,
        is_held=True, id_factory=lambda: FIXED_UUID,
    )

    svc.create_memory(_valid_create_payload(), username="alice")

    _assert_refresh_piggyback(scheduler, debouncer)


def test_create_memory_duplicate_job_falls_back_to_debouncer(
    memories_dir, lock_manager, rate_limiter
):
    from code_indexer.server.services.job_tracker import DuplicateJobError

    svc, scheduler, debouncer = _build_service(
        memories_dir, lock_manager, rate_limiter,
        is_held=False, acquire_ok=True, id_factory=lambda: FIXED_UUID,
    )
    scheduler.trigger_refresh_for_repo.side_effect = DuplicateJobError(
        "refresh", "cidx-meta-global", "job-abc"
    )

    svc.create_memory(_valid_create_payload(), username="alice")

    debouncer.signal_dirty.assert_called_once()
    assert (memories_dir / f"{FIXED_UUID}.md").exists()


def test_create_memory_trigger_unknown_exception_falls_back_to_debouncer(
    memories_dir, lock_manager, rate_limiter
):
    svc, scheduler, debouncer = _build_service(
        memories_dir, lock_manager, rate_limiter,
        is_held=False, acquire_ok=True, id_factory=lambda: FIXED_UUID,
    )
    scheduler.trigger_refresh_for_repo.side_effect = RuntimeError("boom")

    svc.create_memory(_valid_create_payload(), username="alice")

    debouncer.signal_dirty.assert_called_once()
    assert (memories_dir / f"{FIXED_UUID}.md").exists()


# ---------------------------------------------------------------------------
# Shared helpers for tests 9-25
# ---------------------------------------------------------------------------

def _make_counter_id_factory():
    """Return a stateful id factory. Each call to the returned callable yields
    the next id in sequence: id00000001, id00000002, …

    Callers must retain the factory and pass it to every _create_one call that
    shares the same memories_dir to guarantee cross-call uniqueness.
    """
    counter = [0]

    def _factory():
        counter[0] += 1
        return f"id{counter[0]:08x}"

    return _factory


def _create_one(
    memories_dir: Path,
    lock_manager: MemoryFileLockManager,
    rate_limiter: MemoryRateLimiter,
    id_factory,
    *,
    is_held: bool = False,
    acquire_ok: bool = True,
    username: str = "alice",
):
    """Create one memory and return (svc, scheduler, debouncer, memory_id, content_hash).

    Callers own the id_factory lifetime. Passing the same factory across
    multiple _create_one calls in a single test guarantees unique IDs within
    that test's memories_dir.
    """
    svc, scheduler, debouncer = _build_service(
        memories_dir,
        lock_manager,
        rate_limiter,
        is_held=is_held,
        acquire_ok=acquire_ok,
        id_factory=id_factory,
    )
    result = svc.create_memory(_valid_create_payload(), username=username)
    return svc, scheduler, debouncer, result["id"], result["content_hash"]


# ---------------------------------------------------------------------------
# Tests 9-11: conflict, schema failure, edit happy path
# ---------------------------------------------------------------------------

def test_create_memory_conflict_on_locked_id(memories_dir, lock_manager, rate_limiter):
    """Test 9: ConflictError raised when per-memory lock is already held externally."""
    from code_indexer.server.services.memory_store_service import ConflictError

    svc, _s, _d = _build_service(
        memories_dir, lock_manager, rate_limiter, id_factory=lambda: FIXED_UUID
    )
    external_owner = "external@testhost/otherprocess"
    acquired = lock_manager.acquire(FIXED_UUID, external_owner, ttl_seconds=60)
    assert acquired, "External lock acquisition must succeed for test setup"

    try:
        with pytest.raises(ConflictError):
            svc.create_memory(_valid_create_payload(), username="alice")
    finally:
        lock_manager.release(FIXED_UUID, external_owner)


def test_create_memory_never_triggers_on_schema_failure(memories_dir, lock_manager, rate_limiter):
    """Test 10: Schema failure leaves no file on disk and fires no refresh signals."""
    svc, scheduler, debouncer = _build_service(
        memories_dir, lock_manager, rate_limiter,
        id_factory=lambda: FIXED_UUID,
    )
    payload = _valid_create_payload()
    payload["evidence"] = []  # violates: evidence must have at least one entry

    with pytest.raises(MemorySchemaValidationError):
        svc.create_memory(payload, username="alice")

    scheduler.trigger_refresh_for_repo.assert_not_called()
    debouncer.signal_dirty.assert_not_called()
    assert not (memories_dir / f"{FIXED_UUID}.md").exists()


def test_edit_memory_happy_path(memories_dir, lock_manager, rate_limiter):
    """Test 11: edit updates summary, sets edited_by/edited_at, preserves created_by/created_at."""
    ids = _make_counter_id_factory()
    svc, _s, _d, memory_id, original_hash = _create_one(
        memories_dir, lock_manager, rate_limiter, ids
    )

    fm_before, _body, _h = read_memory_file(memories_dir / f"{memory_id}.md")
    original_created_by = fm_before["created_by"]
    original_created_at = fm_before["created_at"]

    edit_payload = {**_valid_create_payload(), "summary": "updated summary text"}
    edit_result = svc.edit_memory(memory_id, edit_payload, original_hash, username="bob")

    assert edit_result["id"] == memory_id
    assert edit_result["content_hash"] != original_hash

    fm_after, _body2, _h2 = read_memory_file(memories_dir / f"{memory_id}.md")
    assert fm_after["summary"] == "updated summary text"
    assert fm_after["edited_by"] == "bob"
    assert isinstance(fm_after["edited_at"], str) and fm_after["edited_at"]
    assert fm_after["created_by"] == original_created_by
    assert fm_after["created_at"] == original_created_at


# ---------------------------------------------------------------------------
# Tests 12-14: stale hash on edit, not-found, immutable fields
# ---------------------------------------------------------------------------

def test_edit_memory_stale_hash_raises_stale_content_error(memories_dir, lock_manager, rate_limiter):
    """Test 12: Wrong hash on edit raises StaleContentError carrying the real hash; disk unchanged."""
    from code_indexer.server.services.memory_store_service import StaleContentError

    ids = _make_counter_id_factory()
    svc, _s, _d, memory_id, real_hash = _create_one(memories_dir, lock_manager, rate_limiter, ids)

    with pytest.raises(StaleContentError) as exc_info:
        svc.edit_memory(memory_id, _valid_create_payload(), "wrong_hash_value", username="alice")

    assert exc_info.value.current_hash == real_hash
    _fm, _body, disk_hash = read_memory_file(memories_dir / f"{memory_id}.md")
    assert disk_hash == real_hash


def test_edit_memory_not_found(memories_dir, lock_manager, rate_limiter):
    """Test 13: edit on a memory_id that was never created raises NotFoundError."""
    from code_indexer.server.services.memory_store_service import NotFoundError

    svc, _s, _d = _build_service(memories_dir, lock_manager, rate_limiter)

    with pytest.raises(NotFoundError):
        svc.edit_memory("nonexistent00001", _valid_create_payload(), "any_hash", username="alice")


@pytest.mark.parametrize("immutable_field,bad_value", [
    ("id", "different-id"),
    ("created_by", "hacker"),
    ("created_at", "1970-01-01T00:00:00+00:00"),
])
def test_edit_memory_immutable_field_change_rejected(
    memories_dir, lock_manager, rate_limiter, immutable_field, bad_value
):
    """Test 14 (parametrized): Changing an immutable field on edit raises MemorySchemaValidationError
    with .field matching the rejected field name."""
    ids = _make_counter_id_factory()
    svc, _s, _d, memory_id, correct_hash = _create_one(
        memories_dir, lock_manager, rate_limiter, ids
    )

    edit_payload = {**_valid_create_payload(), immutable_field: bad_value}
    with pytest.raises(MemorySchemaValidationError) as exc_info:
        svc.edit_memory(memory_id, edit_payload, correct_hash, username="alice")

    assert exc_info.value.field == immutable_field


# ---------------------------------------------------------------------------
# Tests 15-17: edit refresh-trigger paths
# ---------------------------------------------------------------------------

def test_edit_memory_triggers_refresh_direct_acquire(memories_dir, lock_manager, rate_limiter):
    """Test 15: edit with is_write_lock_held=False calls trigger_refresh + release_write_lock once."""
    ids = _make_counter_id_factory()
    svc, scheduler, debouncer, memory_id, original_hash = _create_one(
        memories_dir, lock_manager, rate_limiter, ids, is_held=False, acquire_ok=True
    )
    scheduler.reset_mock()
    debouncer.reset_mock()

    edit_payload = {**_valid_create_payload(), "summary": "new summary direct"}
    svc.edit_memory(memory_id, edit_payload, original_hash, username="alice")

    scheduler.trigger_refresh_for_repo.assert_called_once_with("cidx-meta-global")
    scheduler.release_write_lock.assert_called_once()
    debouncer.signal_dirty.assert_not_called()


def test_edit_memory_triggers_refresh_piggyback(memories_dir, lock_manager, rate_limiter):
    """Test 16: edit with is_write_lock_held=True signals debouncer only; no coarse acquire/release."""
    # Create with direct mode so the file lands on disk
    ids = _make_counter_id_factory()
    svc_create, _sc, _dc, memory_id, original_hash = _create_one(
        memories_dir, lock_manager, rate_limiter, ids, is_held=False, acquire_ok=True
    )

    # Build a separate piggyback-mode service sharing the same infra
    svc_edit, scheduler_edit, debouncer_edit = _build_service(
        memories_dir, lock_manager, rate_limiter,
        is_held=True, id_factory=ids,
    )
    scheduler_edit.reset_mock()
    debouncer_edit.reset_mock()

    edit_payload = {**_valid_create_payload(), "summary": "piggyback edit"}
    svc_edit.edit_memory(memory_id, edit_payload, original_hash, username="alice")

    scheduler_edit.trigger_refresh_for_repo.assert_not_called()
    scheduler_edit.acquire_write_lock.assert_not_called()
    scheduler_edit.release_write_lock.assert_not_called()
    debouncer_edit.signal_dirty.assert_called_once()


def test_edit_memory_stale_does_not_trigger_refresh(memories_dir, lock_manager, rate_limiter):
    """Test 17: StaleContentError on edit fires neither trigger_refresh_for_repo nor signal_dirty."""
    from code_indexer.server.services.memory_store_service import StaleContentError

    ids = _make_counter_id_factory()
    svc, scheduler, debouncer, memory_id, _hash = _create_one(
        memories_dir, lock_manager, rate_limiter, ids, is_held=False, acquire_ok=True
    )
    scheduler.reset_mock()
    debouncer.reset_mock()

    with pytest.raises(StaleContentError):
        svc.edit_memory(memory_id, _valid_create_payload(), "wrong_hash", username="alice")

    scheduler.trigger_refresh_for_repo.assert_not_called()
    debouncer.signal_dirty.assert_not_called()


# ---------------------------------------------------------------------------
# Tests 18-21b: delete paths
# ---------------------------------------------------------------------------

def test_delete_memory_happy_path(memories_dir, lock_manager, rate_limiter):
    """Test 18: delete with correct hash removes the memory file from disk."""
    ids = _make_counter_id_factory()
    svc, _s, _d, memory_id, correct_hash = _create_one(memories_dir, lock_manager, rate_limiter, ids)
    file_path = memories_dir / f"{memory_id}.md"

    assert file_path.exists()
    svc.delete_memory(memory_id, correct_hash, username="alice")
    assert not file_path.exists()


def test_delete_memory_stale_hash_raises(memories_dir, lock_manager, rate_limiter):
    """Test 19: delete with wrong hash raises StaleContentError; file remains on disk."""
    from code_indexer.server.services.memory_store_service import StaleContentError

    ids = _make_counter_id_factory()
    svc, _s, _d, memory_id, _hash = _create_one(memories_dir, lock_manager, rate_limiter, ids)
    file_path = memories_dir / f"{memory_id}.md"

    with pytest.raises(StaleContentError):
        svc.delete_memory(memory_id, "wrong_hash", username="alice")

    assert file_path.exists()


def test_delete_memory_not_found(memories_dir, lock_manager, rate_limiter):
    """Test 20: delete on a never-created memory_id raises NotFoundError."""
    from code_indexer.server.services.memory_store_service import NotFoundError

    svc, _s, _d = _build_service(memories_dir, lock_manager, rate_limiter)

    with pytest.raises(NotFoundError):
        svc.delete_memory("nonexistent00002", "any_hash", username="alice")


def test_delete_triggers_refresh_direct_acquire(memories_dir, lock_manager, rate_limiter):
    """Test 21: delete with is_write_lock_held=False calls trigger_refresh + release_write_lock once."""
    ids = _make_counter_id_factory()
    svc, scheduler, debouncer, memory_id, correct_hash = _create_one(
        memories_dir, lock_manager, rate_limiter, ids, is_held=False, acquire_ok=True
    )
    scheduler.reset_mock()
    debouncer.reset_mock()

    svc.delete_memory(memory_id, correct_hash, username="alice")

    scheduler.trigger_refresh_for_repo.assert_called_once_with("cidx-meta-global")
    scheduler.release_write_lock.assert_called_once()
    debouncer.signal_dirty.assert_not_called()


def test_delete_triggers_refresh_piggyback(memories_dir, lock_manager, rate_limiter):
    """Test 21b: delete with is_write_lock_held=True signals debouncer only; no coarse acquire/release."""
    ids = _make_counter_id_factory()
    svc_create, _sc, _dc, memory_id, correct_hash = _create_one(
        memories_dir, lock_manager, rate_limiter, ids, is_held=False, acquire_ok=True
    )

    svc_delete, scheduler_del, debouncer_del = _build_service(
        memories_dir, lock_manager, rate_limiter,
        is_held=True, id_factory=ids,
    )
    scheduler_del.reset_mock()
    debouncer_del.reset_mock()

    svc_delete.delete_memory(memory_id, correct_hash, username="alice")

    scheduler_del.trigger_refresh_for_repo.assert_not_called()
    scheduler_del.acquire_write_lock.assert_not_called()
    scheduler_del.release_write_lock.assert_not_called()
    debouncer_del.signal_dirty.assert_called_once()


# ---------------------------------------------------------------------------
# Tests 22-25: lock-release guarantees on exception, owner name format
# ---------------------------------------------------------------------------

# Named filesystem permission constants (no magic numbers)
_READ_ONLY_DIR_MODE = 0o555
_WRITABLE_DIR_MODE = 0o755

# The id that a fresh counter factory produces on its very first call
_FIRST_COUNTER_ID = "id00000001"


def _write_failing_create(svc, memories_dir):
    """Context manager: make memories_dir read-only, attempt create_memory,
    restore permissions, and assert PermissionError propagated.

    Encapsulates the repeated chmod + pytest.raises + restore pattern so each
    exception-path test only needs to assert on its own lock-release behavior.
    """
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        memories_dir.mkdir(parents=True, exist_ok=True)
        memories_dir.chmod(_READ_ONLY_DIR_MODE)
        try:
            with pytest.raises(PermissionError):
                svc.create_memory(_valid_create_payload(), username="alice")
        finally:
            memories_dir.chmod(_WRITABLE_DIR_MODE)
        yield

    return _ctx()


def test_per_memory_lock_released_on_exception(memories_dir, lock_manager, rate_limiter):
    """Test 22: per-memory lock is released in finally even when write raises PermissionError."""
    ids = _make_counter_id_factory()
    svc, _s, _d = _build_service(
        memories_dir, lock_manager, rate_limiter, id_factory=ids
    )

    with _write_failing_create(svc, memories_dir):
        pass

    assert not lock_manager.is_locked(_FIRST_COUNTER_ID)


def test_coarse_lock_released_on_exception_direct_acquire(memories_dir, lock_manager, rate_limiter):
    """Test 23: coarse lock is released via release_write_lock when write raises (direct-acquire)."""
    ids = _make_counter_id_factory()
    svc, scheduler, _d = _build_service(
        memories_dir, lock_manager, rate_limiter,
        is_held=False, acquire_ok=True, id_factory=ids,
    )

    with _write_failing_create(svc, memories_dir):
        pass

    scheduler.release_write_lock.assert_called_once()
    alias_used = scheduler.release_write_lock.call_args[0][0]
    assert alias_used == "cidx-meta"


def test_coarse_lock_not_released_on_piggyback(memories_dir, lock_manager, rate_limiter):
    """Test 24: release_write_lock is NOT called on exception when piggybacking (never acquired)."""
    ids = _make_counter_id_factory()
    svc, scheduler, _d = _build_service(
        memories_dir, lock_manager, rate_limiter,
        is_held=True, id_factory=ids,
    )

    with _write_failing_create(svc, memories_dir):
        pass

    scheduler.release_write_lock.assert_not_called()


def test_owner_name_includes_hostname_and_username(memories_dir, lock_manager, rate_limiter):
    """Test 25: owner name passed to lock_manager.acquire is 'memory_store@{hostname}/{username}'."""
    from unittest.mock import patch

    svc, _s, _d = _build_service(
        memories_dir, lock_manager, rate_limiter,
        hostname="node-X", id_factory=lambda: FIXED_UUID,
    )

    with patch.object(lock_manager, "acquire", wraps=lock_manager.acquire) as spy:
        svc.create_memory(_valid_create_payload(), username="alice")

    assert spy.call_count >= 1
    # First call is the per-memory lock: acquire(memory_id, owner_name, ttl_seconds=...)
    args, _kwargs = spy.call_args_list[0]
    owner_arg = args[1]
    assert owner_arg == "memory_store@node-X/alice"
