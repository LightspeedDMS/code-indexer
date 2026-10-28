# ^ see tests/unit/server/storage/test_dashboard_cache_reader_threads_2222.py; loaded only through `-p` by that test
import os
import threading
from pathlib import Path

import pytest

from code_indexer.server.storage.filesystem_backends import (
    FilesystemDashboardCacheBackend,
)

LEFTOVER_FILE_ENV = "CIDX_2222_LEFTOVER_FILE"
# ^ coupled with tests/unit/server/storage/test_filesystem_dashboard_cache.py:_running_readers; reader thread names
READER_THREAD_PREFIX = "dashboard-cache-reader-"


def _interrupted_write(self, *args, **kwargs):
    raise RuntimeError("test body interrupted mid-run")


@pytest.fixture(autouse=True)
def _interrupt_dashboard_writes(monkeypatch):
    monkeypatch.setattr(
        FilesystemDashboardCacheBackend, "set_result", _interrupted_write
    )
    monkeypatch.setattr(
        FilesystemDashboardCacheBackend, "set_cached", _interrupted_write
    )


def pytest_sessionfinish(session, exitstatus):
    leftover = [
        t.name
        for t in threading.enumerate()
        if t is not threading.main_thread()
        and t.is_alive()
        and (t.name.startswith(READER_THREAD_PREFIX) or not t.daemon)
    ]
    Path(os.environ[LEFTOVER_FILE_ENV]).write_text("\n".join(leftover))
