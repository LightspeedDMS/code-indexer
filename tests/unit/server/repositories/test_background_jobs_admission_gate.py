"""memory-aware admission gate in BackgroundJobManager.

Covers the pure decision helper (_admission_blocked) and the pool-worker
behavior (a blocked heavy job is re-queued and NOT executed until the governor
allows). Uses a stub governor installed via set_memory_governor so no real
cgroup/psutil I/O is involved.
"""

import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List

import pytest

# Import via the `code_indexer` package name (NOT `src.code_indexer`): the module
# under test reads the MemoryGovernor singleton through `code_indexer...`, so the
# test must install the stub on that same module object. Mixing the two import
# prefixes would create two module identities with two separate singletons.
from code_indexer.server.repositories.background_jobs import (
    BackgroundJob,
    BackgroundJobManager,
    JobStatus,
)
from code_indexer.server.services import memory_governor as mg
from code_indexer.server.utils.config_manager import BackgroundJobsConfig


class _StubGovernor:
    """Minimal governor stand-in: admission_allowed returns a settable flag."""

    def __init__(self, allowed: bool):
        self.allowed = allowed
        self.calls: List[float] = []
        # Optional governor-reporting attributes, mirroring the real
        # MemoryGovernor's `band`/`last_used_pct` properties. Left unset
        # (None / 0.0) by default; some tests assign real values to exercise
        # _log_admission_deferred's breadcrumb formatting.
        self.band: Any = None
        self.last_used_pct: float = 0.0

    def admission_allowed(self, max_used_pct: float) -> bool:
        self.calls.append(max_used_pct)
        return self.allowed


def _job(job_id: str, op_type: str) -> BackgroundJob:
    return BackgroundJob(
        job_id=job_id,
        operation_type=op_type,
        status=JobStatus.PENDING,
        created_at=datetime.now(timezone.utc),
        started_at=None,
        completed_at=None,
        result=None,
        error=None,
        progress=0,
        username="admin",
        is_admin=True,
    )


class TestAdmissionBlockedHelper:
    """_admission_blocked() decision matrix — no threads."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.storage = Path(self.temp_dir) / "jobs.json"
        self.manager = None

    def teardown_method(self):
        if self.manager is not None:
            self.manager.shutdown()
        mg.clear_memory_governor()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def _manager(self, **cfg_kwargs):
        config = BackgroundJobsConfig(max_concurrent_background_jobs=1, **cfg_kwargs)
        self.manager = BackgroundJobManager(
            storage_path=str(self.storage), background_jobs_config=config
        )
        return self.manager

    def test_heavy_op_blocked_when_governor_denies(self):
        m = self._manager()
        m.jobs["j1"] = _job("j1", "add_golden_repo")
        mg.set_memory_governor(_StubGovernor(allowed=False))
        assert m._admission_blocked("j1") is True

    def test_heavy_op_allowed_when_governor_permits(self):
        m = self._manager()
        m.jobs["j1"] = _job("j1", "add_golden_repo")
        mg.set_memory_governor(_StubGovernor(allowed=True))
        assert m._admission_blocked("j1") is False

    def test_cheap_op_never_blocked(self):
        m = self._manager()
        m.jobs["j1"] = _job("j1", "xray_search")
        mg.set_memory_governor(_StubGovernor(allowed=False))
        assert m._admission_blocked("j1") is False

    def test_no_governor_fails_open(self):
        m = self._manager()
        m.jobs["j1"] = _job("j1", "add_golden_repo")
        mg.clear_memory_governor()
        assert m._admission_blocked("j1") is False

    def test_gate_disabled_fails_open(self):
        m = self._manager(job_admission_memory_gate_enabled=False)
        m.jobs["j1"] = _job("j1", "add_golden_repo")
        mg.set_memory_governor(_StubGovernor(allowed=False))
        assert m._admission_blocked("j1") is False

    def test_governor_exception_fails_open(self):
        # L1: a governor that raises must not propagate out of the admission
        # check -- otherwise the exception would kill the pool-worker thread and
        # silently stop the lane. Fail-open (do not block).
        m = self._manager()
        m.jobs["j1"] = _job("j1", "add_golden_repo")

        class _BoomGovernor:
            def admission_allowed(self, max_used_pct):
                raise RuntimeError("governor boom")

        mg.set_memory_governor(_BoomGovernor())
        assert m._admission_blocked("j1") is False

    def test_uses_configured_watermark(self):
        m = self._manager(job_admission_memory_max_used_pct=73.5)
        m.jobs["j1"] = _job("j1", "add_golden_repo")
        stub = _StubGovernor(allowed=True)
        mg.set_memory_governor(stub)
        m._admission_blocked("j1")
        assert stub.calls == [73.5]

    def test_deferral_log_rate_limited(self, caplog):
        import logging as _logging

        m = self._manager()
        m.jobs["j1"] = _job("j1", "add_golden_repo")
        gov = _StubGovernor(allowed=False)
        gov.band = type("B", (), {"value": "RED"})()
        gov.last_used_pct = 92.0
        mg.set_memory_governor(gov)

        with caplog.at_level(_logging.INFO):
            m._log_admission_deferred("j1")  # first: logs
            m._log_admission_deferred("j1")  # immediately after: suppressed

        defer_lines = [
            r for r in caplog.records if "memory-pressure: deferring" in r.getMessage()
        ]
        assert len(defer_lines) == 1
        assert "band=RED" in defer_lines[0].getMessage()
        assert "op=add_golden_repo" in defer_lines[0].getMessage()


@pytest.mark.slow
class TestPoolWorkerRequeue:
    """The pool worker re-queues a blocked heavy job and runs it once unblocked."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.storage = Path(self.temp_dir) / "jobs.json"
        self.manager = None

    def teardown_method(self):
        if self.manager is not None:
            self.manager.shutdown()
        mg.clear_memory_governor()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_blocked_job_deferred_then_executed_on_recovery(self):
        config = BackgroundJobsConfig(
            max_concurrent_background_jobs=1,
            job_admission_backoff_seconds=0.05,
        )
        self.manager = BackgroundJobManager(
            storage_path=str(self.storage), background_jobs_config=config
        )
        m = self.manager

        executed = []
        m._execute_job = lambda job_id, func, args, kwargs: executed.append(job_id)

        m.jobs["j1"] = _job("j1", "add_golden_repo")
        stub = _StubGovernor(allowed=False)
        stub.band = type("B", (), {"value": "RED"})()
        stub.last_used_pct = 92.0
        mg.set_memory_governor(stub)

        # Enqueue the heavy job directly onto the pool queue.
        m._pending_job_queue.put(("j1", lambda: None, (), {}))

        # While the governor denies, the worker keeps re-queuing — no execution.
        time.sleep(0.3)
        assert executed == []
        assert len(stub.calls) > 0  # the gate was consulted at least once

        # Pod recovers: governor now admits → worker executes exactly once.
        stub.allowed = True
        deadline = time.time() + 2.0
        while not executed and time.time() < deadline:
            time.sleep(0.05)
        assert executed == ["j1"]
