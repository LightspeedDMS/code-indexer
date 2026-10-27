import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LOOP_PATCHING_FILE = "tests/unit/server/mcp/test_xray_multi_repo_bug1074.py"
_TESTCLIENT_TEST = (
    "tests/unit/server/routes/test_xray_routes.py"
    "::TestValidRequest::test_post_xray_search_returns_202_with_job_id"
)
_HARD_TIMEOUT_SECONDS = 60


@pytest.mark.slow
def test_testclient_test_completes_after_loop_patching_tests_in_one_process():
    env = {**os.environ, "PYTHONPATH": str(_REPO_ROOT / "src")}
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
                _LOOP_PATCHING_FILE,
                _TESTCLIENT_TEST,
            ],
            cwd=str(_REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=_HARD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"TestClient test hung after {_LOOP_PATCHING_FILE} "
            f"(no result within {_HARD_TIMEOUT_SECONDS}s)"
        )

    summary = child.stdout.strip().splitlines()[-1:] or [""]
    assert child.returncode == 0, child.stdout[-3000:] + child.stderr[-2000:]
    assert " failed" not in summary[0]
    assert " passed" in summary[0]
