"""
Tests for Story #845: Auto-Updater Installs/Updates Codex CLI.

Verifies DeploymentExecutor._ensure_codex_cli_installed():
  - npm absent  -> WARNING, return True (optional-feature semantics)
  - npm present, install succeeds -> INFO version logged, return True
  - npm install fails (nonzero) -> WARNING, return False, no version probe
  - version probe raises FileNotFoundError -> WARNING, return True
  - idempotent second call -> both calls succeed, no exception
  - execute() wires the call (at least once)
  - subprocess.TimeoutExpired -> WARNING, return False, no re-raise
  - execute() continues even when _ensure_codex_cli_installed returns False

Execute() wiring tests follow the established project pattern from
test_deployment_executor_claude_cli_update.py::test_execute_calls_ensure_claude_cli_updated,
which patches sibling _ensure_* methods to isolate the step under test.
"""

import logging
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

CODEX_INSTALL_CMD = ["npm", "install", "-g", "@openai/codex"]
CODEX_INSTALL_TIMEOUT_SECONDS = 300  # generous timeout; npm installs can be slow


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def executor():
    """DeploymentExecutor instance under test."""
    from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor

    return DeploymentExecutor(
        repo_path=Path("/test/repo"),
        service_name="cidx-server",
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@contextmanager
def _patch_npm_env(subprocess_side_effects):
    """Patch shutil.which and subprocess.run for npm/codex tests.

    Simulates npm and codex both present on PATH (/usr/bin/<name>).
    subprocess.run calls are driven by subprocess_side_effects.

    Yields the mock_run object so callers can assert call counts without
    re-patching subprocess.run.
    """
    with (
        patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
        patch("subprocess.run", side_effect=subprocess_side_effects) as mock_run,
    ):
        yield mock_run


@contextmanager
def _patch_execute_siblings(executor, *, claude_side_effect=None):
    """Patch the execute() sibling step helpers, except _ensure_codex_cli_installed.

    Follows the established project pattern from test_execute_calls_ensure_claude_cli_updated:
    sibling methods are patched so only the step under test runs for real.
    _ensure_codex_cli_installed is intentionally left unpatched.

    Args:
        claude_side_effect: optional side_effect for _ensure_claude_cli_updated.
            Defaults to a no-op returning True.
    """
    if claude_side_effect is None:

        def claude_side_effect(*args, **kwargs):
            return True

    # Use ExitStack to avoid Python 3.9 "too many statically nested blocks" limit
    # when combining more than ~20 context managers in a single with (...) expression.
    from contextlib import AbstractContextManager, ExitStack

    # Explicit annotation: the list mixes several unittest.mock._patch[...]
    # specializations (patch.object with different return_value/side_effect
    # types) plus patch.dict's return type. Without this, mypy widens the
    # list's inferred element type to the bare `object`, which
    # ExitStack.enter_context() rejects (it expects
    # AbstractContextManager[...]). unittest.mock.patch/patch.object/patch.dict
    # all return objects implementing __enter__/__exit__ ("Any" import is at
    # module top, line 24), so they genuinely satisfy the
    # AbstractContextManager protocol -- this is a correct annotation, not
    # error suppression.
    patches: list[AbstractContextManager[Any]] = [
        patch.object(executor, "git_pull", return_value=True),
        patch.object(executor, "git_submodule_update", return_value=True),
        patch.object(executor, "_build_hnswlib_with_fallback", return_value=True),
        patch.object(executor, "pip_install", return_value=True),
        patch.object(executor, "_ensure_launch_config", return_value=None),
        patch.object(executor, "_ensure_cidx_repo_root", return_value=True),
        patch.object(executor, "_ensure_git_safe_directory", return_value=True),
        patch.object(
            executor, "_ensure_git_safe_directory_wildcard", return_value=True
        ),
        patch.object(
            executor, "_ensure_auto_updater_uses_server_python", return_value=True
        ),
        patch.object(executor, "_ensure_data_dir_env_var", return_value=True),
        patch.object(
            executor, "_ensure_auto_update_service_has_cli_path", return_value=True
        ),
        patch.object(executor, "_ensure_malloc_arena_max", return_value=True),
        patch.object(executor, "ensure_ripgrep", return_value=True),
        patch.object(executor, "ensure_nodejs", return_value=True),
        patch.object(executor, "_ensure_sudoers_restart", return_value=True),
        patch.object(executor, "_ensure_memory_overcommit", return_value=True),
        patch.object(executor, "_ensure_swap_file", return_value=True),
        patch.object(
            executor, "_ensure_claude_cli_updated", side_effect=claude_side_effect
        ),
        patch.object(executor, "_ensure_pace_maker_installed", return_value=True),
        patch.object(executor, "_ensure_claude_cli_installed", return_value=True),
        patch.object(executor, "_ensure_nfs_research_symlinks", return_value=True),
        patch.object(executor, "_ensure_systemd_claude_path", return_value=True),
        patch.object(executor, "_ensure_rust_toolchain", return_value=True),
        patch.object(executor, "_calculate_auto_update_hash", return_value="abc123"),
        patch.object(executor, "_ensure_cli_dependencies_synced", return_value=True),
        # Bug #1731: these 3 execute() siblings were unpatched and only
        # "bounded by accident, not explicit isolation" -- confirmed via a
        # runtime spy probe that execute()'s control flow DOES reach them
        # (no earlier `return False` short-circuits it out), and that their
        # real implementations were only no-op'ing today because this
        # machine's on-disk ~/.cidx-server/config.json happens to have
        # clone_backend="local" (_restart_auto_update_service's own guard
        # is a patched _calculate_auto_update_hash constant above, which
        # makes it genuinely unreached -- patched here anyway for the same
        # explicit-isolation discipline as its siblings).
        patch.object(executor, "_restart_auto_update_service", return_value=True),
        patch.object(
            executor,
            "_ensure_activated_repos_symlink_for_cow_daemon",
            return_value=True,
        ),
        patch.object(executor, "_ensure_daemon_storage_path", return_value=True),
        # Found during this same fix's due-diligence pass over the issue's
        # "lower priority" list: _deploy_tmpdir is unconditionally called by
        # execute() with NO internal guard at all (the very first thing
        # execute() does) and performs a real mkdir(parents=True,
        # exist_ok=True) on the live filesystem every test run. Its return
        # value is assigned by execute() into the PROCESS-GLOBAL
        # os.environ["TMPDIR"] (nothing else reads it back in these fully
        # mocked scenarios), so a plain synthetic sentinel string is used
        # here -- mirroring the "abc123" sentinel already used above for
        # _calculate_auto_update_hash -- and the patch.dict(os.environ)
        # entry below restores the real environment on exit so this
        # sentinel never leaks into a later test in the same pytest
        # process.
        # _ensure_golden_repos_symlink_for_cow_daemon and
        # _ensure_cow_storage_mount_options share the identical
        # clone_backend != "cow-daemon" accidental short-circuit as the two
        # Bug #1731-named methods above, so the same fix applies to them.
        patch.object(
            executor, "_deploy_tmpdir", return_value="test-deploy-tmp-sentinel"
        ),
        patch.object(
            executor, "_ensure_golden_repos_symlink_for_cow_daemon", return_value=True
        ),
        patch.object(executor, "_ensure_cow_storage_mount_options", return_value=True),
        # Code review fix (935dfaa4 REQUIRED FIX 1): _ensure_cli_hnswlib_capability
        # was previously left unguarded on the mistaken belief that the
        # tests' own shutil.which patches cover it the same way they cover
        # ensure_scip_python. They do NOT: _get_cli_python_interpreter()
        # (deployment_executor.py:1245) proceeds past shutil.which("cidx")
        # to a real Path(cidx_bin).read_text() call regardless of what
        # shutil.which returns, and on a host where /usr/bin/cidx genuinely
        # exists (a plausible layout for a root-level pip install or a CI
        # container -- just not this dev machine, where the real cidx is at
        # ~/.local/bin/cidx) this reaches a real subprocess.run call that
        # would consume test_execute_continues_when_ensure_codex_fails's
        # single-element subprocess.run side_effect list meant for a
        # different call site, crashing with StopIteration. Guarding this
        # explicitly removes the accidental, machine-state-dependent safety
        # this exact method previously relied on.
        patch.object(executor, "_ensure_cli_hnswlib_capability", return_value=True),
        # Optional consistency fix noted in code review: _write_status_file
        # shares the identical dead-branch gate (_calculate_auto_update_hash
        # patched to a constant above) as _restart_auto_update_service,
        # which is already guarded above for explicit-isolation-discipline
        # consistency -- guarded here for the same reason.
        patch.object(executor, "_write_status_file", return_value=None),
        # Code review fix (935dfaa4 REQUIRED FIX 2): execute() assigns
        # _deploy_tmpdir()'s return value into the PROCESS-GLOBAL
        # os.environ["TMPDIR"] with nothing restoring it afterward.
        # patch.dict(os.environ) snapshots the real environment on enter
        # and restores it verbatim on exit, so this test's sentinel value
        # can never leak into a later test in the same pytest process
        # (relevant under pytest-randomly reordering, and for any later
        # test that spawns a subprocess trusting TMPDIR -- pip, npm, git,
        # cargo, ripgrep).
        patch.dict(os.environ),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


def _install_ok():
    """Return a Mock simulating a successful npm install."""
    return MagicMock(returncode=0, stdout="added 1 package", stderr="")


def _version_ok(version="@openai/codex 0.1.0"):
    """Return a Mock simulating a successful codex --version probe."""
    return MagicMock(returncode=0, stdout=version, stderr="")


# ---------------------------------------------------------------------------
# AC1: npm absent -> WARNING + return True (optional-feature semantics)
# ---------------------------------------------------------------------------


def test_npm_missing_logs_warning_and_returns_true(executor, caplog):
    """When npm is not on PATH, function logs WARNING and returns True.

    Optional-feature semantics: absence of npm is not a fatal condition.
    CIDX must continue; Codex is effectively disabled.
    """
    with (
        patch("shutil.which", return_value=None),
        patch("subprocess.run") as mock_run,
        caplog.at_level(logging.WARNING),
    ):
        result = executor._ensure_codex_cli_installed()

    assert result is True, "Must return True when npm is absent (optional-feature)"
    mock_run.assert_not_called()
    assert any(
        "npm" in record.message.lower()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ), "A WARNING log mentioning npm must be emitted"


# ---------------------------------------------------------------------------
# AC2: npm present, install succeeds -> INFO version logged, return True
# ---------------------------------------------------------------------------


def test_npm_present_install_success_logs_version_at_info(executor, caplog):
    """When npm is on PATH and install succeeds, version string is logged at INFO."""
    version_string = "@openai/codex 0.1.2"

    with (
        _patch_npm_env([_install_ok(), _version_ok(version_string)]),
        caplog.at_level(logging.INFO),
    ):
        result = executor._ensure_codex_cli_installed()

    assert result is True
    info_messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
    assert any("0.1.2" in msg or "codex" in msg.lower() for msg in info_messages), (
        f"INFO log must contain version string; got info records: {info_messages}"
    )


# ---------------------------------------------------------------------------
# AC3: npm install fails (nonzero) -> WARNING, return False, no version probe
# ---------------------------------------------------------------------------


def test_npm_install_nonzero_returncode_logs_warning_returns_false(executor, caplog):
    """When npm install returns nonzero, function logs WARNING and returns False.

    The codex --version probe must NOT be attempted after a failed install.
    CIDX must not raise — non-fatal contract.
    """
    install_fail = MagicMock(
        returncode=1, stdout="", stderr="EACCES: permission denied"
    )

    with (
        _patch_npm_env([install_fail]) as mock_run,
        caplog.at_level(logging.WARNING),
    ):
        result = executor._ensure_codex_cli_installed()

    assert result is False, "Must return False when npm install exits nonzero"
    assert mock_run.call_count == 1, (
        "codex --version must NOT be probed after failed install"
    )
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "At least one WARNING must be emitted"
    )


# ---------------------------------------------------------------------------
# AC4: version probe raises FileNotFoundError -> WARNING, return True
# ---------------------------------------------------------------------------


def test_codex_version_probe_fails_logs_warning_returns_true(executor, caplog):
    """When install succeeds but codex --version raises FileNotFoundError, return True.

    Design choice: return True because npm reported install success (returncode 0).
    The binary not being callable immediately is unusual (PATH propagation delay,
    shell hash cache) but is not the auto-updater's fault. WARNING surfaces the anomaly.
    """
    with (
        _patch_npm_env([_install_ok(), FileNotFoundError("codex not found")]),
        caplog.at_level(logging.WARNING),
    ):
        result = executor._ensure_codex_cli_installed()

    assert result is True, (
        "Must return True when install succeeded but version probe raised FileNotFoundError"
    )
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "A WARNING must be emitted when post-install probe fails"
    )


