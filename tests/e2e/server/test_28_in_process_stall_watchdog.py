"""Phase 3: in-process apps run no worker-stall watchdog.

The stall watchdog (Story S12) is one per uvicorn WORKER process: it re-arms
the process-global faulthandler timer and reports when no Python thread in
that worker ran for 3 s. Phase 3 runs the shared app, any throwaway app a
test builds, the TestClient and the test code in ONE interpreter, so a
watchdog there reports the harness's own GIL use and starvation, and several
watchdogs re-arm and cancel the same process-global timer. The fixtures
that build long-lived in-process apps stop it through its real stop API
(``conftest.stop_in_process_stall_watchdog``); real workers are covered by
Phase 4 (live uvicorn) and the unit tests.
"""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient

_WATCHDOG_THREAD_NAME = "cidx-stall-watchdog"


def test_in_process_session_app_runs_no_stall_watchdog(
    test_client: TestClient,
) -> None:
    # The session fixture installs its app as the module-global app.
    import code_indexer.server.app as app_module

    app = app_module.app
    assert test_client.app is app
    assert getattr(app.state, "stall_watchdog", None) is None
    running = [t.name for t in threading.enumerate() if t.name == _WATCHDOG_THREAD_NAME]
    assert running == [], running
