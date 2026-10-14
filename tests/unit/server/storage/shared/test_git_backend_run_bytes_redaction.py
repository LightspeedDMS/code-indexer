"""LocalGitBackend._run_bytes: a failure's streams are redacted.

git echoes a credentialed remote URL back on failure, and a
CalledProcessError's output/stderr travel into job errors, API responses and
logs. A success keeps stdout as the exact bytes git produced.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from code_indexer.server.storage.shared import git_backend
from code_indexer.server.storage.shared.git_backend import LocalGitBackend

SECRET_URL = "https://oauth2:glpat-SECRET@gitlab.com/o/r.git"


def _fake_run(returncode: int, stdout: bytes, stderr: bytes) -> Any:
    def run(cmd: Any, *, cwd: Any, check: bool, timeout: Any, text: bool = True) -> Any:
        assert text is False and check is False
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    return run


def test_failure_redacts_stdout_and_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        git_backend,
        "run_git_command",
        _fake_run(
            128,
            f"remote: {SECRET_URL}\n".encode(),
            f"fatal: unable to access '{SECRET_URL}/': 403\n".encode(),
        ),
    )
    with pytest.raises(subprocess.CalledProcessError) as info:
        LocalGitBackend._run_bytes(["git", "show", "HEAD:a"], Path("/r"))
    err = info.value
    assert err.returncode == 128
    assert isinstance(err.output, bytes) and isinstance(err.stderr, str)
    assert "glpat-SECRET" not in err.output.decode() + err.stderr
    assert "gitlab.com/o/r.git" in err.stderr


def test_failure_keeps_every_non_credential_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redaction touches only the credential: undecodable bytes around it
    survive the failure path unchanged (no U+FFFD substitution)."""
    stdout = b"\x00\xff\xfe bin " + SECRET_URL.encode() + b" \x80tail\n"
    monkeypatch.setattr(git_backend, "run_git_command", _fake_run(1, stdout, b""))
    with pytest.raises(subprocess.CalledProcessError) as info:
        LocalGitBackend._run_bytes(["git", "show", "HEAD:a"], Path("/r"))
    assert info.value.output == (
        b"\x00\xff\xfe bin https://***@gitlab.com/o/r.git \x80tail\n"
    )


def test_success_keeps_exact_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    blob = b"\x00\xff binary " + SECRET_URL.encode()
    monkeypatch.setattr(git_backend, "run_git_command", _fake_run(0, blob, b"warn\n"))
    result = LocalGitBackend._run_bytes(["git", "show", "HEAD:a"], Path("/r"))
    assert result.stdout == blob
    assert result.stderr == "warn\n"


def test_unchecked_failure_returns_raw_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(git_backend, "run_git_command", _fake_run(1, b"out", b"err"))
    result = LocalGitBackend._run_bytes(
        ["git", "show", "HEAD:a"], Path("/r"), check=False
    )
    assert (result.returncode, result.stdout, result.stderr) == (1, b"out", "err")