# ---------------------------------------------------------------------------
# AC5: idempotent second call -> both calls return True, no exception
# ---------------------------------------------------------------------------


def test_idempotent_second_run_no_error(executor):
    """Calling _ensure_codex_cli_installed twice in a row must not raise.

    Both calls must return True when npm is present and install succeeds.
    """
    success_runs = [_install_ok(), _version_ok(), _install_ok(), _version_ok()]

    with _patch_npm_env(success_runs):
        result1 = executor._ensure_codex_cli_installed()
        result2 = executor._ensure_codex_cli_installed()

    assert result1 is True, "First call must return True"
    assert result2 is True, "Second call must return True (idempotent)"


# ---------------------------------------------------------------------------
# AC6: execute() wires _ensure_codex_cli_installed (called at least once)
# ---------------------------------------------------------------------------


def test_execute_wires_ensure_codex_cli_installed(executor, caplog):
    """execute() must call _ensure_codex_cli_installed at least once.

    Following the established project pattern (test_execute_calls_ensure_claude_cli_updated):
    sibling steps are patched, _ensure_codex_cli_installed runs for real with
    shutil.which and subprocess.run as the true external boundaries.

    npm is absent so the method hits the optional-feature branch, emitting a
    WARNING — which is used as observable proof that execute() reached the method.
    """
    with (
        _patch_execute_siblings(executor),
        patch("shutil.which", return_value=None),
        caplog.at_level(logging.WARNING),
    ):
        executor.execute()

    assert any(
        "npm" in record.message.lower()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ), (
        "execute() must have reached _ensure_codex_cli_installed: "
        "expected a WARNING about npm being absent"
    )


