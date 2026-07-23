"""
Tests for Bug #1251: Auto-updater no-sudo pip path has no TMPDIR -> "No usable
temporary directory" on PrivateTmp + py3.12 (the #1245 no-sudo fix uncovered
#1243's sudo-only TMPDIR).

Root cause: Bug #1243 set TMPDIR ONLY on the SUDO pip command
(["sudo", "env", f"TMPDIR={tmpdir}", python, "-m", "pip", "install", ...]).
Bug #1245's writability fix routes editable-home installs (the staging
cluster) through the NO-SUDO command ([python, "-m", "pip", "install", ...]),
which carries no TMPDIR at all. Under systemd PrivateTmp=yes + Python 3.12,
the auto-updater's private /tmp is isolated and unusable, so pip's
tempfile.mkdtemp() -> tempfile.gettempdir() raises:

    FileNotFoundError: No usable temporary directory found in
    ['/tmp', '/var/tmp', '/usr/tmp', '/']

at the pybind11/hnswlib build step -> [DEPLOY-GENERAL-044] -> the
auto-updater dead-loops (same self-perpetuating class as #1182/#1243/#1245:
the broken auto-updater cannot deploy its own fix).

Fix: DeploymentExecutor.execute() sets os.environ["TMPDIR"] =
self._deploy_tmpdir() at the very start, BEFORE any subprocess is spawned.
None of the NO-SUDO subprocess.run() calls anywhere in this module pass an
explicit env= override that excludes os.environ -- every call either omits
env= entirely (which inherits the live process environment as found in
os.environ AT CALL TIME) or explicitly builds env from os.environ.copy() /
dict(os.environ) (build_non_interactive_git_env(), the self-restart smoke
test, the pace-maker no-sudo install, the Rust toolchain install + cargo
build). So this single early mutation propagates TMPDIR to every no-sudo
child process without needing any per-call-site change.

The SUDO path is UNCHANGED: it keeps the explicit
["sudo", "env", f"TMPDIR={tmpdir}", ...] prefix from Bug #1243, because
sudo's env_reset strips inherited environment variables -- a plain
os.environ mutation in the parent process would never reach a sudo'd child.

Mocking strategy: subprocess.run is the only external boundary mocked. The
capturing dispatch records both the command list AND os.environ.get(
"TMPDIR") at the moment of each call -- exactly what a real env=None
subprocess.run call hands to its child process. All of execute()'s non-pip
steps (the Step 1.1 self-restart hash guard, Step 1.5 git_submodule_update,
and Step 3 through Step 16) are mocked at the method level via
patch.object(), matching the established pattern in
test_deployment_executor_python.py::TestExecuteCallsEnsureAutoUpdater. This
lets build_custom_hnswlib() and pip_install() run for REAL inside the REAL
execute() so the env-var-setting fix under test is genuinely exercised
end-to-end, not just unit-tested in isolation.

monkeypatch.delenv("TMPDIR", raising=False) (not manual save/restore) is
used so pytest restores the real environment automatically after every test,
even on failure -- avoiding any TMPDIR leakage across the test session.

These tests MUST FAIL against the pre-fix code (no TMPDIR on the no-sudo
path) and PASS after the fix.
"""

import contextlib
import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import code_indexer.server.auto_update.deployment_executor as _de_mod
from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def executor(tmp_path: Path) -> DeploymentExecutor:
    """DeploymentExecutor with a temp repo path."""
    return DeploymentExecutor(
        repo_path=tmp_path,
        branch="master",
        service_name="cidx-server",
    )


@pytest.fixture()
def patched_data_dir(tmp_path: Path) -> Path:  # type: ignore[misc]
    """Patch _cidx_data_dir to tmp_path/.cidx-server so _deploy_tmpdir() writes inside tmp_path."""
    data_dir = tmp_path / ".cidx-server"
    with patch.object(_de_mod, "_cidx_data_dir", data_dir):
        yield data_dir


