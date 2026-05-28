"""Story #1024 - Unit tests for DeploymentExecutor._ensure_rust_toolchain().

Tests the Rust toolchain provisioning and xray-cli build logic:

AC1: rustc already at stable version -> skip install, build xray-cli, return True (idempotent)
AC2: rustup install fails -> return False (FATAL path)
AC3: cargo build succeeds when Rust is freshly installed -> return True
AC4: rust/ directory not found -> logs WARNING, return True (non-fatal - older code)
AC5: No C compiler found -> logs ERROR, return False
AC6: cargo build fails -> return False

Only true external dependencies are mocked:
  - subprocess.run (for rustc, rustup, cargo, curl|sh commands)
  - shutil.which (for C compiler detection — gcc/cc/clang)
  - Path.home (home directory resolution)
  - __file__ module attribute (for repo root resolution)
"""

import logging
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MODULE = "code_indexer.server.auto_update.deployment_executor"


def _make_executor() -> DeploymentExecutor:
    """Build a minimal DeploymentExecutor without real side effects."""
    executor = DeploymentExecutor.__new__(DeploymentExecutor)
    executor.service_name = "cidx-server"
    return executor


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    """Return a MagicMock mimicking subprocess.CompletedProcess."""
    p = MagicMock()
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


def _has_level(caplog: pytest.LogCaptureFixture, level: int) -> bool:
    return any(r.levelno >= level for r in caplog.records)


@contextmanager
def _rust_env(tmp_path: Path) -> Iterator[MagicMock]:
    """Context manager that patches Path.home and __file__ for _ensure_rust_toolchain tests.

    Yields the mock_run MagicMock so callers can configure side_effects.
    """
    fake_file = str(
        tmp_path
        / "src"
        / "code_indexer"
        / "server"
        / "auto_update"
        / "deployment_executor.py"
    )
    with (
        patch(f"{_MODULE}.Path.home", return_value=tmp_path),
        patch(f"{_MODULE}.__file__", fake_file, create=True),
        patch(f"{_MODULE}.SYSTEMD_UNIT_DIR", tmp_path / "systemd"),
        patch("subprocess.run") as mock_run,
    ):
        yield mock_run


@contextmanager
def _all_steps_except_rust(executor: DeploymentExecutor) -> Iterator[None]:
    """Context manager that mocks all execute() steps except _ensure_rust_toolchain."""
    with (
        patch.object(executor, "_calculate_auto_update_hash", return_value="hash"),
        patch.object(executor, "git_pull", return_value=True),
        patch.object(executor, "git_submodule_update", return_value=True),
        patch.object(executor, "build_custom_hnswlib", return_value=True),
        patch.object(executor, "pip_install", return_value=True),
        patch.object(executor, "ensure_ripgrep", return_value=True),
        patch.object(executor, "_ensure_sudoers_restart", return_value=True),
        patch.object(executor, "_ensure_memory_overcommit", return_value=True),
        patch.object(executor, "_ensure_swap_file", return_value=True),
        patch.object(executor, "_ensure_claude_cli_updated", return_value=True),
        patch.object(executor, "_ensure_pace_maker_installed", return_value=True),
        patch.object(executor, "_ensure_claude_cli_installed", return_value=True),
        patch.object(executor, "_ensure_nfs_research_symlinks", return_value=True),
        patch.object(executor, "_ensure_systemd_claude_path", return_value=True),
    ):
        yield


# ---------------------------------------------------------------------------
# AC1: rustc already present -> idempotent, skip install, build, return True
# ---------------------------------------------------------------------------


