"""
Tests for Bug #897 mitigation 2: MALLOC_ARENA_MAX=2 idempotent step in
DeploymentExecutor._ensure_malloc_arena_max().

Verifies all four idempotent cases (flag x presence matrix) and error logging.
"""

import logging
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# Named constants — no magic strings scattered through tests
MALLOC_ARENA_ENV_LINE = "Environment=MALLOC_ARENA_MAX=2"
DEPLOY_ERROR_CODE = "DEPLOY-GENERAL-143"

# ---------------------------------------------------------------------------
# Service file templates
# ---------------------------------------------------------------------------

_SERVICE_WITHOUT_ARENA = """\
[Unit]
Description=CIDX Server

[Service]
User=code-indexer
ExecStart=/opt/pipx/venvs/code-indexer/bin/python -m uvicorn code_indexer.server.app:app
Restart=always

[Install]
WantedBy=multi-user.target
"""

_SERVICE_WITH_ARENA = """\
[Unit]
Description=CIDX Server

[Service]
User=code-indexer
Environment=MALLOC_ARENA_MAX=2
ExecStart=/opt/pipx/venvs/code-indexer/bin/python -m uvicorn code_indexer.server.app:app
Restart=always

[Install]
WantedBy=multi-user.target
"""


def _make_subprocess_run_mock(service_content: str, *, write_success: bool = True):
    """Build subprocess.run side_effect list for _ensure_malloc_arena_max.

    Produces:
      1 read mock (sudo cat of the server service file),
      then if write_success=True: tee + daemon-reload success mocks,
      or if write_success=False: tee failure mock only.
    """
    read_ok = Mock(returncode=0, stdout=service_content, stderr="")
    if write_success:
        return [
            read_ok,
            Mock(returncode=0, stdout="", stderr=""),  # sudo tee
            Mock(returncode=0, stdout="", stderr=""),  # systemctl daemon-reload
        ]
    return [
        read_ok,
        Mock(returncode=1, stdout="", stderr="Permission denied"),  # tee fails
    ]


def _make_server_config(enable_malloc_arena_max: bool):
    """Build a minimal fake ServerConfig carrying only the arena-max bootstrap flag."""
    config = Mock()
    config.enable_malloc_arena_max = enable_malloc_arena_max
    return config


def _run_ensure(executor, *, service_content, flag_enabled, write_success=True):
    """Invoke executor._ensure_malloc_arena_max() with patched subprocess and config.

    Patches:
      subprocess.run  — side_effect from _make_subprocess_run_mock
      ServerConfigManager — returns fake_config with the requested flag value

    Returns:
        (result, mock_run) so callers can inspect the subprocess call history.
    """
    fake_config = _make_server_config(flag_enabled)
    side_effects = _make_subprocess_run_mock(
        service_content, write_success=write_success
    )

    with (
        patch("subprocess.run") as mock_run,
        patch(
            "code_indexer.server.utils.config_manager.ServerConfigManager"
        ) as mock_mgr_cls,
    ):
        mock_mgr_cls.return_value.load_config.return_value = fake_config
        mock_run.side_effect = side_effects
        result = executor._ensure_malloc_arena_max()

    return result, mock_run


@pytest.fixture
def executor():
    """DeploymentExecutor instance under test."""
    from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor

    return DeploymentExecutor(
        repo_path=Path("/test/repo"),
        service_name="cidx-server",
    )


# ---------------------------------------------------------------------------
# Test 6: flag True + line absent → line injected, daemon-reload called
# ---------------------------------------------------------------------------


def test_ensure_malloc_arena_max_adds_line_when_flag_on_and_missing(executor):
    result, mock_run = _run_ensure(
        executor,
        service_content=_SERVICE_WITHOUT_ARENA,
        flag_enabled=True,
    )

    assert result is True
    tee_calls = [c for c in mock_run.call_args_list if "tee" in c[0][0]]
    assert len(tee_calls) == 1, "sudo tee must be called exactly once"
    written = tee_calls[0][1]["input"]
    assert MALLOC_ARENA_ENV_LINE in written, "MALLOC_ARENA_MAX=2 line must be injected"

    reload_calls = [c for c in mock_run.call_args_list if "daemon-reload" in c[0][0]]
    assert len(reload_calls) == 1, "systemctl daemon-reload must be called"


# ---------------------------------------------------------------------------
# Test 7: flag False + line present → line removed, daemon-reload called
# ---------------------------------------------------------------------------


def test_ensure_malloc_arena_max_removes_line_when_flag_off_and_present(executor):
    result, mock_run = _run_ensure(
        executor,
        service_content=_SERVICE_WITH_ARENA,
        flag_enabled=False,
    )

    assert result is True
    tee_calls = [c for c in mock_run.call_args_list if "tee" in c[0][0]]
    assert len(tee_calls) == 1, "sudo tee must be called to remove the line"
    written = tee_calls[0][1]["input"]
    assert MALLOC_ARENA_ENV_LINE not in written, (
        "MALLOC_ARENA_MAX=2 line must be stripped when flag is False"
    )


# ---------------------------------------------------------------------------
# Test 8: flag True + line already present → no-op (no tee call)
# ---------------------------------------------------------------------------


def test_ensure_malloc_arena_max_noop_when_flag_on_and_present(executor):
    result, mock_run = _run_ensure(
        executor,
        service_content=_SERVICE_WITH_ARENA,
        flag_enabled=True,
    )

    assert result is True
    tee_calls = [c for c in mock_run.call_args_list if "tee" in c[0][0]]
    assert len(tee_calls) == 0, "sudo tee must NOT be called when already correct"


# ---------------------------------------------------------------------------
# Test 9: flag False + line absent → no-op (no tee call)
# ---------------------------------------------------------------------------


def test_ensure_malloc_arena_max_noop_when_flag_off_and_absent(executor):
    result, mock_run = _run_ensure(
        executor,
        service_content=_SERVICE_WITHOUT_ARENA,
        flag_enabled=False,
    )

    assert result is True
    tee_calls = [c for c in mock_run.call_args_list if "tee" in c[0][0]]
    assert len(tee_calls) == 0, "sudo tee must NOT be called when already correct"


# ---------------------------------------------------------------------------
# Test 10: write failure → returns False and DEPLOY-GENERAL-143 logged
# ---------------------------------------------------------------------------


def test_ensure_malloc_arena_max_logs_deploy_error_code(executor, caplog):
    with caplog.at_level(logging.WARNING):
        result, _ = _run_ensure(
            executor,
            service_content=_SERVICE_WITHOUT_ARENA,
            flag_enabled=True,
            write_success=False,
        )

    assert result is False, "Must return False when write fails"
    assert DEPLOY_ERROR_CODE in caplog.text, (
        f"Error code {DEPLOY_ERROR_CODE} must appear in logs on write failure"
    )
