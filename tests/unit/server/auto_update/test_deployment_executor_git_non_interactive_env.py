"""Epic #2103 item 18: every network git call made by the auto-updater's
DeploymentExecutor runs with the shared non-interactive git environment
(``build_non_interactive_git_env``): git's own credential prompt disabled
and SSH in BatchMode, so a deploy can never block on a prompt.

The environment is captured at the real call sites: a fake ``git`` (and
``sudo``, used by the submodule cleanup) placed first on PATH records the
environment and arguments it was launched with. No subprocess call is
patched.
"""

import ast
from pathlib import Path
from typing import Dict, List, Set

import pytest

from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor

_FAKE_GIT = """#!/bin/sh
printf '%s|%s|%s\\n' "${GIT_TERMINAL_PROMPT:-unset}" "${GIT_SSH_COMMAND:-unset}" "$*" >> "$FAKE_GIT_LOG"
case "$1" in
  submodule)
    count_file="$FAKE_GIT_LOG.submodule"
    count=$(cat "$count_file" 2>/dev/null || echo 0)
    echo $((count + 1)) > "$count_file"
    if [ "$count" = "0" ] && [ -n "$FAKE_GIT_FIRST_SUBMODULE_FAILS" ]; then
      echo "fatal: could not lock config file" >&2
      exit 1
    fi
    ;;
esac
exit 0
"""

_FAKE_SUDO = """#!/bin/sh
exit 0
"""


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


@pytest.fixture
def fake_git_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a recording fake git/sudo first on PATH; return the log path."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "git", _FAKE_GIT)
    _write_executable(bin_dir / "sudo", _FAKE_SUDO)
    log = tmp_path / "git-calls.log"
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_GIT_LOG", str(log))
    # The parent environment must not already carry the settings under
    # test, or an inherited environment would pass for the shared one.
    monkeypatch.delenv("GIT_TERMINAL_PROMPT", raising=False)
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    return log


def _calls(log: Path, subcommand: str) -> List[Dict[str, str]]:
    calls = []
    for line in log.read_text().splitlines():
        prompt, ssh_command, args = line.split("|", 2)
        if subcommand in args.split():
            calls.append({"prompt": prompt, "ssh": ssh_command, "args": args})
    return calls


def _assert_non_interactive(calls: List[Dict[str, str]]) -> None:
    assert calls, "the git call under test was never made"
    for call in calls:
        assert call["prompt"] == "0", call
        assert "BatchMode=yes" in call["ssh"], call


def _pace_maker_executor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An executor whose pace-maker clone lives under ``tmp_path`` and whose
    install.sh fails, so the method stops right after the git step."""
    monkeypatch.setenv("HOME", str(tmp_path))
    executor = DeploymentExecutor(
        repo_path=tmp_path, service_name="cidx-test-no-such-unit"
    )
    clone_path = tmp_path / "claude-pace-maker"
    return executor, clone_path


class TestSubmoduleUpdateUsesNonInteractiveEnv:
    def test_submodule_update_first_attempt_runs_non_interactive(
        self, tmp_path: Path, fake_git_log: Path
    ) -> None:
        executor = DeploymentExecutor(repo_path=tmp_path)

        assert executor.git_submodule_update() is True

        calls = _calls(fake_git_log, "submodule")
        assert len(calls) == 1
        _assert_non_interactive(calls)

    def test_submodule_update_retry_after_cleanup_runs_non_interactive(
        self,
        tmp_path: Path,
        fake_git_log: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("FAKE_GIT_FIRST_SUBMODULE_FAILS", "1")
        executor = DeploymentExecutor(repo_path=tmp_path)

        assert executor.git_submodule_update() is True

        calls = _calls(fake_git_log, "submodule")
        assert len(calls) == 2, "first attempt and the retry"
        _assert_non_interactive(calls)


class TestLocalGitCallsUseNonInteractiveEnv:
    def test_safe_directory_listing_and_add_run_non_interactive(
        self, tmp_path: Path, fake_git_log: Path
    ) -> None:
        from code_indexer.server.auto_update.deployment_executor import (
            ensure_single_safe_directory,
        )

        assert ensure_single_safe_directory(str(tmp_path / "repo")) is None

        calls = _calls(fake_git_log, "safe.directory")
        assert len(calls) == 2, "listing, then add"
        _assert_non_interactive(calls)

    def test_submodule_commit_lookup_runs_non_interactive(
        self, tmp_path: Path, fake_git_log: Path
    ) -> None:
        executor = DeploymentExecutor(repo_path=tmp_path)

        executor._get_hnswlib_submodule_commit()

        calls = _calls(fake_git_log, "ls-files")
        assert len(calls) == 1
        _assert_non_interactive(calls)


def _git_argv_names(func: ast.AST) -> Set[str]:
    """Names assigned a git argv (a list holding "git" or *git) in *func*."""
    names: Set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and _is_git_argv(node.value, set()):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _is_git_argv(node: ast.AST, git_names: Set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in git_names
    if not isinstance(node, ast.List):
        return False
    for element in node.elts:
        if isinstance(element, ast.Constant) and element.value == "git":
            return True
        if isinstance(element, ast.Starred) and isinstance(element.value, ast.Name):
            if element.value.id in ("git", *git_names):
                return True
    return False


# safe.directory listing and add/replace, pull, two submodule updates,
# hnswlib clone, ls-files, pace-maker clone and pull.
_KNOWN_GIT_CALLS = 9


def test_every_git_subprocess_call_passes_the_non_interactive_env() -> None:
    """Static enumeration of every git subprocess call in the module."""
    import code_indexer.server.auto_update.deployment_executor as module

    source = Path(module.__file__).read_text()
    tree = ast.parse(source)
    git_calls = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        git_names = _git_argv_names(func)
        for call in ast.walk(func):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "subprocess"
                and call.args
                and _is_git_argv(call.args[0], git_names)
            ):
                git_calls.append(call)

    assert len(git_calls) >= _KNOWN_GIT_CALLS, [
        ast.get_source_segment(source, c) for c in git_calls
    ]
    for call in git_calls:
        env = [k for k in call.keywords if k.arg == "env"]
        assert env and ast.get_source_segment(source, env[0].value) == (
            "build_non_interactive_git_env()"
        ), ast.get_source_segment(source, call)


class TestPaceMakerGitUsesNonInteractiveEnv:
    def test_pace_maker_fresh_clone_runs_non_interactive(
        self,
        tmp_path: Path,
        fake_git_log: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        executor, clone_path = _pace_maker_executor(tmp_path, monkeypatch)

        # install.sh is absent, so the method returns False after the clone.
        assert executor._ensure_pace_maker_installed() is False

        calls = _calls(fake_git_log, "clone")
        assert len(calls) == 1
        assert str(clone_path) in calls[0]["args"]
        _assert_non_interactive(calls)

    def test_pace_maker_update_pull_runs_non_interactive(
        self,
        tmp_path: Path,
        fake_git_log: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        executor, clone_path = _pace_maker_executor(tmp_path, monkeypatch)
        (clone_path / ".git").mkdir(parents=True)

        assert executor._ensure_pace_maker_installed() is False

        calls = _calls(fake_git_log, "pull")
        assert len(calls) == 1
        assert str(clone_path) in calls[0]["args"]
        _assert_non_interactive(calls)
