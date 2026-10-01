"""Tests for Issue #1440 self-heal: Environment="PATH=..." in cidx-auto-update.service.

Already-deployed hosts predate the #1440 systemd unit TEMPLATE fix (which adds
Environment="PATH={HOME}/.local/bin:..." for fresh installs) and have ZERO
Environment="PATH=" lines in their cidx-auto-update.service file. This breaks
npm/node discovery inside the auto-update subprocess (Codex CLI install,
scip-python install). _ensure_auto_update_service_has_cli_path() self-heals
those already-deployed hosts on the next auto-update deploy cycle.
"""

import pwd
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import Mock, patch

import pytest

from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor


@pytest.fixture
def executor():
    """Create DeploymentExecutor instance for testing."""
    return DeploymentExecutor(
        repo_path=Path("/test/repo"),
        service_name="cidx-server",
    )


def _make_subprocess_run_mock(*service_contents):
    """Build a side_effect list for subprocess.run.

    Each item in service_contents maps to one 'sudo cat' call (there is only
    ONE service file read by _ensure_auto_update_service_has_cli_path -- the
    auto-update unit's own content, unlike _ensure_data_dir_env_var which
    cross-references two unit files).  Subsequent calls (sudo tee, systemctl
    daemon-reload, systemctl restart) return success by default.
    """
    read_responses = [
        Mock(returncode=0, stdout=content, stderr="") for content in service_contents
    ]
    write_success = Mock(returncode=0, stdout="", stderr="")
    return read_responses + [write_success, write_success, write_success]


def _auto_update_service_with_correct_path(home: Path) -> str:
    """Build auto-update service content with the correct PATH env line."""
    return f"""\
[Unit]
Description=CIDX Auto-Update

[Service]
User=root
Environment="CIDX_SERVER_REPO_PATH=/opt/code-indexer"
Environment="PATH={home}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
ExecStart=/usr/bin/python3 -m code_indexer.server.auto_update.watcher
Restart=always

[Install]
WantedBy=multi-user.target
"""


_AUTO_UPDATE_SERVICE_NO_PATH_LINE = """\
[Unit]
Description=CIDX Auto-Update

[Service]
User=root
Environment="CIDX_SERVER_REPO_PATH=/opt/code-indexer"
Environment="CIDX_AUTO_UPDATE_BRANCH=staging"
ExecStart=/usr/bin/python3 -m code_indexer.server.auto_update.watcher
Restart=always

[Install]
WantedBy=multi-user.target
"""


