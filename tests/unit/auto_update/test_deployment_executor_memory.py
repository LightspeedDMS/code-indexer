"""
Unit tests for Story #356: Auto-Updater Memory Overcommit and Swap Configuration.

Tests for two new idempotent methods in DeploymentExecutor:
- _ensure_memory_overcommit(): Configures vm.overcommit_memory=1 via sysctl
- _ensure_swap_file(): Creates and enables a 4GB /swapfile

Both methods follow the _ensure_sudoers_restart() pattern:
- subprocess.run() with capture_output=True, text=True
- try/except with format_error_log using unique DEPLOY-GENERAL-09x codes
- Return bool (True on success or already-configured, False on error)
- Non-fatal: deployment continues even if they fail
"""

from unittest.mock import MagicMock, patch, call
import pytest

from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor


@pytest.fixture
def executor(tmp_path):
    """Create a DeploymentExecutor instance for testing."""
    return DeploymentExecutor(
        repo_path=tmp_path,
        branch="master",
        service_name="cidx-server",
        server_url="http://localhost:8000",
    )


def make_proc(returncode=0, stdout="", stderr=""):
    """Helper to create a mock subprocess.CompletedProcess result."""
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


# ---------------------------------------------------------------------------
# Tests for _ensure_memory_overcommit()
# ---------------------------------------------------------------------------


