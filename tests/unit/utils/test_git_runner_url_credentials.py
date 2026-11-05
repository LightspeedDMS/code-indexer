"""Invariant: a credential embedded in a git URL argument never leaves
run_git_command in an exception -- not in the command it carries (which
``str(error)`` prints), nor in the output git echoes back -- whether git ran
here or through the installed GitInvoker. The caller's own argv is untouched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_indexer.utils import git_runner

SECRET = "Zt4urlEmbeddedCloneSecret88"
# Port 9 (discard) on loopback: the clone fails fast with connection refused.
CRED_URL = f"https://example-user:{SECRET}@127.0.0.1:9/example-repo.git"


def _assert_no_secret(error: subprocess.CalledProcessError) -> None:
    assert SECRET not in str(error)
    assert SECRET not in " ".join(str(arg) for arg in error.cmd)
    assert SECRET not in str(error.stderr or "")
    assert SECRET not in str(error.output or "")


def test_native_clone_failure_hides_the_url_credential(tmp_path: Path) -> None:
    cmd = ["git", "clone", CRED_URL, str(tmp_path / "dest")]
    with pytest.raises(subprocess.CalledProcessError) as raised:
        git_runner.run_git_command(cmd, cwd=tmp_path, timeout=30)
    _assert_no_secret(raised.value)
    assert "127.0.0.1:9/example-repo.git" in " ".join(raised.value.cmd)
    assert cmd[2] == CRED_URL, "the caller's argv must not be rewritten"


class _FailingInvoker:
    def run(self, cmd, cwd, *, timeout=None):
        return subprocess.CompletedProcess(
            args=list(cmd),
            returncode=128,
            stdout=f"pushing to {CRED_URL}",
            stderr=f"fatal: unable to access '{CRED_URL}/': refused",
        )


def test_delegated_failure_hides_the_url_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(git_runner, "get_git_invoker", lambda: _FailingInvoker())
    with pytest.raises(subprocess.CalledProcessError) as raised:
        git_runner.run_git_command(["git", "push", CRED_URL, "HEAD"], Path("/repo"))
    _assert_no_secret(raised.value)
    assert "unable to access" in raised.value.stderr
