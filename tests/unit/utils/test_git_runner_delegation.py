"""
run_git_command delegation seam — EVO-64616 Phase 2.

Pins that the ONE chokepoint routes through the installed ``GitInvoker`` for
mutations while reads stay local, with native ``subprocess`` behavior unchanged
when no invoker is installed (the default cidx product). Because both the
``GitBackend`` path and ``GitOperationsService`` bottom out here, this is the
single seam through which *everything* flows.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_indexer.utils import git_runner


class _RecordingInvoker:
    def __init__(self, returncode=0, stdout="delegated-out", stderr=""):
        self.calls = []
        self._rc = returncode
        self._out = stdout
        self._err = stderr

    def run(self, cmd, cwd, *, timeout=None):
        self.calls.append((list(cmd), cwd, timeout))
        return subprocess.CompletedProcess(
            args=list(cmd), returncode=self._rc, stdout=self._out, stderr=self._err
        )


@pytest.fixture
def no_native_subprocess(monkeypatch):
    """Fail loudly if the native subprocess path is taken when we expect delegation."""
    sentinel = {"called": False}

    def _boom(*a, **k):
        sentinel["called"] = True
        raise AssertionError("native subprocess.run should NOT have run")

    monkeypatch.setattr(git_runner.subprocess, "run", _boom)
    return sentinel


def _install(monkeypatch, invoker):
    monkeypatch.setattr(git_runner, "get_git_invoker", lambda: invoker)


def test_mutation_is_delegated_to_invoker(monkeypatch, no_native_subprocess):
    inv = _RecordingInvoker(stdout="committed")
    _install(monkeypatch, inv)
    result = git_runner.run_git_command(["git", "commit", "-m", "x"], Path("/repo"))
    assert len(inv.calls) == 1
    assert inv.calls[0][0] == ["git", "commit", "-m", "x"]
    assert result.stdout == "committed"
    assert result.returncode == 0


def test_read_stays_native_even_with_invoker_installed(monkeypatch):
    inv = _RecordingInvoker()
    _install(monkeypatch, inv)
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="native-status", stderr="")

    monkeypatch.setattr(git_runner.subprocess, "run", _fake_run)
    result = git_runner.run_git_command(["git", "status", "--porcelain"], Path("/repo"))
    assert inv.calls == []  # invoker NOT consulted for a read
    assert captured["cmd"] == ["git", "status", "--porcelain"]
    assert result.stdout == "native-status"


def test_no_invoker_runs_native(monkeypatch):
    _install(monkeypatch, None)
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(git_runner.subprocess, "run", _fake_run)
    git_runner.run_git_command(["git", "commit", "-m", "x"], Path("/repo"))
    assert captured["cmd"] == ["git", "commit", "-m", "x"]  # native even for a mutation


def test_delegated_nonzero_exit_raises_when_check(monkeypatch, no_native_subprocess):
    _install(monkeypatch, _RecordingInvoker(returncode=1, stderr="push rejected"))
    with pytest.raises(subprocess.CalledProcessError) as exc:
        git_runner.run_git_command(["git", "push", "origin", "HEAD"], Path("/repo"))
    assert exc.value.returncode == 1
    assert exc.value.stderr == "push rejected"


def test_delegated_nonzero_exit_no_raise_when_check_false(
    monkeypatch, no_native_subprocess
):
    _install(monkeypatch, _RecordingInvoker(returncode=2, stderr="boom"))
    result = git_runner.run_git_command(["git", "push"], Path("/repo"), check=False)
    assert result.returncode == 2  # returned, not raised


def test_non_git_command_still_rejected(monkeypatch):
    _install(monkeypatch, _RecordingInvoker())
    with pytest.raises(ValueError):
        git_runner.run_git_command(["ls", "-la"], Path("/repo"))


def test_retry_variant_also_delegates_mutations(monkeypatch, no_native_subprocess):
    # Regression (MR !36 review): run_git_command_with_retry must route through
    # the same seam -- network mutations (fetch/pull/push) use the retry path, so
    # a bypass there would leave the most important delegations running locally.
    inv = _RecordingInvoker(stdout="fetched")
    _install(monkeypatch, inv)
    result = git_runner.run_git_command_with_retry(
        ["git", "fetch", "origin"], Path("/repo")
    )
    assert len(inv.calls) == 1
    assert inv.calls[0][0] == ["git", "fetch", "origin"]
    assert result.stdout == "fetched"


def test_retry_variant_read_stays_native(monkeypatch):
    inv = _RecordingInvoker()
    _install(monkeypatch, inv)
    calls = {"n": 0}

    def _fake_run(cmd, **kwargs):
        calls["n"] += 1
        return subprocess.CompletedProcess(cmd, 0, stdout="native", stderr="")

    monkeypatch.setattr(git_runner.subprocess, "run", _fake_run)
    git_runner.run_git_command_with_retry(["git", "ls-remote", "url"], Path("/repo"))
    assert inv.calls == []  # read: invoker not consulted even on the retry path
    assert calls["n"] == 1
