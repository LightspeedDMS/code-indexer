import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit.server.storage._interrupt_dashboard_writes_plugin import (
    LEFTOVER_FILE_ENV,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_TARGET = "tests/unit/server/storage/test_filesystem_dashboard_cache.py"
_READER_TESTS = (
    f"{_TARGET}::test_atomic_write_readers_see_complete_payload",
    f"{_TARGET}::test_concurrent_set_cached_atomic",
)
_HARD_TIMEOUT_SECONDS = 60


@pytest.mark.slow
def test_interrupted_reader_tests_leave_no_thread_behind(tmp_path):
    leftover_file = tmp_path / "leftover_threads.txt"
    env = {
        **os.environ,
        "PYTHONPATH": str(_REPO_ROOT / "src"),
        LEFTOVER_FILE_ENV: str(leftover_file),
    }
    try:
        child = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-W",
                "ignore",
                "-p",
                "tests.unit.server.storage._interrupt_dashboard_writes_plugin",
                *_READER_TESTS,
            ],
            cwd=str(_REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=_HARD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            "interrupted reader tests left threads running: the child pytest "
            f"did not exit within {_HARD_TIMEOUT_SECONDS}s"
        )

    output = child.stdout[-3000:] + child.stderr[-2000:]
    assert child.returncode == 1, output
    assert "2 failed" in child.stdout, output
    assert "test body interrupted mid-run" in child.stdout
    assert leftover_file.read_text() == ""