class TestRustcAlreadyPresent:
    """When rustc is already installed, skip rustup and go straight to build."""

    def test_returns_true_when_rustc_present_and_build_succeeds(
        self, tmp_path: Path
    ) -> None:
        executor = _make_executor()
        rust_dir = tmp_path / "rust"
        rust_dir.mkdir()

        with _rust_env(tmp_path) as mock_run:
            mock_run.side_effect = [
                _proc(0, stdout="rustc 1.78.0"),  # rustc --version
                _proc(0),  # cargo build
            ]
            with patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/gcc"):
                result = executor._ensure_rust_toolchain()

        assert result is True

    def test_rustup_install_not_called_when_rustc_present(self, tmp_path: Path) -> None:
        """If rustc --version succeeds, no rustup/curl install should happen."""
        executor = _make_executor()
        rust_dir = tmp_path / "rust"
        rust_dir.mkdir()

        with _rust_env(tmp_path) as mock_run:
            mock_run.side_effect = [
                _proc(0, stdout="rustc 1.78.0"),
                _proc(0),  # cargo build
            ]
            with patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/gcc"):
                executor._ensure_rust_toolchain()

        for c in mock_run.call_args_list:
            assert "rustup.rs" not in str(c), (
                f"Expected no rustup install when rustc already present, got: {c}"
            )


# ---------------------------------------------------------------------------
# AC2: rustup install fails -> return False
# ---------------------------------------------------------------------------


