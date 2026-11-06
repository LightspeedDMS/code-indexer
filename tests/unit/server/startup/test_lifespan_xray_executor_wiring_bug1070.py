"""Bug #1070 regression guard: X-Ray compute runs on a dedicated executor.

Bug #1070: handle_xray_search and handle_xray_explore submitted to
BackgroundJobManager's 5-worker pool, causing starvation and 504s. The fix: a
dedicated ThreadPoolExecutor sized by ``xray_max_concurrent_jobs`` (20),
installed where the handlers read it, shut down on exit.

Since modular plan phase 4c the cidx-xray feature's on_startup builds it
(``cidx_xray/server/lifecycle.py``), not the core's lifespan; its size comes
from the core's background-jobs config through the server platform. The
executable proof is tests/unit/xray/test_xray_feature_lifecycle.py; these
source guards pin the pieces.
"""

from __future__ import annotations

import inspect


def test_the_feature_startup_builds_and_installs_the_executor() -> None:
    from cidx_xray.server import lifecycle

    source = inspect.getsource(lifecycle)
    assert "platform.xray_executor_pool_size()" in source
    assert "executor = ThreadPoolExecutor(" in source
    assert "runtime.EXECUTOR.install(executor)" in source


def test_the_executor_is_shut_down_when_the_feature_stops() -> None:
    from cidx_xray.server import lifecycle

    source = inspect.getsource(lifecycle._shutdown_executor)
    assert "executor.shutdown(" in source
    assert "runtime.EXECUTOR.uninstall()" in source


def test_the_pool_is_sized_by_xray_max_concurrent_jobs() -> None:
    from code_indexer.server.feature_platform import ServerPlatform
    from code_indexer.server.utils.config_manager import BackgroundJobsConfig

    expected = BackgroundJobsConfig().xray_max_concurrent_jobs
    assert ServerPlatform().xray_executor_pool_size() == expected == 20


def test_the_core_lifespan_builds_no_xray_executor() -> None:
    from code_indexer.server.startup import lifespan

    assert "_xray_executor" not in inspect.getsource(lifespan)
