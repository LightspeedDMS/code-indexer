"""Invariant: with a cancel check set, a git command's output is masked for
the supplied credential everywhere it leaves the runner -- the lines logged
while git is still running, the returned output and any raised exception.

The secret is echoed bare (no ``key=value`` shape), so only the supplied
secret redactor can mask it.
"""

import logging
import subprocess
from pathlib import Path
from typing import List

import pytest

from code_indexer.utils.git_runner import run_git_command

SECRET = "Qv7unstructuredPushSecret41"
CRED_URL = f"https://example-user:{SECRET}@git.example.com/example-repo.git"


def _echo_alias(exit_code: int, sleep_seconds: int = 0) -> List[tuple]:
    body = (
        "!f() { "
        'echo "ERROR remote said $CIDX_GIT_REMOTE_PASSWORD"; '
        'echo "ERROR remote said $CIDX_GIT_REMOTE_PASSWORD" >&2; '
        f"sleep {sleep_seconds}; "
        f"exit {exit_code}; "
        "}; f"
    )
    return [("alias.echo-secret", body)]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


def _assert_no_secret_logged(caplog: pytest.LogCaptureFixture) -> None:
    assert caplog.records, "the ERROR-level output line must still be logged"
    for record in caplog.records:
        assert SECRET not in record.getMessage()


def test_streamed_and_returned_output_masks_supplied_secret(
    repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    result = run_git_command(
        ["git", "echo-secret"],
        cwd=repo,
        check=False,
        credentials_url=CRED_URL,
        run_time_config=_echo_alias(0),
        cancel_check=lambda: False,
    )
    assert "ERROR remote said" in result.stdout
    assert SECRET not in result.stdout
    assert SECRET not in result.stderr
    _assert_no_secret_logged(caplog)


def test_failed_command_exception_masks_supplied_secret(
    repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(subprocess.CalledProcessError) as raised:
        run_git_command(
            ["git", "echo-secret"],
            cwd=repo,
            check=True,
            credentials_url=CRED_URL,
            run_time_config=_echo_alias(3),
            cancel_check=lambda: False,
        )
    error = raised.value
    assert SECRET not in str(error.output)
    assert SECRET not in str(error.stderr)
    assert SECRET not in str(error)
    _assert_no_secret_logged(caplog)


def test_timed_out_command_exception_masks_supplied_secret(
    repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        run_git_command(
            ["git", "echo-secret"],
            cwd=repo,
            check=False,
            timeout=1,
            credentials_url=CRED_URL,
            run_time_config=_echo_alias(0, sleep_seconds=5),
            cancel_check=lambda: False,
        )
    error = raised.value
    assert SECRET not in str(error.output)
    assert SECRET not in str(error.stderr)
    assert SECRET not in str(error)
    _assert_no_secret_logged(caplog)