class TestEnsureAutoUpdateServiceHasCliPath:
    """Tests for DeploymentExecutor._ensure_auto_update_service_has_cli_path()."""

    def test_already_correct_returns_true_no_write_no_restart(self, executor):
        """Test 1: existing correct Environment="PATH=..." line -> no-op.

        The auto-update unit runs as User=root, so the expected PATH is
        rooted at root's home directory.
        """
        root_home = Path(pwd.getpwnam("root").pw_dir)
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _make_subprocess_run_mock(
                _auto_update_service_with_correct_path(root_home)
            )
            result = executor._ensure_auto_update_service_has_cli_path()

        assert result is True

        tee_calls = [c for c in mock_run.call_args_list if "tee" in c[0][0]]
        restart_calls = [c for c in mock_run.call_args_list if "restart" in c[0][0]]
        assert len(tee_calls) == 0, "sudo tee should NOT be called when already correct"
        assert len(restart_calls) == 0, (
            "systemctl restart should NOT be called when already correct"
        )

    def test_missing_path_line_injects_correct_line_preserving_existing_env(
        self, executor
    ):
        """Test 2: zero Environment="PATH=" lines -> injects the correct line
        after the last existing Environment= line, calls write+reload+restart
        exactly once, and does NOT corrupt Environment="CIDX_SERVER_REPO_PATH=..."
        (proving the marker-specific filter, not a naive "PATH" substring check).
        """
        root_home = Path(pwd.getpwnam("root").pw_dir)
        expected_line = (
            f'Environment="PATH={root_home}/.local/bin:/usr/local/sbin:'
            f'/usr/local/bin:/usr/sbin:/usr/bin"'
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _make_subprocess_run_mock(
                _AUTO_UPDATE_SERVICE_NO_PATH_LINE
            )
            result = executor._ensure_auto_update_service_has_cli_path()

        assert result is True

        tee_calls = [c for c in mock_run.call_args_list if "tee" in c[0][0]]
        assert len(tee_calls) == 1, "sudo tee should be called exactly once"

        written_content = tee_calls[0][1]["input"]
        assert expected_line in written_content, (
            f"Written content should contain {expected_line}"
        )
        assert (
            'Environment="CIDX_SERVER_REPO_PATH=/opt/code-indexer"' in written_content
        ), (
            "Existing CIDX_SERVER_REPO_PATH line must be preserved untouched -- "
            "proves the filter is PATH-marker-specific, not a bare substring check"
        )
        assert 'Environment="CIDX_AUTO_UPDATE_BRANCH=staging"' in written_content, (
            "Existing CIDX_AUTO_UPDATE_BRANCH line must be preserved untouched"
        )

        lines = written_content.splitlines()
        branch_idx = next(
            i for i, line in enumerate(lines) if "CIDX_AUTO_UPDATE_BRANCH" in line
        )
        path_idx = next(i for i, line in enumerate(lines) if expected_line in line)
        assert path_idx == branch_idx + 1, (
            "Injected PATH line should immediately follow the last existing "
            "Environment= line"
        )

        reload_calls = [
            c for c in mock_run.call_args_list if "daemon-reload" in c[0][0]
        ]
        assert len(reload_calls) == 1, (
            "systemctl daemon-reload should be called exactly once"
        )

        restart_calls = [c for c in mock_run.call_args_list if "restart" in c[0][0]]
        assert len(restart_calls) == 1, (
            "systemctl restart should be called exactly once"
        )

    def test_no_environment_lines_at_all_injects_after_service_header(self, executor):
        """Test 3: unit file has NO Environment= lines at all -> injects the
        PATH line immediately after the [Service] header."""
        root_home = Path(pwd.getpwnam("root").pw_dir)
        expected_line = (
            f'Environment="PATH={root_home}/.local/bin:/usr/local/sbin:'
            f'/usr/local/bin:/usr/sbin:/usr/bin"'
        )
        auto_update_no_env = """\
[Unit]
Description=CIDX Auto-Update

[Service]
User=root
ExecStart=/usr/bin/python3 -m code_indexer.server.auto_update.watcher
Restart=always

[Install]
WantedBy=multi-user.target
"""

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = _make_subprocess_run_mock(auto_update_no_env)
            result = executor._ensure_auto_update_service_has_cli_path()

        assert result is True

        tee_calls = [c for c in mock_run.call_args_list if "tee" in c[0][0]]
        assert len(tee_calls) == 1, "sudo tee should be called exactly once"

        written_content = tee_calls[0][1]["input"]
        assert expected_line in written_content

        lines = written_content.splitlines()
        service_idx = next(
            i for i, line in enumerate(lines) if line.strip() == "[Service]"
        )
        path_idx = next(i for i, line in enumerate(lines) if expected_line in line)
        assert path_idx == service_idx + 1, (
            "PATH line should be inserted immediately after [Service] header "
            "when no Environment= lines exist"
        )

    def test_read_service_file_returns_none_returns_false(self, executor):
        """Test 4: auto-update unit file missing/unreadable -> returns False,
        no crash, no sudo tee/restart calls."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = Mock(
                returncode=1, stdout="", stderr="No such file or directory"
            )
            result = executor._ensure_auto_update_service_has_cli_path()

        assert result is False

        tee_calls = [c for c in mock_run.call_args_list if "tee" in c[0][0]]
        restart_calls = [c for c in mock_run.call_args_list if "restart" in c[0][0]]
        assert len(tee_calls) == 0, "sudo tee should NOT be called when read fails"
        assert len(restart_calls) == 0, (
            "systemctl restart should NOT be called when read fails"
        )

    def test_write_failure_returns_false_before_restart(self, executor):
        """Test 5: sudo tee (write) fails -> returns False, systemctl restart
        is NEVER called afterward (fail-fast, matching _ensure_data_dir_env_var's
        exact control flow: write failure short-circuits before restart)."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                Mock(
                    returncode=0, stdout=_AUTO_UPDATE_SERVICE_NO_PATH_LINE, stderr=""
                ),  # sudo cat
                Mock(returncode=1, stdout="", stderr="tee failed"),  # sudo tee FAILS
            ]
            result = executor._ensure_auto_update_service_has_cli_path()

        assert result is False

        restart_calls = [c for c in mock_run.call_args_list if "restart" in c[0][0]]
        assert len(restart_calls) == 0, (
            "systemctl restart must NOT be called after a write failure"
        )

    def test_restart_failure_returns_false(self, executor):
        """Test 6: write+reload succeed but systemctl restart fails ->
        returns False."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                Mock(
                    returncode=0, stdout=_AUTO_UPDATE_SERVICE_NO_PATH_LINE, stderr=""
                ),  # sudo cat
                Mock(returncode=0, stdout="", stderr=""),  # sudo tee
                Mock(returncode=0, stdout="", stderr=""),  # daemon-reload
                Mock(returncode=1, stdout="", stderr="restart failed"),  # restart FAILS
            ]
            result = executor._ensure_auto_update_service_has_cli_path()

        assert result is False, (
            "Must return False when restart fails so the caller's warning "
            "log fires at the execute() level"
        )

    def test_no_user_line_resolves_home_for_root(self, executor):
        """Test 7: no User= line in the auto-update unit -> resolves home for
        "root" specifically (a oneshot with no explicit User= runs as root
        under systemd) -- NOT any other user.

        pwd.getpwnam is mocked to raise KeyError for any username other than
        "root" so this test proves the exact lookup, not merely a lookup
        that happens to also succeed for the current test-runner user.
        """
        fake_root_home = "/fake/root/home"
        fake_root_pwentry = pwd.struct_passwd(
            ("root", "x", 0, 0, "root", fake_root_home, "/bin/bash")
        )

        def fake_getpwnam(username):
            if username == "root":
                return fake_root_pwentry
            raise KeyError(f"getpwnam(): name not found: {username!r}")

        auto_update_no_user = """\