class TestEnsureMemoryOvercommit:
    """Tests for the _ensure_memory_overcommit() idempotent sysctl method."""

    def test_memory_overcommit_already_configured(self, executor, caplog):
        """
        When sysctl -n vm.overcommit_memory returns "1",
        the method must return True without writing any config file.
        """
        import logging

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_proc(returncode=0, stdout="1\n")

            with caplog.at_level(logging.DEBUG):
                result = executor._ensure_memory_overcommit()

        assert result is True
        # Only one subprocess call: the sysctl read
        assert mock_run.call_count == 1
        called_cmd = mock_run.call_args[0][0]
        assert called_cmd == ["sysctl", "-n", "vm.overcommit_memory"]
        # Debug message should mention "already"
        assert any("already" in r.message.lower() for r in caplog.records)

    def test_memory_overcommit_configures_successfully(self, executor, caplog):
        """
        When sysctl returns "0" (not yet configured),
        the method must write the config file via sudo tee,
        apply it via sysctl -p, and return True.
        """
        import logging

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                make_proc(returncode=0, stdout="0\n"),  # sysctl -n read
                make_proc(returncode=0, stdout=""),  # sudo tee write
                make_proc(returncode=0, stdout=""),  # sudo sysctl -p apply
            ]

            with caplog.at_level(logging.INFO):
                result = executor._ensure_memory_overcommit()

        assert result is True
        assert mock_run.call_count == 3

        # First call: read current value
        assert mock_run.call_args_list[0] == call(
            ["sysctl", "-n", "vm.overcommit_memory"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Second call: write config file via sudo tee
        assert mock_run.call_args_list[1] == call(
            ["sudo", "tee", "/etc/sysctl.d/99-cidx-memory.conf"],
            input="vm.overcommit_memory = 1\n",
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Third call: apply immediately via sysctl -p
        assert mock_run.call_args_list[2] == call(
            ["sudo", "sysctl", "-p", "/etc/sysctl.d/99-cidx-memory.conf"],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_memory_overcommit_write_failure(self, executor, caplog):
        """
        When sudo tee returns non-zero (write fails),
        the method must log DEPLOY-GENERAL-090 and return False.
        """
        import logging

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                make_proc(returncode=0, stdout="0\n"),  # sysctl -n read
                make_proc(returncode=1, stderr="Permission denied"),  # sudo tee fails
            ]

            with caplog.at_level(logging.ERROR):
                result = executor._ensure_memory_overcommit()

        assert result is False
        assert any("DEPLOY-GENERAL-090" in r.message for r in caplog.records)

    def test_memory_overcommit_apply_failure(self, executor, caplog):
        """
        When sysctl -p returns non-zero (apply fails),
        the method must log DEPLOY-GENERAL-091 and return False.
        """
        import logging

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                make_proc(returncode=0, stdout="0\n"),  # sysctl -n read
                make_proc(returncode=0, stdout=""),  # sudo tee write succeeds
                make_proc(returncode=1, stderr="sysctl: error"),  # sysctl -p fails
            ]

            with caplog.at_level(logging.ERROR):
                result = executor._ensure_memory_overcommit()

        assert result is False
        assert any("DEPLOY-GENERAL-091" in r.message for r in caplog.records)

    def test_memory_overcommit_exception_handling(self, executor, caplog):
        """
        When subprocess.run raises an unexpected exception,
        the method must log DEPLOY-GENERAL-092 and return False.
        """
        import logging

        with patch("subprocess.run", side_effect=OSError("Unexpected error")):
            with caplog.at_level(logging.ERROR):
                result = executor._ensure_memory_overcommit()

        assert result is False
        assert any("DEPLOY-GENERAL-092" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Tests for _ensure_swap_file()
# ---------------------------------------------------------------------------


class TestEnsureSwapFile:
    """Tests for the _ensure_swap_file() idempotent swap provisioning method."""

    def test_swap_already_exists(self, executor, caplog):
        """
        When swapon --show returns non-empty output (swap exists),
        the method must return True without any creation steps.
        """
        import logging

        existing_swap_output = "/swapfile file 4G 1.2G -2\n"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = make_proc(returncode=0, stdout=existing_swap_output)

            with caplog.at_level(logging.DEBUG):
                result = executor._ensure_swap_file()

        assert result is True
        # Only one subprocess call: swapon --show
        assert mock_run.call_count == 1
        called_cmd = mock_run.call_args[0][0]
        assert called_cmd == ["swapon", "--show", "--noheadings"]
        # Debug message should mention "already"
        assert any("already" in r.message.lower() for r in caplog.records)

    def test_swap_creates_full_sequence(self, executor, caplog):
        """
        When no swap exists (swapon --show returns empty),
        the method must execute the full creation sequence:
        fallocate, chmod, mkswap, swapon, check/append fstab, return True.
        """
        import logging

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                make_proc(returncode=0, stdout=""),  # swapon --show (no swap)
                make_proc(returncode=0, stdout=""),  # fallocate
                make_proc(returncode=0, stdout=""),  # chmod 600
                make_proc(returncode=0, stdout=""),  # mkswap
                make_proc(returncode=0, stdout=""),  # swapon
                make_proc(
                    returncode=0, stdout="/etc/fstab content without swapfile"
                ),  # cat /etc/fstab
                make_proc(returncode=0, stdout=""),  # tee -a /etc/fstab
            ]

            with caplog.at_level(logging.INFO):
                result = executor._ensure_swap_file()

        assert result is True
        assert mock_run.call_count == 7

        calls = mock_run.call_args_list
        # Call 0: swapon --show
        assert calls[0] == call(
            ["swapon", "--show", "--noheadings"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Call 1: fallocate
        assert calls[1] == call(
            ["sudo", "fallocate", "-l", "4G", "/swapfile"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        # Call 2: chmod 600
        assert calls[2] == call(
            ["sudo", "chmod", "600", "/swapfile"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Call 3: mkswap
        assert calls[3] == call(
            ["sudo", "mkswap", "/swapfile"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Call 4: swapon
        assert calls[4] == call(
            ["sudo", "swapon", "/swapfile"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Call 5: cat /etc/fstab
        assert calls[5] == call(
            ["cat", "/etc/fstab"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Call 6: tee -a /etc/fstab
        assert calls[6] == call(
            ["sudo", "tee", "-a", "/etc/fstab"],
            input="/swapfile none swap sw 0 0\n",
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_swap_fallocate_failure(self, executor, caplog):
        """
        When fallocate returns non-zero,
        the method must log DEPLOY-GENERAL-093 and return False.
        """
        import logging

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                make_proc(returncode=0, stdout=""),  # swapon --show (no swap)
                make_proc(
                    returncode=1, stderr="fallocate: /swapfile: fallocate failed"
                ),  # fallocate fails
            ]

            with caplog.at_level(logging.ERROR):
                result = executor._ensure_swap_file()

        assert result is False
        assert any("DEPLOY-GENERAL-093" in r.message for r in caplog.records)

    def test_swap_chmod_failure(self, executor, caplog):
        """
        When chmod returns non-zero,
        the method must log DEPLOY-GENERAL-094 and return False.
        """
        import logging

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                make_proc(returncode=0, stdout=""),  # swapon --show (no swap)
                make_proc(returncode=0, stdout=""),  # fallocate succeeds
                make_proc(returncode=1, stderr="chmod: cannot access"),  # chmod fails
            ]

            with caplog.at_level(logging.ERROR):
                result = executor._ensure_swap_file()

        assert result is False
        assert any("DEPLOY-GENERAL-094" in r.message for r in caplog.records)

    def test_swap_mkswap_failure(self, executor, caplog):
        """
        When mkswap returns non-zero,
        the method must log DEPLOY-GENERAL-095 and return False.
        """
        import logging

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                make_proc(returncode=0, stdout=""),  # swapon --show (no swap)
                make_proc(returncode=0, stdout=""),  # fallocate succeeds
                make_proc(returncode=0, stdout=""),  # chmod succeeds
                make_proc(returncode=1, stderr="mkswap: error"),  # mkswap fails
            ]

            with caplog.at_level(logging.ERROR):
                result = executor._ensure_swap_file()

        assert result is False
        assert any("DEPLOY-GENERAL-095" in r.message for r in caplog.records)

    def test_swap_swapon_failure(self, executor, caplog):
        """
        When swapon /swapfile returns non-zero,
        the method must log DEPLOY-GENERAL-096 and return False.
        """
        import logging

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                make_proc(returncode=0, stdout=""),  # swapon --show (no swap)
                make_proc(returncode=0, stdout=""),  # fallocate succeeds
                make_proc(returncode=0, stdout=""),  # chmod succeeds
                make_proc(returncode=0, stdout=""),  # mkswap succeeds
                make_proc(
                    returncode=1, stderr="swapon: /swapfile: read swap header failed"
                ),  # swapon fails
            ]

            with caplog.at_level(logging.ERROR):
                result = executor._ensure_swap_file()

        assert result is False
        assert any("DEPLOY-GENERAL-096" in r.message for r in caplog.records)

    def test_swap_fstab_already_contains_entry(self, executor, caplog):
        """
        When /etc/fstab already contains "/swapfile",
        the method must NOT append a duplicate entry (no tee -a call).
        """
        import logging

        fstab_with_swap = (
            "UUID=abc123 / ext4 defaults 1 1\n/swapfile none swap sw 0 0\n"
        )

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                make_proc(returncode=0, stdout=""),  # swapon --show (no swap)
                make_proc(returncode=0, stdout=""),  # fallocate
                make_proc(returncode=0, stdout=""),  # chmod 600
                make_proc(returncode=0, stdout=""),  # mkswap
                make_proc(returncode=0, stdout=""),  # swapon
                make_proc(
                    returncode=0, stdout=fstab_with_swap
                ),  # cat /etc/fstab (already has entry)
                # NO tee -a call should happen
            ]

            with caplog.at_level(logging.INFO):
                result = executor._ensure_swap_file()

        assert result is True
        # Should be 6 calls (no tee -a for fstab)
        assert mock_run.call_count == 6

        # Verify tee -a was NOT called
        for c in mock_run.call_args_list:
            cmd = c[0][0]
            assert not (cmd == ["sudo", "tee", "-a", "/etc/fstab"]), (
                "tee -a /etc/fstab should NOT be called when /swapfile already in fstab"
            )

    def test_swap_fstab_append_failure_non_fatal(self, executor, caplog):
        """
        When fstab tee -a fails, the method must:
        - Log a WARNING (not error) with DEPLOY-GENERAL-097
        - Still return True (swap IS active, just not reboot-persistent)
        """
        import logging

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                make_proc(returncode=0, stdout=""),  # swapon --show (no swap)
                make_proc(returncode=0, stdout=""),  # fallocate
                make_proc(returncode=0, stdout=""),  # chmod 600
                make_proc(returncode=0, stdout=""),  # mkswap
                make_proc(returncode=0, stdout=""),  # swapon
                make_proc(
                    returncode=0, stdout="other fstab content without swapfile"
                ),  # cat /etc/fstab
                make_proc(
                    returncode=1, stderr="tee: /etc/fstab: Permission denied"
                ),  # tee -a fails
            ]

            with caplog.at_level(logging.WARNING):
                result = executor._ensure_swap_file()

        # Non-fatal: swap is active, must still return True
        assert result is True
        assert any("DEPLOY-GENERAL-097" in r.message for r in caplog.records)
        # Must be WARNING level, not ERROR
        warn_records = [r for r in caplog.records if "DEPLOY-GENERAL-097" in r.message]
        assert len(warn_records) > 0
        assert all(r.levelname == "WARNING" for r in warn_records)

    def test_swap_exception_handling(self, executor, caplog):
        """
        When subprocess.run raises an unexpected exception,
        the method must log DEPLOY-GENERAL-098 and return False.
        """
        import logging

        with patch(
            "subprocess.run", side_effect=RuntimeError("Unexpected system error")
        ):
            with caplog.at_level(logging.ERROR):
                result = executor._ensure_swap_file()

        assert result is False
        assert any("DEPLOY-GENERAL-098" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Tests for execute() wiring
# ---------------------------------------------------------------------------


class TestExecuteWiring:
    """Tests verifying that new steps 9 and 10 are wired into execute() correctly."""

    def _make_executor_with_mocked_steps(self, tmp_path):
        """
        Create an executor where ALL _ensure_* methods and git/pip operations
        are mocked to succeed, so we can test the wiring of steps 9 and 10.
        """
        return DeploymentExecutor(
            repo_path=tmp_path,
            branch="master",
            service_name="cidx-server",
            server_url="http://localhost:8000",
        )

    def test_execute_calls_memory_overcommit_after_sudoers(self, tmp_path):
        """
        Verify _ensure_memory_overcommit is called in execute() after _ensure_sudoers_restart.
        Use call order tracking to confirm ordering.
        """
        executor = self._make_executor_with_mocked_steps(tmp_path)

        call_order = []

        def mock_sudoers():
            call_order.append("sudoers_restart")
            return True

        def mock_memory_overcommit():
            call_order.append("memory_overcommit")
            return True

        def mock_swap_file():
            call_order.append("swap_file")
            return True

        with (
            patch.object(executor, "git_pull", return_value=True),
            patch.object(executor, "_calculate_auto_update_hash", return_value=None),
            patch.object(executor, "git_submodule_update", return_value=True),
            patch.object(executor, "_build_hnswlib_with_fallback", return_value=True),
            patch.object(executor, "pip_install", return_value=True),
            patch.object(executor, "_ensure_workers_config", return_value=True),
            patch.object(executor, "_ensure_cidx_repo_root", return_value=True),
            patch.object(executor, "_ensure_git_safe_directory", return_value=True),
            patch.object(
                executor, "_ensure_auto_updater_uses_server_python", return_value=True
            ),
            patch.object(executor, "ensure_ripgrep", return_value=True),
            patch.object(executor, "_ensure_sudoers_restart", side_effect=mock_sudoers),
            patch.object(
                executor,
                "_ensure_memory_overcommit",
                side_effect=mock_memory_overcommit,
            ),
            patch.object(executor, "_ensure_swap_file", side_effect=mock_swap_file),
        ):
            result = executor.execute()

        assert result is True
        assert "sudoers_restart" in call_order
        assert "memory_overcommit" in call_order
        sudoers_idx = call_order.index("sudoers_restart")
        memory_idx = call_order.index("memory_overcommit")
        assert memory_idx > sudoers_idx, (
            f"_ensure_memory_overcommit (idx={memory_idx}) must be called "
            f"AFTER _ensure_sudoers_restart (idx={sudoers_idx})"
        )

    def test_execute_calls_swap_file_after_memory_overcommit(self, tmp_path):
        """
        Verify _ensure_swap_file is called after _ensure_memory_overcommit in execute().
        """
        executor = self._make_executor_with_mocked_steps(tmp_path)

        call_order = []

        def mock_memory_overcommit():
            call_order.append("memory_overcommit")
            return True

        def mock_swap_file():
            call_order.append("swap_file")
            return True

        with (
            patch.object(executor, "git_pull", return_value=True),
            patch.object(executor, "_calculate_auto_update_hash", return_value=None),
            patch.object(executor, "git_submodule_update", return_value=True),
            patch.object(executor, "_build_hnswlib_with_fallback", return_value=True),
            patch.object(executor, "pip_install", return_value=True),
            patch.object(executor, "_ensure_workers_config", return_value=True),
            patch.object(executor, "_ensure_cidx_repo_root", return_value=True),
            patch.object(executor, "_ensure_git_safe_directory", return_value=True),
            patch.object(
                executor, "_ensure_auto_updater_uses_server_python", return_value=True
            ),
            patch.object(executor, "ensure_ripgrep", return_value=True),
            patch.object(executor, "_ensure_sudoers_restart", return_value=True),
            patch.object(
                executor,
                "_ensure_memory_overcommit",
                side_effect=mock_memory_overcommit,
            ),
            patch.object(executor, "_ensure_swap_file", side_effect=mock_swap_file),
        ):
            result = executor.execute()

        assert result is True
        assert "memory_overcommit" in call_order
        assert "swap_file" in call_order
        memory_idx = call_order.index("memory_overcommit")
        swap_idx = call_order.index("swap_file")
        assert swap_idx > memory_idx, (
            f"_ensure_swap_file (idx={swap_idx}) must be called "
            f"AFTER _ensure_memory_overcommit (idx={memory_idx})"
        )

    def test_execute_continues_on_memory_overcommit_failure(self, tmp_path, caplog):
        """
        When _ensure_memory_overcommit returns False,
        execute() must:
        - Log a warning with DEPLOY-GENERAL-099
        - Continue (NOT abort)
        - Call _ensure_swap_file anyway
        - Return True (deployment succeeds)
        """
        import logging

        executor = self._make_executor_with_mocked_steps(tmp_path)

        swap_was_called = []

        def mock_swap_file():
            swap_was_called.append(True)
            return True

        with (
            patch.object(executor, "git_pull", return_value=True),
            patch.object(executor, "_calculate_auto_update_hash", return_value=None),
            patch.object(executor, "git_submodule_update", return_value=True),
            patch.object(executor, "_build_hnswlib_with_fallback", return_value=True),
            patch.object(executor, "pip_install", return_value=True),
            patch.object(executor, "_ensure_workers_config", return_value=True),
            patch.object(executor, "_ensure_cidx_repo_root", return_value=True),
            patch.object(executor, "_ensure_git_safe_directory", return_value=True),
            patch.object(
                executor, "_ensure_auto_updater_uses_server_python", return_value=True
            ),
            patch.object(executor, "ensure_ripgrep", return_value=True),
            patch.object(executor, "_ensure_sudoers_restart", return_value=True),
            patch.object(executor, "_ensure_memory_overcommit", return_value=False),
            patch.object(executor, "_ensure_swap_file", side_effect=mock_swap_file),
        ):
            with caplog.at_level(logging.WARNING):
                result = executor.execute()

        assert result is True, (
            "execute() must return True even when _ensure_memory_overcommit fails"
        )
        assert swap_was_called, (
            "_ensure_swap_file must still be called after memory overcommit failure"
        )
        assert any("DEPLOY-GENERAL-099" in r.message for r in caplog.records)

    def test_execute_continues_on_swap_file_failure(self, tmp_path, caplog):
        """
        When _ensure_swap_file returns False,
        execute() must:
        - Log a warning with DEPLOY-GENERAL-100
        - Return True (deployment succeeds - swap failure is non-fatal)
        """
        import logging

        executor = self._make_executor_with_mocked_steps(tmp_path)

        with (
            patch.object(executor, "git_pull", return_value=True),
            patch.object(executor, "_calculate_auto_update_hash", return_value=None),
            patch.object(executor, "git_submodule_update", return_value=True),
            patch.object(executor, "_build_hnswlib_with_fallback", return_value=True),
            patch.object(executor, "pip_install", return_value=True),
            patch.object(executor, "_ensure_workers_config", return_value=True),
            patch.object(executor, "_ensure_cidx_repo_root", return_value=True),
            patch.object(executor, "_ensure_git_safe_directory", return_value=True),
            patch.object(
                executor, "_ensure_auto_updater_uses_server_python", return_value=True
            ),
            patch.object(executor, "ensure_ripgrep", return_value=True),
            patch.object(executor, "_ensure_sudoers_restart", return_value=True),
            patch.object(executor, "_ensure_memory_overcommit", return_value=True),
            patch.object(executor, "_ensure_swap_file", return_value=False),
        ):
            with caplog.at_level(logging.WARNING):
                result = executor.execute()

        assert result is True, (
            "execute() must return True even when _ensure_swap_file fails"
        )
        assert any("DEPLOY-GENERAL-100" in r.message for r in caplog.records)
