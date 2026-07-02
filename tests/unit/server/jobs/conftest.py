"""
Test fixtures for tests/unit/server/jobs/.

Provides autouse infrastructure to prevent real system resource pressure
(CPU/memory) from activating SyncJobManager degraded mode during tests.

Root cause of flakiness:
    SyncJobManager._get_effective_concurrency_limits() queries real psutil
    metrics.  When CPU > degraded_mode_cpu_threshold (default 70%) the
    effective total-concurrent limit switches from the test-specified value
    (e.g. max_total=2) to degraded_max_total_concurrent_jobs (default 3).
    Running inside the large Chunk-6 server test suite regularly pushes CPU
    above 70%, so all 3 test jobs start immediately and the "queued" assertion
    fires on a "running" job.

Fix strategy:
    Patch the external psutil calls so SyncJobManager._get_resource_metrics()
    always receives 0% CPU and 0% memory.  SyncJobManager.is_in_degraded_mode()
    executes its real logic against these controlled inputs, ensuring the
    effective concurrency limits always match what each test explicitly
    configured.
"""

import pytest
from unittest.mock import MagicMock


@pytest.fixture(autouse=True)
def _stable_resource_metrics(monkeypatch):
    """Return zero CPU/memory from psutil so degraded mode never activates.

    SyncJobManager._get_resource_metrics() calls psutil.cpu_percent() and
    psutil.virtual_memory().  Under a heavy CI/test-suite load those can
    exceed the 70% degraded-mode threshold, silently changing the effective
    concurrency limits from the test-specified values to the degraded
    defaults.  Patching the external psutil calls (not the SUT) gives the
    manager real, deterministic inputs to reason about.

    This fixture is autouse so it applies to every test in this directory.
    """
    import code_indexer.server.jobs.manager as manager_module

    virtual_mem = MagicMock()
    virtual_mem.percent = 0.0

    monkeypatch.setattr(manager_module.psutil, "cpu_percent", lambda interval=None: 0.0)
    monkeypatch.setattr(manager_module.psutil, "virtual_memory", lambda: virtual_mem)
