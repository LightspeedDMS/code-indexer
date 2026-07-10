"""Tests for scip-python provisioning in DeploymentExecutor.

The SCIP indexing feature (`cidx scip generate` / add_golden_repo_index
index_type=scip) shells out to the `scip-python` binary (see
src/code_indexer/scip/indexers/python.py::PythonIndexer). That binary ships
as the npm package `@sourcegraph/scip-python`. Neither the idempotent
installer (scripts/install-cidx-server.sh) nor the auto-updater
(DeploymentExecutor) provisioned it, so SCIP indexes could never build on a
freshly-provisioned cluster node -- `[Errno 2] No such file or directory:
'scip-python'`.

This mirrors the existing ensure_ripgrep()/_ensure_codex_cli_installed()
idempotent check-then-install pattern:
  - already installed (shutil.which resolves) -> no npm call, returns True
  - not installed, npm present -> runs
    `npm install -g @sourcegraph/scip-python`, returns True on exit 0
  - npm absent -> WARNING logged, returns False, no subprocess call
  - npm install nonzero exit -> WARNING logged, returns False
  - npm install times out -> WARNING logged, returns False, no re-raise
  - npm install raises OSError (spawn failure) -> WARNING, returns False,
    no re-raise
  - execute() wires the call (non-fatal to overall deployment)
"""

import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor

SCIP_PYTHON_INSTALL_CMD = ["npm", "install", "-g", "@sourcegraph/scip-python"]
SCIP_PYTHON_INSTALL_TIMEOUT_SECONDS = 300  # generous timeout; npm installs can be slow


@pytest.fixture
def executor():
    """DeploymentExecutor instance under test."""
    return DeploymentExecutor(
        repo_path=Path("/test/repo"),
        service_name="cidx-server",
    )


def _which_scip_python_absent_npm_present(name: str):
    """shutil.which side_effect: scip-python missing, npm present."""
    if name == "scip-python":
        return None
    if name == "npm":
        return "/usr/bin/npm"
    return None


def _install_ok() -> MagicMock:
    """Simulate a successful `npm install -g @sourcegraph/scip-python`."""
    return MagicMock(returncode=0, stdout="added 1 package", stderr="")


# ---------------------------------------------------------------------------
# Already installed -> no npm call
# ---------------------------------------------------------------------------


def test_already_installed_skips_npm_install_and_returns_true(executor, caplog):
    """When scip-python already resolves on PATH, no npm install is attempted."""
    with (
        patch("shutil.which", return_value="/usr/local/bin/scip-python"),
        patch("subprocess.run") as mock_run,
        caplog.at_level(logging.INFO),
    ):
        result = executor.ensure_scip_python()

    assert result is True
    mock_run.assert_not_called()
    assert any("scip-python" in record.message.lower() for record in caplog.records), (
        "Expected an INFO log mentioning scip-python already installed"
    )


# ---------------------------------------------------------------------------
# Not installed, npm present -> installs, returns True on exit 0
# ---------------------------------------------------------------------------


def test_not_installed_npm_present_installs_and_returns_true(executor, caplog):
    """When scip-python is absent and npm is present, npm install is run."""
    with (
        patch("shutil.which", side_effect=_which_scip_python_absent_npm_present),
        patch("subprocess.run", return_value=_install_ok()) as mock_run,
        caplog.at_level(logging.INFO),
    ):
        result = executor.ensure_scip_python()

    assert result is True
    mock_run.assert_called_once()
    args, kwargs = mock_run.call_args
    assert args[0] == SCIP_PYTHON_INSTALL_CMD
    assert kwargs.get("timeout") == SCIP_PYTHON_INSTALL_TIMEOUT_SECONDS


def test_idempotent_second_run_no_error(executor):
    """Calling ensure_scip_python() twice must not raise; both succeed."""
    with (
        patch("shutil.which", side_effect=_which_scip_python_absent_npm_present),
        patch("subprocess.run", return_value=_install_ok()),
    ):
        result1 = executor.ensure_scip_python()
        result2 = executor.ensure_scip_python()

    assert result1 is True
    assert result2 is True