class TestRustupInstallFails:
    """When rustc is missing and rustup install fails, return False."""

    def test_returns_false_when_rustup_install_fails(self, tmp_path: Path) -> None:
        executor = _make_executor()

        with _rust_env(tmp_path) as mock_run:
            mock_run.side_effect = [
                FileNotFoundError("rustc not found"),  # rustc --version -> missing
                _proc(1, stderr="error: could not install"),  # rustup -> fail
            ]
            result = executor._ensure_rust_toolchain()

        assert result is False

    def test_logs_error_when_rustup_install_fails(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        executor = _make_executor()

        with caplog.at_level(logging.ERROR):
            with _rust_env(tmp_path) as mock_run:
                mock_run.side_effect = [
                    FileNotFoundError("rustc not found"),
                    _proc(1, stderr="install failed"),
                ]
                executor._ensure_rust_toolchain()

        assert _has_level(caplog, logging.ERROR)


# ---------------------------------------------------------------------------
# AC3: Fresh install succeeds, cargo build succeeds -> return True
# ---------------------------------------------------------------------------


class TestFreshRustInstallSucceeds:
    """Full fresh-install: rustc missing -> install -> pin stable -> build -> True."""

    def test_returns_true_after_fresh_install_and_build(self, tmp_path: Path) -> None:
        executor = _make_executor()
        rust_dir = tmp_path / "rust"
        rust_dir.mkdir()

        with _rust_env(tmp_path) as mock_run:
            mock_run.side_effect = [
                FileNotFoundError("rustc not found"),  # rustc --version -> missing
                _proc(0),  # rustup install script
                _proc(0),  # rustup default stable
                _proc(0),  # cargo build
            ]
            with patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/gcc"):
                result = executor._ensure_rust_toolchain()

        assert result is True

    def test_rustup_default_stable_called_after_install(self, tmp_path: Path) -> None:
        """After installing rustup, 'rustup default stable' must be called."""
        executor = _make_executor()
        rust_dir = tmp_path / "rust"
        rust_dir.mkdir()

        with _rust_env(tmp_path) as mock_run:
            mock_run.side_effect = [
                FileNotFoundError("rustc not found"),
                _proc(0),  # install
                _proc(0),  # rustup default stable
                _proc(0),  # cargo build
            ]
            with patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/gcc"):
                executor._ensure_rust_toolchain()

        found = any(
            "rustup" in str(c) and "default" in str(c) and "stable" in str(c)
            for c in mock_run.call_args_list
        )
        assert found, "Expected 'rustup default stable' call not found"

    def test_returns_true_after_fresh_install_when_stable_baked_into_sh(
        self, tmp_path: Path
    ) -> None:
        """Stable toolchain pinning uses --default-toolchain stable baked into RUSTUP_SH_ARGS,
        so there is no separate 'rustup default stable' subprocess call.
        Fresh install path: rustc missing -> curl ok -> sh ok -> cargo build ok -> True."""
        executor = _make_executor()
        rust_dir = tmp_path / "rust"
        rust_dir.mkdir()

        with _rust_env(tmp_path) as mock_run:
            mock_run.side_effect = [
                FileNotFoundError("rustc not found"),  # rustc --version -> missing
                _proc(0),  # curl: download installer script -> ok
                _proc(0),  # sh: run installer with --default-toolchain stable -> ok
                _proc(0),  # cargo build -> ok
            ]
            with patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/gcc"):
                result = executor._ensure_rust_toolchain()

        assert result is True

    def test_warning_logged_when_pin_stable_fails(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """'rustup default stable' failure must be logged at WARNING level."""
        executor = _make_executor()
        rust_dir = tmp_path / "rust"
        rust_dir.mkdir()

        with caplog.at_level(logging.WARNING):
            with _rust_env(tmp_path) as mock_run:
                mock_run.side_effect = [
                    FileNotFoundError("rustc not found"),
                    _proc(0),  # install ok
                    _proc(1, stderr="toolchain error"),  # pin stable -> FAIL
                    _proc(0),  # cargo build
                ]
                with patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/gcc"):
                    executor._ensure_rust_toolchain()

        assert _has_level(caplog, logging.WARNING)


# ---------------------------------------------------------------------------
# AC4: rust/ directory missing -> log WARNING, return True (non-fatal)
# ---------------------------------------------------------------------------


class TestRustDirMissing:
    """When rust/ directory doesn't exist, log WARNING and return True (non-fatal)."""

    def test_returns_true_when_rust_dir_missing(self, tmp_path: Path) -> None:
        """rust/ directory not present -> skip build, return True."""
        executor = _make_executor()
        # Deliberately do NOT create rust_dir

        with _rust_env(tmp_path) as mock_run:
            mock_run.side_effect = [
                _proc(0, stdout="rustc 1.78.0"),  # rustc --version
                # No gcc/cargo calls expected (early return before C compiler check)
            ]
            result = executor._ensure_rust_toolchain()

        assert result is True

    def test_warning_logged_when_rust_dir_missing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        executor = _make_executor()

        with caplog.at_level(logging.WARNING):
            with _rust_env(tmp_path) as mock_run:
                mock_run.side_effect = [
                    _proc(0, stdout="rustc 1.78.0"),
                ]
                executor._ensure_rust_toolchain()

        assert _has_level(caplog, logging.WARNING)


# ---------------------------------------------------------------------------
# AC5: No C compiler found -> log ERROR, return False
# ---------------------------------------------------------------------------


class TestNoCCompilerFound:
    """When no gcc/cc/clang is found, log ERROR and return False."""

    def test_returns_false_when_no_c_compiler(self, tmp_path: Path) -> None:
        executor = _make_executor()
        rust_dir = tmp_path / "rust"
        rust_dir.mkdir()

        with _rust_env(tmp_path) as mock_run:
            mock_run.side_effect = [
                _proc(0, stdout="rustc 1.78.0"),  # rustc --version
                _proc(0),  # cargo build (unreachable — shutil.which returns None)
            ]
            with patch(f"{_MODULE}.shutil.which", return_value=None):
                result = executor._ensure_rust_toolchain()

        assert result is False

    def test_error_logged_when_no_c_compiler(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        executor = _make_executor()
        rust_dir = tmp_path / "rust"
        rust_dir.mkdir()

        with caplog.at_level(logging.ERROR):
            with _rust_env(tmp_path) as mock_run:
                mock_run.side_effect = [
                    _proc(0, stdout="rustc 1.78.0"),
                ]
                with patch(f"{_MODULE}.shutil.which", return_value=None):
                    executor._ensure_rust_toolchain()

        assert _has_level(caplog, logging.ERROR)


# ---------------------------------------------------------------------------
# AC6: cargo build fails -> return False
# ---------------------------------------------------------------------------


class TestCargoBuildFails:
    """When cargo build fails, return False."""

    def test_returns_false_when_cargo_build_fails(self, tmp_path: Path) -> None:
        executor = _make_executor()
        rust_dir = tmp_path / "rust"
        rust_dir.mkdir()

        with _rust_env(tmp_path) as mock_run:
            mock_run.side_effect = [
                _proc(0, stdout="rustc 1.78.0"),  # rustc --version
                _proc(1, stderr="error[E0001]: ..."),  # cargo build -> FAIL
            ]
            with patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/gcc"):
                result = executor._ensure_rust_toolchain()

        assert result is False

    def test_error_logged_when_cargo_build_fails(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        executor = _make_executor()
        rust_dir = tmp_path / "rust"
        rust_dir.mkdir()

        with caplog.at_level(logging.ERROR):
            with _rust_env(tmp_path) as mock_run:
                mock_run.side_effect = [
                    _proc(0, stdout="rustc 1.78.0"),
                    _proc(1, stderr="build failed"),
                ]
                with patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/gcc"):
                    executor._ensure_rust_toolchain()

        assert _has_level(caplog, logging.ERROR)


# ---------------------------------------------------------------------------
# Step 16 in execute(): FATAL - deployment returns False when toolchain fails
# ---------------------------------------------------------------------------


class TestStep16InExecute:
    """Step 16 is FATAL: execute() returns False when _ensure_rust_toolchain fails."""

    @pytest.fixture()
    def executor(self, tmp_path: Path) -> DeploymentExecutor:
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        return DeploymentExecutor(
            repo_path=repo_dir,
            service_name="cidx-server",
        )

    def test_execute_returns_false_when_rust_toolchain_fails(
        self, executor: DeploymentExecutor
    ) -> None:
        with _all_steps_except_rust(executor):
            with patch.object(executor, "_ensure_rust_toolchain", return_value=False):
                result = executor.execute()

        assert result is False

    def test_execute_logs_error_when_rust_toolchain_fails(
        self,
        executor: DeploymentExecutor,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.ERROR):
            with _all_steps_except_rust(executor):
                with patch.object(
                    executor, "_ensure_rust_toolchain", return_value=False
                ):
                    executor.execute()

        assert _has_level(caplog, logging.ERROR)

    def test_execute_returns_true_when_rust_toolchain_succeeds(
        self, executor: DeploymentExecutor
    ) -> None:
        with _all_steps_except_rust(executor):
            with patch.object(executor, "_ensure_rust_toolchain", return_value=True):
                result = executor.execute()

        assert result is True


# ---------------------------------------------------------------------------
# Timeout handling: subprocess.TimeoutExpired -> return False
# ---------------------------------------------------------------------------


class TestTimeoutHandling:
    """When subprocess.run raises TimeoutExpired, methods return False gracefully."""

    def test_install_rust_toolchain_returns_false_on_timeout(
        self, tmp_path: Path
    ) -> None:
        """_install_rust_toolchain returns False when the rustup install times out."""
        executor = _make_executor()

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd=["curl", "-sSf", "https://sh.rustup.rs"],
                timeout=300,
            )
            result = executor._install_rust_toolchain(env={})

        assert result is False

    def test_build_xray_cli_returns_false_on_timeout(self, tmp_path: Path) -> None:
        """_build_xray_cli returns False when the cargo build times out."""
        executor = _make_executor()
        rust_dir = tmp_path / "rust"
        rust_dir.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(
                cmd=["cargo", "build", "--release", "-p", "xray-cli"],
                timeout=1200,
            )
            result = executor._build_xray_cli(rust_dir=rust_dir, env={})

        assert result is False


# ---------------------------------------------------------------------------
# M5: _install_rust_toolchain must NOT use shell=True
# ---------------------------------------------------------------------------


class TestNoShellTrueInInstall:
    """M5: Rust installer must use two separate subprocess calls (curl + sh),
    never shell=True with a pipeline string."""

    def test_curl_called_as_list_not_shell_string(self, tmp_path: Path) -> None:
        """First subprocess call must be curl with a list argument (no shell=True)."""
        executor = _make_executor()

        calls = []

        def recording_run(args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append((args, kwargs))
            p = MagicMock()
            p.returncode = 0
            p.stdout = b"fake installer"
            p.stderr = b""
            return p

        with patch("subprocess.run", side_effect=recording_run):
            executor._install_rust_toolchain(env={})

        assert len(calls) >= 1, "Expected at least one subprocess call"
        first_args, first_kwargs = calls[0]
        # Must be a list (not a string)
        assert isinstance(first_args, list), (
            f"First subprocess call must use list args, got: {type(first_args)}"
        )
        # Must not use shell=True
        assert not first_kwargs.get("shell", False), (
            "First subprocess call must not use shell=True"
        )
        # Must be curl targeting rustup.rs
        assert first_args[0] == "curl", (
            f"Expected 'curl' as first arg, got {first_args[0]}"
        )
        assert any("rustup.rs" in str(a) for a in first_args), (
            "Expected rustup.rs URL in curl args"
        )

    def test_sh_called_as_list_not_shell_string(self, tmp_path: Path) -> None:
        """Second subprocess call (sh installer) must use list args (no shell=True)."""
        executor = _make_executor()

        calls = []

        def recording_run(args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append((args, kwargs))
            p = MagicMock()
            p.returncode = 0
            p.stdout = b""
            p.stderr = b""
            return p

        with patch("subprocess.run", side_effect=recording_run):
            executor._install_rust_toolchain(env={})

        assert len(calls) >= 2, "Expected at least two subprocess calls (curl + sh)"
        second_args, second_kwargs = calls[1]
        assert isinstance(second_args, list), (
            f"Second subprocess call must use list args, got: {type(second_args)}"
        )
        assert not second_kwargs.get("shell", False), (
            "Second subprocess call must not use shell=True"
        )
        assert second_args[0] == "sh", (
            f"Expected 'sh' as first arg of second call, got {second_args[0]}"
        )

    def test_installer_script_piped_as_input_bytes(self, tmp_path: Path) -> None:
        """The downloaded curl output must be passed as input= to the sh call."""
        executor = _make_executor()
        fake_script = b"#!/bin/sh\necho 'fake rustup installer'"

        call_inputs = []

        def recording_run(args, **kwargs):  # type: ignore[no-untyped-def]
            call_inputs.append(kwargs.get("input"))
            p = MagicMock()
            p.returncode = 0
            p.stdout = fake_script if args[0] == "curl" else b""
            p.stderr = b""
            return p

        with patch("subprocess.run", side_effect=recording_run):
            executor._install_rust_toolchain(env={})

        # The second call (sh) must receive the curl stdout as input
        assert len(call_inputs) >= 2
        assert call_inputs[1] == fake_script, (
            "sh must receive the curl stdout as input= bytes"
        )


# ---------------------------------------------------------------------------
# M6: PATH must not contain empty segment when env PATH is absent
# ---------------------------------------------------------------------------


class TestPathNoTrailingColon:
    """M6: When PATH env var is not set, cargo_bin must be set as PATH without
    a trailing colon (which would add CWD to PATH — a security risk)."""

    def test_path_has_no_trailing_colon_when_env_path_absent(
        self, tmp_path: Path
    ) -> None:
        """PATH must not end with ':' when original PATH is empty/missing."""
        executor = _make_executor()
        # rust/ dir present so we get past the early-return
        rust_dir = tmp_path / "rust"
        rust_dir.mkdir()

        observed_envs = []

        def recording_run(args, **kwargs):  # type: ignore[no-untyped-def]
            env = kwargs.get("env") or {}
            observed_envs.append(dict(env))
            p = MagicMock()
            p.returncode = 0
            p.stdout = b"rustc 1.78.0"
            p.stderr = b""
            return p

        fake_file = str(
            tmp_path
            / "src"
            / "code_indexer"
            / "server"
            / "auto_update"
            / "deployment_executor.py"
        )

        # Remove PATH from the environment copy that os.environ.copy() produces
        original_environ = dict(os.environ)
        original_environ.pop("PATH", None)

        with (
            patch(f"{_MODULE}.Path.home", return_value=tmp_path),
            patch(f"{_MODULE}.__file__", fake_file, create=True),
            patch(f"{_MODULE}.os.environ", original_environ),
            patch(f"{_MODULE}.shutil.which", return_value="/usr/bin/gcc"),
            patch("subprocess.run", side_effect=recording_run),
        ):
            executor._ensure_rust_toolchain()

        # At least one subprocess call should have been made with env containing PATH
        paths_seen = [e.get("PATH", "") for e in observed_envs if "PATH" in e]
        assert paths_seen, "Expected at least one subprocess call with PATH env"
        for path_val in paths_seen:
            assert not path_val.endswith(":"), (
                f"PATH must not end with ':' (empty segment = CWD in PATH), got: {path_val!r}"
            )
            assert "::" not in path_val, (
                f"PATH must not contain '::' (empty segment), got: {path_val!r}"
            )
