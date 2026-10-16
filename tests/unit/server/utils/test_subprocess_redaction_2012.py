"""Bug #2012 follow-up: a cancellable subprocess's argv and captured output
must never carry credentials into exceptions or logs.

Real child processes only; the child receives the real (unredacted)
arguments -- only what leaves the process is redacted.
"""

import logging
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from code_indexer.server.logging_utils import redact_command
from code_indexer.server.utils.cancellable_subprocess import (
    SubprocessCancelledError,
    run_with_cancel,
)

SECRET = "secret-token"
URL = f"https://user:{SECRET}@example.com/repo.git"
MASKED_URL = "https://***@example.com/repo.git"
# The child echoes its credential-bearing args to stdout and stderr, then
# sleeps; its stderr line starts with "ERROR:" while it is still running.
_ECHO_AND_SLEEP = (
    "import sys, time; a = ' '.join(sys.argv[1:]); "
    "print(a, flush=True); sys.stderr.write('ERROR: fetch ' + a + '\\n'); "
    "sys.stderr.flush(); time.sleep(120)"
)


def _argv() -> list:
    return [sys.executable, "-c", _ECHO_AND_SLEEP, URL, "--token", SECRET]


def _assert_clean(text: str) -> None:
    assert SECRET not in text, f"secret leaked: {text!r}"
    assert "user:" not in text, f"URL userinfo leaked: {text!r}"


def test_cancelled_command_message_is_redacted() -> None:
    started = time.monotonic()
    with pytest.raises(SubprocessCancelledError) as raised:
        run_with_cancel(
            _argv(),
            lambda: time.monotonic() - started > 0.5,
            capture_output=True,
            text=True,
        )
    message = str(raised.value)
    _assert_clean(message)
    assert MASKED_URL in message and "--token" in message


def test_timed_out_command_cmd_and_output_are_redacted() -> None:
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        run_with_cancel(
            _argv(), lambda: False, capture_output=True, text=True, timeout=1
        )
    _assert_clean(repr(raised.value.cmd))
    _assert_clean(str(raised.value))
    output = str(raised.value.output or "")  # text mode: always str
    _assert_clean(output)
    _assert_clean(str(raised.value.stderr or ""))
    assert MASKED_URL in output


def test_failed_checked_command_is_redacted() -> None:
    script = "import sys; print(' '.join(sys.argv[1:])); sys.exit(2)"
    with pytest.raises(subprocess.CalledProcessError) as raised:
        run_with_cancel(
            [sys.executable, "-c", script, URL, "--token", SECRET],
            lambda: False,
            capture_output=True,
            text=True,
            check=True,
        )
    _assert_clean(str(raised.value))
    _assert_clean(raised.value.output or "")


def test_error_line_logged_while_running_is_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR)
    started = time.monotonic()
    with pytest.raises(SubprocessCancelledError):
        run_with_cancel(
            _argv(),
            lambda: time.monotonic() - started > 1.0,
            capture_output=True,
            text=True,
        )
    # The parent's own record ("Subprocess ... emitted an ERROR-level stderr
    # line while still running: <line>") embeds the child's "ERROR: fetch"
    # line together with the command.
    records = [
        r.getMessage()
        for r in caplog.records
        if "emitted an ERROR-level" in r.getMessage()
    ]
    assert records, "the child's ERROR line must still be logged"
    for message in records:
        assert "ERROR: fetch" in message
        _assert_clean(message)
    assert any(MASKED_URL in m for m in records)


def test_normal_commands_keep_their_shape() -> None:
    assert redact_command(["git", "fetch", "origin"]) == ["git", "fetch", "origin"]
    assert redact_command(["git", "clone", URL, "/tmp/x"]) == [
        "git",
        "clone",
        MASKED_URL,
        "/tmp/x",
    ]
    assert redact_command(["cmd", "--password=hunter2", "API_KEY=abc", "-v"]) == [
        "cmd",
        "--password=***",
        "API_KEY=***",
        "-v",
    ]


def test_refresh_cancel_log_line_is_redacted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from code_indexer.config import ConfigManager
    from code_indexer.global_repos.cleanup_manager import CleanupManager
    from code_indexer.global_repos.query_tracker import QueryTracker
    from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

    golden = tmp_path / "golden_repos"
    golden.mkdir()
    tracker = QueryTracker()
    scheduler = RefreshScheduler(
        golden_repos_dir=str(golden),
        config_source=ConfigManager(tmp_path / ".code-indexer" / "config.json"),
        query_tracker=tracker,
        cleanup_manager=CleanupManager(tracker),
    )
    caplog.set_level(logging.INFO)
    with patch.object(
        scheduler.alias_manager,
        "read_alias",
        side_effect=SubprocessCancelledError(f"git fetch {URL} cancelled"),
    ):
        with pytest.raises(SubprocessCancelledError):
            scheduler._execute_refresh("example-repo-global")
    lines = [
        r.getMessage() for r in caplog.records if "Refresh cancelled" in r.getMessage()
    ]
    assert lines, "the cancellation must still be logged"
    for line in lines:
        _assert_clean(line)