# ---------------------------------------------------------------------------
# npm absent -> WARNING, returns False, no subprocess call
# ---------------------------------------------------------------------------


def test_npm_absent_logs_warning_and_returns_false(executor, caplog):
    """When npm is not on PATH, function logs WARNING and returns False.

    Unlike the optional Codex CLI feature, scip-python is required for SCIP
    indexing to function at all, so absence of npm is reported as a failure
    to provision (non-fatal to the overall deploy, but not silently "ok").
    """
    with (
        patch("shutil.which", return_value=None),
        patch("subprocess.run") as mock_run,
        caplog.at_level(logging.WARNING),
    ):
        result = executor.ensure_scip_python()

    assert result is False
    mock_run.assert_not_called()
    assert any(
        "npm" in record.message.lower()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ), "A WARNING log mentioning npm must be emitted"


# ---------------------------------------------------------------------------
# npm install nonzero exit -> WARNING, returns False
# ---------------------------------------------------------------------------


def test_npm_install_nonzero_returncode_logs_warning_returns_false(executor, caplog):
    """When npm install returns nonzero, function logs WARNING and returns False."""
    install_fail = MagicMock(
        returncode=1, stdout="", stderr="EACCES: permission denied"
    )

    with (
        patch("shutil.which", side_effect=_which_scip_python_absent_npm_present),
        patch("subprocess.run", return_value=install_fail) as mock_run,
        caplog.at_level(logging.WARNING),
    ):
        result = executor.ensure_scip_python()

    assert result is False
    assert mock_run.call_count == 1
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# npm install times out -> WARNING, returns False, no re-raise
# ---------------------------------------------------------------------------


def test_npm_install_timeout_logs_warning_returns_false_no_raise(executor, caplog):
    """subprocess.TimeoutExpired is caught, logged, and does not propagate."""
    timeout_error = subprocess.TimeoutExpired(
        cmd=SCIP_PYTHON_INSTALL_CMD, timeout=SCIP_PYTHON_INSTALL_TIMEOUT_SECONDS
    )

    with (
        patch("shutil.which", side_effect=_which_scip_python_absent_npm_present),
        patch("subprocess.run", side_effect=timeout_error),
        caplog.at_level(logging.WARNING),
    ):
        result = executor.ensure_scip_python()

    assert result is False
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# npm install raises OSError (spawn failure) -> WARNING, returns False
# ---------------------------------------------------------------------------


def test_npm_install_oserror_logs_warning_returns_false_no_raise(executor, caplog):
    """OSError (e.g. npm binary vanished between which() and run()) is caught."""
    with (
        patch("shutil.which", side_effect=_which_scip_python_absent_npm_present),
        patch("subprocess.run", side_effect=OSError("cannot spawn npm")),
        caplog.at_level(logging.WARNING),
    ):
        result = executor.ensure_scip_python()

    assert result is False
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


# ---------------------------------------------------------------------------
# execute() wires the call
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_execute_wires_ensure_scip_python(executor, caplog):
    """execute() must call ensure_scip_python() at least once.

    Following the established project pattern (ensure_ripgrep/_ensure_codex_
    cli_installed wiring tests): sibling steps are patched so only the step
    under test runs for real, with shutil.which/subprocess.run as the true
    external boundary. npm is reported absent so the method hits the
    "cannot provision" branch, which emits an observable WARNING proving
    execute() reached ensure_scip_python().
    """
    with (
        patch.object(executor, "_calculate_auto_update_hash", return_value="abc123"),
        patch.object(executor, "git_pull", return_value=True),
        patch.object(executor, "git_submodule_update", return_value=True),
        patch.object(executor, "_build_hnswlib_with_fallback", return_value=True),
        patch.object(executor, "pip_install", return_value=True),
        patch.object(executor, "ensure_ripgrep", return_value=True),
        patch.object(executor, "_ensure_rust_toolchain", return_value=True),
        patch("shutil.which", return_value=None),
        caplog.at_level(logging.WARNING),
    ):
        executor.execute()

    assert any(
        "npm" in record.message.lower() and "scip-python" in record.message.lower()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ), (
        "execute() must have reached ensure_scip_python(): expected a WARNING mentioning npm and scip-python"
    )