@pytest.fixture()
def no_ambient_tmpdir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear TMPDIR from the test process env so every test starts from the
    same "PrivateTmp isolated, no TMPDIR set" state the bug describes.
    monkeypatch restores the original value automatically after the test,
    so TMPDIR never leaks across the test session.
    """
    monkeypatch.delenv("TMPDIR", raising=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_hnswlib_path(tmp_path: Path) -> Path:
    """Create a minimal third_party/hnswlib directory with setup.py."""
    hnswlib_path = tmp_path / "third_party" / "hnswlib"
    hnswlib_path.mkdir(parents=True)
    (hnswlib_path / "setup.py").write_text("# setup")
    return hnswlib_path


# execute() steps that are NOT under test here: mocked to a no-op success so
# the real build_custom_hnswlib() / pip_install() calls are the only
# subprocess-spawning behavior actually exercised by execute().
_NOOP_EXECUTE_STEPS: dict = {
    "git_pull": True,
    "_calculate_auto_update_hash": "fixed-hash-1251",
    "git_submodule_update": True,
    "_ensure_launch_config": None,
    "_ensure_cidx_repo_root": True,
    "_ensure_git_safe_directory": True,
    "_ensure_git_safe_directory_wildcard": True,
    "_ensure_auto_updater_uses_server_python": True,
    "_ensure_data_dir_env_var": True,
    "_ensure_malloc_arena_max": True,
    "_ensure_codex_cli_installed": True,
    "ensure_ripgrep": True,
    "_ensure_sudoers_restart": True,
    "_ensure_memory_overcommit": True,
    "_ensure_swap_file": True,
    "_ensure_claude_cli_updated": True,
    "_ensure_pace_maker_installed": True,
    "_ensure_claude_cli_installed": True,
    "_ensure_nfs_research_symlinks": True,
    "_ensure_activated_repos_symlink_for_cow_daemon": True,
    "_ensure_daemon_storage_path": True,
    "_ensure_systemd_claude_path": True,
    "_ensure_rust_toolchain": True,
}


def _run_execute_with_real_pip_steps(
    executor: DeploymentExecutor, tmp_path: Path
) -> tuple:
    """Run the REAL DeploymentExecutor.execute() with build_custom_hnswlib()
    and pip_install() exercised for REAL (only subprocess.run is mocked), and
    every other execute() step mocked to a no-op success. This keeps the test
    hermetic and fast while genuinely exercising the TMPDIR-propagation fix
    under test (the fix lives in execute(), so calling the sub-methods
    directly would NOT exercise it).

    _is_user_install is forced True (the no-sudo / editable-home layout that
    Bug #1245 routes through the broken no-TMPDIR command shape).

    Returns (result, calls, tmpdir_at_call):
        result: execute()'s return value.
        calls: list of every subprocess.run command list invoked, in order.
        tmpdir_at_call: {tuple(cmd): os.environ.get("TMPDIR")} captured for
            the 3 no-sudo-relevant commands (pybind11 install, hnswlib
            --force-reinstall install, pip install -e .) -- the ambient
            TMPDIR a real env=None subprocess.run call would hand to the
            child process at the moment each command was dispatched.
    """
    _make_hnswlib_path(tmp_path)
    calls: list = []
    tmpdir_at_call: dict = {}

    def dispatch(cmd: list, **_kw: object) -> Mock:
        calls.append(list(cmd))
        if "-m" in cmd and "pip" in cmd and "--version" in cmd:
            return Mock(
                returncode=0,
                stdout="pip 23.1 from /path (python 3.9)\n",
                stderr="",
            )
        is_pybind11 = "pybind11" in cmd
        is_hnswlib_build = "--force-reinstall" in cmd
        is_pip_install_dash_e = "-e" in cmd and ".[cluster]" in cmd
        if is_pybind11 or is_hnswlib_build or is_pip_install_dash_e:
            tmpdir_at_call[tuple(cmd)] = os.environ.get("TMPDIR")
        return Mock(returncode=0, stdout="", stderr="")

    with contextlib.ExitStack() as stack:
        for method_name, value in _NOOP_EXECUTE_STEPS.items():
            stack.enter_context(patch.object(executor, method_name, return_value=value))
        stack.enter_context(
            patch.object(
                executor, "_get_server_python", return_value="/usr/bin/python3"
            )
        )
        stack.enter_context(
            patch.object(executor, "_ensure_build_dependencies", return_value=True)
        )
        stack.enter_context(
            patch.object(executor, "_hnswlib_importable", return_value=False)
        )
        stack.enter_context(
            patch.object(executor, "_get_hnswlib_submodule_commit", return_value=None)
        )
        stack.enter_context(
            patch.object(executor, "_get_last_built_hnswlib_commit", return_value=None)
        )
        stack.enter_context(
            patch.object(executor, "_is_user_install", return_value=True)
        )
        stack.enter_context(patch("subprocess.run", side_effect=dispatch))
        result = executor.execute()

    return result, calls, tmpdir_at_call


# ---------------------------------------------------------------------------
# TestExecuteSetsTmpdirBeforeAnySubprocess
# ---------------------------------------------------------------------------


class TestExecuteSetsTmpdirBeforeAnySubprocess:
    """execute() must set os.environ['TMPDIR'] before ANY subprocess runs."""

    def test_tmpdir_env_set_before_git_pull(
        self,
        executor: DeploymentExecutor,
        tmp_path: Path,
        patched_data_dir: Path,
        no_ambient_tmpdir: None,
    ) -> None:
        """os.environ['TMPDIR'] must already equal the deploy-tmp dir by the
        time git_pull() -- execute()'s first subprocess-spawning step --
        runs. git_pull is made to fail so execute() short-circuits
        immediately afterward, proving the env var is set unconditionally
        and early, before any subprocess (sudo or no-sudo) is spawned.
        """
        captured: dict = {}

        def fake_git_pull() -> bool:
            captured["tmpdir_at_git_pull"] = os.environ.get("TMPDIR")
            return False  # short-circuit execute() right after Step 1 fails

        with patch.object(executor, "git_pull", side_effect=fake_git_pull):
            result = executor.execute()

        assert result is False, "git_pull() failure must short-circuit execute()"
        expected = executor._deploy_tmpdir()  # type: ignore[attr-defined]
        assert captured.get("tmpdir_at_git_pull") == expected, (
            f"TMPDIR must be set to the deploy-tmp dir BEFORE git_pull() runs "
            f"(Bug #1251); got {captured.get('tmpdir_at_git_pull')!r}, "
            f"expected {expected!r}"
        )


# ---------------------------------------------------------------------------
# TestNoSudoSubprocessesInheritTmpdir
# ---------------------------------------------------------------------------


class TestNoSudoSubprocessesInheritTmpdir:
    """Bug #1251: every NO-SUDO deploy subprocess that could build a wheel
    via tempfile (pybind11, hnswlib, pip install -e .) must see
    TMPDIR=<deploy-tmp> in its ambient environment -- proven by running the
    REAL execute(), not by pre-seeding os.environ in the test (which would
    pass even on the pre-fix code and prove nothing about execute() itself).
    """

    def test_pybind11_install_inherits_tmpdir(
        self,
        executor: DeploymentExecutor,
        tmp_path: Path,
        patched_data_dir: Path,
        no_ambient_tmpdir: None,
    ) -> None:
        """pybind11 install (build_custom_hnswlib) must see TMPDIR set."""
        result, calls, tmpdir_at_call = _run_execute_with_real_pip_steps(
            executor, tmp_path
        )

        assert result is True, f"execute() must succeed in this scenario; calls={calls}"
        expected = executor._deploy_tmpdir()  # type: ignore[attr-defined]
        pybind11_calls = [cmd for cmd in tmpdir_at_call if "pybind11" in cmd]
        assert pybind11_calls, f"Expected a pybind11 install call; all calls: {calls}"
        for cmd in pybind11_calls:
            assert cmd[0] != "sudo", f"Expected a no-sudo pybind11 cmd; got: {cmd}"
            assert tmpdir_at_call[cmd] == expected, (
                f"pybind11 install must see TMPDIR={expected!r} in its ambient "
                f"environment (Bug #1251); got {tmpdir_at_call[cmd]!r}. cmd={list(cmd)}"
            )

    def test_hnswlib_build_inherits_tmpdir(
        self,
        executor: DeploymentExecutor,
        tmp_path: Path,
        patched_data_dir: Path,
        no_ambient_tmpdir: None,
    ) -> None:
        """hnswlib --force-reinstall install (build_custom_hnswlib) must see TMPDIR set."""
        result, calls, tmpdir_at_call = _run_execute_with_real_pip_steps(
            executor, tmp_path
        )

        assert result is True, f"execute() must succeed in this scenario; calls={calls}"
        expected = executor._deploy_tmpdir()  # type: ignore[attr-defined]
        hnswlib_calls = [cmd for cmd in tmpdir_at_call if "--force-reinstall" in cmd]
        assert hnswlib_calls, f"Expected an hnswlib install call; all calls: {calls}"
        for cmd in hnswlib_calls:
            assert cmd[0] != "sudo", f"Expected a no-sudo hnswlib cmd; got: {cmd}"
            assert tmpdir_at_call[cmd] == expected, (
                f"hnswlib build must see TMPDIR={expected!r} in its ambient "
                f"environment (Bug #1251); got {tmpdir_at_call[cmd]!r}. cmd={list(cmd)}"
            )

    def test_pip_install_dash_e_inherits_tmpdir(
        self,
        executor: DeploymentExecutor,
        tmp_path: Path,
        patched_data_dir: Path,
        no_ambient_tmpdir: None,
    ) -> None:
        """pip install -e . (pip_install) must see TMPDIR set."""
        result, calls, tmpdir_at_call = _run_execute_with_real_pip_steps(
            executor, tmp_path
        )

        assert result is True, f"execute() must succeed in this scenario; calls={calls}"
        expected = executor._deploy_tmpdir()  # type: ignore[attr-defined]
        pip_install_calls = [
            cmd for cmd in tmpdir_at_call if "-e" in cmd and ".[cluster]" in cmd
        ]
        assert pip_install_calls, (
            f"Expected a pip install -e . call; all calls: {calls}"
        )
        for cmd in pip_install_calls:
            assert cmd[0] != "sudo", f"Expected a no-sudo pip install cmd; got: {cmd}"
            assert tmpdir_at_call[cmd] == expected, (
                f"pip install -e . must see TMPDIR={expected!r} in its ambient "
                f"environment (Bug #1251); got {tmpdir_at_call[cmd]!r}. cmd={list(cmd)}"
            )


# ---------------------------------------------------------------------------
# TestSudoPathUnchanged
# ---------------------------------------------------------------------------


class TestSudoPathUnchanged:
    """Bug #1251 must NOT alter the SUDO command shape established by #1243."""

    def test_sudo_pip_install_still_uses_explicit_env_tmpdir_prefix(
        self,
        executor: DeploymentExecutor,
        tmp_path: Path,
        patched_data_dir: Path,
        no_ambient_tmpdir: None,
    ) -> None:
        """System install (use_sudo=True) -> pip_install()'s command must
        still be ["sudo", "env", f"TMPDIR=<dir>", python, "-m", "pip",
        "install", ...] -- byte-identical to Bug #1243, even though
        os.environ["TMPDIR"] is now ALSO set by the #1251 fix (the #1251 fix
        is additive/belt-and-suspenders for the no-sudo path, not a
        replacement for the explicit sudo env prefix, which remains required
        because sudo's env_reset strips inherited environment variables).
        """
        calls: list = []

        def dispatch(cmd: list, **_kw: object) -> Mock:
            calls.append(list(cmd))
            if "-m" in cmd and "pip" in cmd and "--version" in cmd:
                return Mock(
                    returncode=0,
                    stdout="pip 23.1 from /path (python 3.9)\n",
                    stderr="",
                )
            return Mock(returncode=0, stdout="", stderr="")

        with (
            patch.object(
                executor, "_get_server_python", return_value="/usr/bin/python3"
            ),
            patch.object(executor, "_is_user_install", return_value=False),
            patch("subprocess.run", side_effect=dispatch),
        ):
            result = executor.pip_install()

        assert result is True
        install_calls = [c for c in calls if "-e" in c]
        assert install_calls, f"Expected pip install -e . call; all calls: {calls}"
        cmd = install_calls[0]
        assert cmd[0] == "sudo" and cmd[1] == "env" and cmd[2].startswith("TMPDIR="), (
            f"SUDO path command shape must be unchanged by Bug #1251: "
            f"['sudo', 'env', 'TMPDIR=...', ...]; got {cmd}"
        )