# ---------------------------------------------------------------------------
# AC7: subprocess.TimeoutExpired -> WARNING, return False, no re-raise
# ---------------------------------------------------------------------------


def test_ensure_codex_cli_does_not_raise_on_subprocess_timeout(executor, caplog):
    """When subprocess.run raises TimeoutExpired, function catches it and returns False.

    The exception must not propagate. CIDX continues despite the timeout.
    """
    timeout_error = subprocess.TimeoutExpired(
        cmd=CODEX_INSTALL_CMD,
        timeout=CODEX_INSTALL_TIMEOUT_SECONDS,
    )

    with (
        _patch_npm_env([timeout_error]),
        caplog.at_level(logging.WARNING),
    ):
        result = executor._ensure_codex_cli_installed()

    assert result is False, "Must return False when npm install times out"
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "A WARNING must be emitted on timeout"
    )


# ---------------------------------------------------------------------------
# AC8: execute() continues even when _ensure_codex_cli_installed returns False
# ---------------------------------------------------------------------------


def test_execute_continues_when_ensure_codex_fails(executor):
    """execute() must proceed with subsequent steps when _ensure_codex_cli_installed returns False.

    Following the established project pattern: sibling steps are patched,
    _ensure_codex_cli_installed runs for real (npm present but install fails),
    returning False naturally. _ensure_claude_cli_updated is a spy that records
    its own invocation, confirming the non-blocking contract.
    """
    steps_called = []

    def record_claude(*args, **kwargs):
        steps_called.append("claude_cli_updated")
        return True

    install_fail = MagicMock(returncode=1, stdout="", stderr="EACCES")

    with (
        _patch_execute_siblings(executor, claude_side_effect=record_claude),
        patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}"),
        patch("subprocess.run", side_effect=[install_fail]),
    ):
        executor.execute()

    assert "claude_cli_updated" in steps_called, (
        "execute() must call _ensure_claude_cli_updated even after "
        "_ensure_codex_cli_installed returns False"
    )
