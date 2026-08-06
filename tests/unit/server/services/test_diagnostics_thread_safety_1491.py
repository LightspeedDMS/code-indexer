"""Story #1491 AC4 follow-up: diagnostics shared-state locking.

``get_status()`` read AND mutated the shared ``_cache`` / ``_cache_timestamps``
dicts without taking the ``threading.Lock`` that ``run_all_diagnostics`` /
``run_category`` take from a different thread (each diagnostics run is now a
sync Starlette background task running its own ``asyncio.run()`` loop).  A
reader could therefore observe a half-published generation:
``_cache[category]`` already updated while ``_cache_timestamps[category]`` still
holds the previous run's value.  The fix takes a consistent snapshot under the
lock WITHOUT ever holding that cross-thread lock across database I/O.

Scope note -- the sibling review item about the "cosmetic" ``asyncio.wait_for``
wrappers in ``run_infrastructure_diagnostics`` is deliberately resolved by
DOCUMENTATION, not by code: the diagnostics run already executes off the event
loop (sync background task -> threadpool), so a deadline at that layer protects
nothing, and manufacturing one would mean abandoning a live worker thread it
cannot actually cancel.  See the rationale block at both ``wait_for`` sites in
``diagnostics_service.run_infrastructure_diagnostics``.  There is nothing
executable to assert for that decision, so no test here covers it.

What is real and what is stood in for: the ``DiagnosticsService`` under test is
real, with its real lock, driven by real threads.  Only EXTERNAL dependencies
are substituted, both through the seams the service already exposes for them --
the constructor-injected ``DiagnosticsBackend`` (its storage boundary) and
``create_token_manager`` (the credential store).  No method of the service under
test is patched anywhere in this file.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from unittest.mock import patch

from code_indexer.server.services.diagnostics_service import (
    DiagnosticCategory,
    DiagnosticsService,
)

# Bounded waits (Messi Rule #14): every wait in this file has an explicit
# ceiling and asserts on expiry rather than looping forever.
_JOIN_TIMEOUT_SECONDS = 30.0
_POLL_SECONDS = 0.01

# How long a reader is given to prove it is NOT blocked by a held lock.
_UNBLOCKED_OBSERVATION_SECONDS = 0.5


class _NoStoredTokens:
    """Credential-store stand-in: no stored platform tokens.

    Substituted for ``create_token_manager``'s product -- an external store --
    so every external-API check takes its real NOT_CONFIGURED branch and the
    concurrency test performs no network I/O.
    """

    def get_token(self, platform: str) -> None:
        return None


class _LockObservingBackend:
    """A real, in-memory DiagnosticsBackend that records lock state per read.

    This is the service's OWN constructor-injected storage boundary (Story #525
    ``DiagnosticsBackend``), i.e. the external dependency the service delegates
    all persistence to -- not a patched service method.  Every
    ``load_category_results`` call records whether the caller currently holds
    the shared-state lock.

    Detection is real, not inferred: ``threading.Lock`` is non-reentrant, so a
    non-blocking acquire from the very thread that already holds it fails.
    """

    def __init__(self) -> None:
        self._observed_lock: Optional[threading.Lock] = None
        self._rows: Dict[str, Tuple[str, str]] = {}
        self.lock_held_during_read: List[bool] = []

    def observe_lock(self, lock: threading.Lock) -> None:
        """Point the observer at the lock whose state each read must report."""
        self._observed_lock = lock

    def _record_lock_state(self) -> None:
        assert self._observed_lock is not None, "observe_lock() was never called"
        acquired = self._observed_lock.acquire(blocking=False)
        if acquired:
            self._observed_lock.release()
        self.lock_held_during_read.append(not acquired)

    def save_results(self, category: str, results_json: str, run_at: str) -> None:
        self._rows[category] = (results_json, run_at)

    def load_all_results(self) -> List[Tuple[str, str, str]]:
        return [(cat, row[0], row[1]) for cat, row in self._rows.items()]

    def load_category_results(self, category: str) -> Optional[Tuple[str, str]]:
        self._record_lock_state()
        return self._rows.get(category)

    def close(self) -> None:
        return None


def _service(tmp_path: Path) -> DiagnosticsService:
    """A real service whose configured storage layout exists on disk."""
    (tmp_path / "data" / "golden-repos").mkdir(parents=True)
    return DiagnosticsService(db_path=str(tmp_path / "cidx_server.db"))


def test_ac4_get_status_takes_the_shared_state_lock(tmp_path: Path) -> None:
    """get_status() must not read shared state while a writer holds the lock.

    RED before the fix: get_status() never touched the lock, so it returned
    immediately while a writer was mid-publication (cache updated, timestamps
    not yet) -- a torn read across two dicts.
    """
    service = _service(tmp_path)
    lock: threading.Lock = service._lock

    returned = threading.Event()
    error: List[BaseException] = []

    def _read() -> None:
        try:
            service.get_status()
        except BaseException as exc:  # pragma: no cover - surfaced below
            error.append(exc)
        finally:
            returned.set()

    # Populate the cache first so get_status takes its pure in-memory path and
    # the ONLY thing that can delay it is the lock itself.
    service.get_status()

    with lock:
        reader = threading.Thread(target=_read, name="story1491-status-reader")
        reader.start()
        blocked = not returned.wait(_UNBLOCKED_OBSERVATION_SECONDS)

    reader.join(timeout=_JOIN_TIMEOUT_SECONDS)
    assert not reader.is_alive(), "the reader thread never finished"
    assert not error, f"get_status raised: {error}"
    assert blocked, (
        "get_status() returned while a writer held the shared-state lock -- it "
        "is reading _cache/_cache_timestamps unsynchronised and can observe a "
        "half-published generation (Story #1491 AC4)"
    )


def test_ac4_get_status_never_holds_the_lock_across_storage_io(
    tmp_path: Path,
) -> None:
    """The cross-thread lock must never be held across the persistence read.

    Holding it there would make every concurrent reader/writer block on storage
    I/O -- the exact defect this story removed from run_all_diagnostics's
    persistence step, and it must not be reintroduced on the read side.
    """
    backend = _LockObservingBackend()
    service = DiagnosticsService(
        db_path=str(tmp_path / "cidx_server.db"), storage_backend=backend
    )
    backend.observe_lock(service._lock)

    persisted = json.dumps(
        [
            {
                "name": "persisted-probe",
                "status": "working",
                "message": "persisted by the injected backend",
                "details": {},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ]
    )
    for category in DiagnosticCategory:
        backend.save_results(
            category.value, persisted, datetime.now(timezone.utc).isoformat()
        )

    status = service.get_status()

    assert backend.lock_held_during_read, "the storage read path never ran"
    assert not any(backend.lock_held_during_read), (
        "get_status() held the cross-thread shared-state lock across a storage "
        "read -- persistence I/O must always be outside that lock"
    )
    # The persisted rows really were served, so the observed reads are the ones
    # on the live status path rather than a placeholder shortcut.
    for category in DiagnosticCategory:
        assert [r.name for r in status[category]] == ["persisted-probe"]


def test_ac4_concurrent_get_status_during_real_run_stays_consistent(
    tmp_path: Path,
) -> None:
    """A real in-flight run_all_diagnostics must never expose a torn snapshot.

    Drives the REAL sync background-task entry point (the production
    registration Starlette threadpools) on a worker thread while the main
    thread hammers the REAL get_status(), asserting every observation is
    internally consistent: every category present with a non-empty result list.
    """
    with patch(
        "code_indexer.server.services.ci_token_manager.create_token_manager",
        return_value=_NoStoredTokens(),
    ):
        service = _service(tmp_path)
        failures: List[str] = []

        worker = threading.Thread(
            target=service.run_all_diagnostics_sync,
            name="story1491-diagnostics-run",
        )
        worker.start()
        try:
            deadline = time.monotonic() + _JOIN_TIMEOUT_SECONDS
            observations = 0
            while worker.is_alive() and time.monotonic() < deadline:
                status = service.get_status()
                observations += 1
                for category in DiagnosticCategory:
                    if category not in status:
                        failures.append(f"{category.value} missing from get_status()")
                        continue
                    if not status[category]:
                        failures.append(
                            f"{category.value} returned an empty result list"
                        )
                time.sleep(_POLL_SECONDS)
        finally:
            worker.join(timeout=_JOIN_TIMEOUT_SECONDS)

    assert not worker.is_alive(), "the diagnostics run never completed"
    assert observations > 0, "no concurrent get_status() observation was taken"
    assert not failures, f"inconsistent get_status() observations: {failures[:5]}"
