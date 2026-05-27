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

    def test_returns_true_even_when_pin_stable_fails(self, tmp_path: Path) -> None:
        """'rustup default stable' failing is non-fatal; method continues and returns True."""
        executor = _make_executor()
        rust_dir = tmp_path / "rust"
        rust_dir.mkdir()

        with _rust_env(tmp_path) as mock_run:
            mock_run.side_effect = [
                FileNotFoundError("rustc not found"),  # rustc --version -> missing
                _proc(0),  # rustup install -> ok
                _proc(1, stderr="toolchain error"),  # rustup default stable -> FAIL
                _proc(0),  # cargo build
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