[Unit]
Description=CIDX Auto-Update

[Service]
Environment="CIDX_SERVER_REPO_PATH=/opt/code-indexer"
ExecStart=/usr/bin/python3 -m code_indexer.server.auto_update.watcher
Restart=always

[Install]
WantedBy=multi-user.target
"""
        expected_line = (
            f'Environment="PATH={fake_root_home}/.local/bin:/usr/local/sbin:'
            f'/usr/local/bin:/usr/sbin:/usr/bin"'
        )

        with patch(
            "code_indexer.server.auto_update.deployment_executor.pwd.getpwnam",
            side_effect=fake_getpwnam,
        ) as mock_getpwnam:
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = _make_subprocess_run_mock(auto_update_no_user)
                result = executor._ensure_auto_update_service_has_cli_path()

        assert result is True
        mock_getpwnam.assert_called_once_with("root")

        tee_calls = [c for c in mock_run.call_args_list if "tee" in c[0][0]]
        assert len(tee_calls) == 1
        written_content = tee_calls[0][1]["input"]
        assert expected_line in written_content, (
            f"Expected PATH rooted at root's home ({fake_root_home}), "
            f"got: {written_content}"
        )


@contextmanager
def _all_steps_except_auto_update_path(
    executor: DeploymentExecutor,
) -> Iterator[None]:
    """Mock the 19 execute() steps this file's tests must isolate, except
    _ensure_auto_update_service_has_cli_path.

    Shared by both TestExecuteWiring tests below to avoid duplicating this
    patch list (mirrors test_rust_toolchain.py's _all_steps_except_rust).
    Includes _build_hnswlib_with_fallback (Bug #1651/#1640): with a fake
    repo_path, its unmocked fallback branch does a real `git clone`. Also
    includes _ensure_cli_dependencies_synced (Bug #1715): with a fake
    repo_path, its unmocked branch does a real `pip install -e .`.
    """
    with (
        patch.object(
            executor, "_ensure_git_safe_directory_wildcard", return_value=True
        ),
        patch.object(executor, "_ensure_malloc_arena_max", return_value=True),
        patch.object(executor, "_ensure_data_dir_env_var", return_value=True),
        patch.object(
            executor, "_ensure_auto_updater_uses_server_python", return_value=True
        ),
        patch.object(executor, "ensure_ripgrep", return_value=True),
        patch.object(executor, "_ensure_git_safe_directory", return_value=True),
        patch.object(executor, "_ensure_cidx_repo_root", return_value=True),
        patch.object(executor, "_ensure_launch_config", return_value=None),
        patch.object(executor, "pip_install", return_value=True),
        patch.object(executor, "_ensure_cli_dependencies_synced", return_value=True),
        patch.object(executor, "build_custom_hnswlib", return_value=True),
        patch.object(executor, "_build_hnswlib_with_fallback", return_value=True),
        patch.object(executor, "git_submodule_update", return_value=True),
        patch.object(executor, "_calculate_auto_update_hash", return_value="same_hash"),
        patch.object(executor, "git_pull", return_value=True),
        patch.object(executor, "_ensure_rust_toolchain", return_value=True),
        patch.object(executor, "_ensure_claude_cli_updated", return_value=True),
        patch.object(executor, "_ensure_codex_cli_installed", return_value=True),
        patch.object(executor, "_ensure_pace_maker_installed", return_value=True),
    ):
        yield


@pytest.mark.slow
class TestExecuteWiring:
    """Tests for execute() calling _ensure_auto_update_service_has_cli_path()."""

    def test_execute_calls_ensure_auto_update_path(self, executor):
        """Test that execute() calls _ensure_auto_update_service_has_cli_path()."""
        with (
            _all_steps_except_auto_update_path(executor),
            patch.object(
                executor,
                "_ensure_auto_update_service_has_cli_path",
                return_value=True,
            ) as mock_ensure_auto_update_path,
        ):
            result = executor.execute()

        assert result is True
        mock_ensure_auto_update_path.assert_called_once()

    def test_execute_continues_on_ensure_auto_update_path_failure(self, executor):
        """Test that execute() still returns True overall when
        _ensure_auto_update_service_has_cli_path() returns False (non-fatal
        contract, matching the sibling self-heal methods' exact pattern)."""
        with (
            _all_steps_except_auto_update_path(executor),
            patch.object(
                executor,
                "_ensure_auto_update_service_has_cli_path",
                return_value=False,
            ) as mock_ensure_auto_update_path,
        ):
            result = executor.execute()

        assert result is True
        mock_ensure_auto_update_path.assert_called_once()
