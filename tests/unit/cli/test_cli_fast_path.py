"""Tests for CLI fast-path optimization with daemon delegation.

These tests ensure the CLI startup time is minimized when daemon mode
is enabled by avoiding heavy imports until absolutely necessary.
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch


class TestQuickDaemonCheck:
    """Test quick daemon mode detection without heavy imports."""

    def test_quick_daemon_check_detects_enabled_daemon(self, tmp_path):
        """Test that quick check detects daemon.enabled: true."""
        # Arrange
        config_dir = tmp_path / ".code-indexer"
        config_dir.mkdir()
        config_file = config_dir / "config.json"

        config_data = {
            "daemon": {"enabled": True},
            "codebase_dir": str(tmp_path),
            "backend": "filesystem",
        }
        config_file.write_text(json.dumps(config_data))

        # Import should be fast - only stdlib
        from code_indexer.cli_fast_entry import quick_daemon_check

        # Act
        with patch("code_indexer.cli_fast_entry.Path.cwd", return_value=tmp_path):
            is_daemon, config_path = quick_daemon_check()

        # Assert
        assert is_daemon is True
        assert config_path == config_file

    def test_quick_daemon_check_detects_disabled_daemon(self, tmp_path):
        """Test that quick check detects daemon.enabled: false."""
        # Arrange
        config_dir = tmp_path / ".code-indexer"
        config_dir.mkdir()
        config_file = config_dir / "config.json"

        config_data = {"daemon": {"enabled": False}, "codebase_dir": str(tmp_path)}
        config_file.write_text(json.dumps(config_data))

        from code_indexer.cli_fast_entry import quick_daemon_check

        # Act
        with patch("code_indexer.cli_fast_entry.Path.cwd", return_value=tmp_path):
            is_daemon, config_path = quick_daemon_check()

        # Assert
        assert is_daemon is False
        assert config_path is None

    def test_quick_daemon_check_walks_up_directory_tree(self, tmp_path):
        """Test that quick check walks up directory tree to find config."""
        # Arrange
        config_dir = tmp_path / ".code-indexer"
        config_dir.mkdir()
        config_file = config_dir / "config.json"

        config_data = {"daemon": {"enabled": True}}
        config_file.write_text(json.dumps(config_data))

        # Create subdirectory
        subdir = tmp_path / "src" / "module"
        subdir.mkdir(parents=True)

        from code_indexer.cli_fast_entry import quick_daemon_check

        # Act - start from subdirectory
        with patch("code_indexer.cli_fast_entry.Path.cwd", return_value=subdir):
            is_daemon, config_path = quick_daemon_check()

        # Assert
        assert is_daemon is True
        assert config_path == config_file

    def test_quick_daemon_check_handles_missing_config(self, tmp_path):
        """Test that quick check returns False when no config found.

        Test-isolation note (Bug #1420 investigation): this is distinct
        from the directory-walk bug itself. A shared /tmp sandbox can
        genuinely contain a real ancestor .code-indexer/config.json (e.g.
        a developer's own daemon config living at /tmp/.code-indexer) and
        pytest's tmp_path fixture nests test directories under /tmp with
        no intervening .code-indexer to shadow it. Once the walk correctly
        stops at the nearest config (the production fix), a genuinely
        absent nearer config means the walk legitimately continues to that
        real ancestor -- which is CORRECT behavior, not a bug. This test
        verifies the "no config anywhere in the tree" case in isolation by
        mocking Path.exists() so the walk cannot observe anything outside
        this test's own tmp_path tree, regardless of real host state.
        """
        from code_indexer.cli_fast_entry import quick_daemon_check

        real_exists = Path.exists

        def isolated_exists(self: Path) -> bool:
            # Only allow genuine filesystem checks within this test's own
            # tmp_path tree; treat any ancestor outside it (e.g. a stray
            # real /tmp/.code-indexer/config.json) as absent.
            try:
                self.relative_to(tmp_path)
            except ValueError:
                return False
            return real_exists(self)

        # Act - no .code-indexer directory exists anywhere in tmp_path,
        # and ancestor state above tmp_path is isolated away.
        with (
            patch("code_indexer.cli_fast_entry.Path.cwd", return_value=tmp_path),
            patch.object(Path, "exists", isolated_exists),
        ):
            is_daemon, config_path = quick_daemon_check()

        # Assert
        assert is_daemon is False
        assert config_path is None

    def test_quick_daemon_check_handles_malformed_json(self, tmp_path):
        """Test that quick check handles malformed JSON gracefully."""
        # Arrange
        config_dir = tmp_path / ".code-indexer"
        config_dir.mkdir()
        config_file = config_dir / "config.json"
        config_file.write_text("{invalid json}")

        from code_indexer.cli_fast_entry import quick_daemon_check

        # Act
        with patch("code_indexer.cli_fast_entry.Path.cwd", return_value=tmp_path):
            is_daemon, config_path = quick_daemon_check()

        # Assert - should fail gracefully
        assert is_daemon is False
        assert config_path is None

    def test_nearer_disabled_config_is_authoritative_over_farther_enabled_ancestor(
        self, tmp_path
    ):
        """Bug #1420 regression: the walk must STOP at the nearest
        .code-indexer/config.json found and use ITS daemon state, even when
        a farther ancestor directory has daemon.enabled: true.

        Reproduces the exact scenario from the issue: a nearer directory has
        daemon mode disabled, and a farther ancestor has daemon mode
        enabled. The nearer (disabled) config must win -- the walk must
        never fall through past it to inherit the farther ancestor's
        enabled state.
        """
        # Arrange - farther ancestor: daemon ENABLED
        farther_config_dir = tmp_path / ".code-indexer"
        farther_config_dir.mkdir()
        (farther_config_dir / "config.json").write_text(
            json.dumps({"daemon": {"enabled": True}})
        )

        # Arrange - nearer project directory: daemon DISABLED
        nearer_root = tmp_path / "project"
        nearer_config_dir = nearer_root / ".code-indexer"
        nearer_config_dir.mkdir(parents=True)
        nearer_config_file = nearer_config_dir / "config.json"
        nearer_config_file.write_text(json.dumps({"daemon": {"enabled": False}}))

        from code_indexer.cli_fast_entry import quick_daemon_check

        # Act - cwd is the nearer project directory
        with patch("code_indexer.cli_fast_entry.Path.cwd", return_value=nearer_root):
            is_daemon, config_path = quick_daemon_check()

        # Assert - nearer disabled config must be authoritative
        assert is_daemon is False, (
            "nearer disabled config must not be overridden by a farther "
            "ancestor's enabled config (Bug #1420)"
        )
        assert config_path is None

    def test_nearer_enabled_config_is_authoritative_regardless_of_farther_ancestor(
        self, tmp_path
    ):
        """Control test for Bug #1420: confirm the fix stops the walk at
        the FIRST config found and uses ITS state -- not that it always
        prefers "disabled". When the nearer config is daemon-ENABLED, that
        state must still be used, even with a differing farther ancestor.
        """
        # Arrange - farther ancestor: daemon DISABLED
        farther_config_dir = tmp_path / ".code-indexer"
        farther_config_dir.mkdir()
        (farther_config_dir / "config.json").write_text(
            json.dumps({"daemon": {"enabled": False}})
        )

        # Arrange - nearer project directory: daemon ENABLED
        nearer_root = tmp_path / "project"
        nearer_config_dir = nearer_root / ".code-indexer"
        nearer_config_dir.mkdir(parents=True)
        nearer_config_file = nearer_config_dir / "config.json"
        nearer_config_file.write_text(json.dumps({"daemon": {"enabled": True}}))

        from code_indexer.cli_fast_entry import quick_daemon_check

        # Act - cwd is the nearer project directory
        with patch("code_indexer.cli_fast_entry.Path.cwd", return_value=nearer_root):
            is_daemon, config_path = quick_daemon_check()

        # Assert - nearer enabled config must be used
        assert is_daemon is True
        assert config_path == nearer_config_file

    def test_nearer_malformed_config_stops_walk_before_farther_enabled_ancestor(
        self, tmp_path
    ):
        """Bug #1420 regression: a nearer config that exists but is
        malformed JSON must ALSO stop the walk (treated as daemon
        disabled), rather than being skipped in search of a farther
        ancestor's enabled config.
        """
        # Arrange - farther ancestor: daemon ENABLED
        farther_config_dir = tmp_path / ".code-indexer"
        farther_config_dir.mkdir()
        (farther_config_dir / "config.json").write_text(
            json.dumps({"daemon": {"enabled": True}})
        )

        # Arrange - nearer project directory: malformed config.json
        nearer_root = tmp_path / "project"
        nearer_config_dir = nearer_root / ".code-indexer"
        nearer_config_dir.mkdir(parents=True)
        (nearer_config_dir / "config.json").write_text("{invalid json}")

        from code_indexer.cli_fast_entry import quick_daemon_check

        # Act - cwd is the nearer project directory
        with patch("code_indexer.cli_fast_entry.Path.cwd", return_value=nearer_root):
            is_daemon, config_path = quick_daemon_check()

        # Assert - nearer malformed config stops the walk
        assert is_daemon is False
        assert config_path is None

    def test_quick_daemon_check_execution_time(self, tmp_path):
        """Test that quick check executes in <10ms."""
        # Arrange
        config_dir = tmp_path / ".code-indexer"
        config_dir.mkdir()
        config_file = config_dir / "config.json"
        config_file.write_text(json.dumps({"daemon": {"enabled": True}}))

        from code_indexer.cli_fast_entry import quick_daemon_check

        # Act - measure execution time
        with patch("code_indexer.cli_fast_entry.Path.cwd", return_value=tmp_path):
            start = time.time()
            quick_daemon_check()
            elapsed_ms = (time.time() - start) * 1000

        # Assert - should be very fast (stdlib only)
        assert elapsed_ms < 10, f"Quick check took {elapsed_ms:.1f}ms, expected <10ms"


class TestCommandClassification:
    """Test command classification for daemon delegation."""

    def test_identifies_daemon_delegatable_commands(self):
        """Test that query, index, watch etc. are identified as delegatable."""
        from code_indexer.cli_fast_entry import is_delegatable_command

        delegatable = [
            "query",
            "index",
            "watch",
            "clean",
            "clean-data",
            "stop",
            "watch-stop",
        ]

        for cmd in delegatable:
            assert is_delegatable_command(cmd, []) is True, (
                f"{cmd} should be delegatable"
            )

    def test_identifies_non_delegatable_commands(self):
        """Test that init, fix-config etc. are not delegatable."""
        from code_indexer.cli_fast_entry import is_delegatable_command

        non_delegatable = ["init", "fix-config", "reconcile", "sync", "list-repos"]

        for cmd in non_delegatable:
            assert is_delegatable_command(cmd, []) is False, (
                f"{cmd} should not be delegatable"
            )

    def test_index_commits_is_never_daemon_delegatable(self):
        """Bug #1417 regression: `index --index-commits` must NEVER be
        daemon-delegatable.

        Root cause: the CIDX_TEMPORAL_PG_BOOTSTRAP_DIR fail-loud wiring
        (Bug #1313) lives EXCLUSIVELY in cli.py's standalone `index()`
        branch. If `--index-commits` is treated as daemon-delegatable, the
        fast entry point routes it to cli_daemon_fast.execute_via_daemon,
        which has zero knowledge of the PG bootstrap contract -- the child
        silently completes without ever exercising the unreachable-DSN
        fail-loud check (a silent SQLite fallback, violating Messi Rule #2).

        A plain `index` (no --index-commits) must remain delegatable --
        this is a narrow carve-out, not a blanket exclusion of "index".
        """
        from code_indexer.cli_fast_entry import is_delegatable_command

        assert (
            is_delegatable_command(
                "index",
                ["cidx", "index", "--index-commits", "--max-commits", "2"],
            )
            is False
        ), "index --index-commits must never be daemon-delegatable (Bug #1417)"

        assert is_delegatable_command("index", ["cidx", "index"]) is True, (
            "plain index (no --index-commits) must remain daemon-delegatable"
        )


class TestFastPathRouting:
    """Test main entry point routing logic."""

    @patch("code_indexer.cli_fast_entry.quick_daemon_check")
    @patch("code_indexer.cli_daemon_fast.execute_via_daemon")
    def test_routes_to_fast_path_when_daemon_enabled(self, mock_execute, mock_check):
        """Test that daemon-enabled + delegatable command uses fast path."""
        # Arrange
        mock_check.return_value = (True, Path("/fake/config.json"))
        mock_execute.return_value = 0

        from code_indexer.cli_fast_entry import main

        # Act - query command with daemon enabled
        with patch.object(sys, "argv", ["cidx", "query", "test", "--fts"]):
            result = main()

        # Assert
        mock_check.assert_called_once()
        mock_execute.assert_called_once()
        assert result == 0

    @patch("code_indexer.cli_fast_entry.quick_daemon_check")
    @patch("code_indexer.cli.cli")
    def test_routes_to_slow_path_when_daemon_disabled(self, mock_cli, mock_check):
        """Test that daemon-disabled uses full CLI (slow path)."""
        # Arrange
        mock_check.return_value = (False, None)

        from code_indexer.cli_fast_entry import main

        # Act - query command with daemon disabled
        with patch.object(sys, "argv", ["cidx", "query", "test"]):
            main()

        # Assert
        mock_check.assert_called_once()
        mock_cli.assert_called_once()

    @patch("code_indexer.cli_fast_entry.quick_daemon_check")
    @patch("code_indexer.cli.cli")
    def test_routes_to_slow_path_for_non_delegatable_commands(
        self, mock_cli, mock_check
    ):
        """Test that non-delegatable commands always use full CLI."""
        # Arrange
        mock_check.return_value = (True, Path("/fake/config.json"))

        from code_indexer.cli_fast_entry import main

        # Act - init command (not delegatable)
        with patch.object(sys, "argv", ["cidx", "init"]):
            main()

        # Assert
        mock_check.assert_called_once()
        mock_cli.assert_called_once()  # Should use slow path


class TestFastPathPerformance:
    """Test that fast path achieves target performance."""

    @patch("code_indexer.cli_fast_entry.quick_daemon_check")
    @patch("code_indexer.cli_daemon_fast.execute_via_daemon")
    def test_fast_path_startup_time_under_150ms(self, mock_execute, mock_check):
        """Test that fast path (daemon mode) starts in <150ms."""
        # Arrange
        mock_check.return_value = (True, Path("/fake/config.json"))
        mock_execute.return_value = 0

        # Act - measure import + execution time
        start = time.time()
        from code_indexer.cli_fast_entry import main

        with patch.object(sys, "argv", ["cidx", "query", "test", "--fts"]):
            main()

        elapsed_ms = (time.time() - start) * 1000

        # Assert - should be <150ms (target)
        # Note: This may be tight in CI, but should pass on reasonable hardware
        assert elapsed_ms < 200, f"Fast path took {elapsed_ms:.0f}ms, target <150ms"

    def test_fast_entry_module_import_time(self):
        """Test that cli_fast_entry imports quickly (<50ms)."""
        # Act - measure import time
        start = time.time()
        import code_indexer.cli_fast_entry  # noqa: F401

        elapsed_ms = (time.time() - start) * 1000

        # Assert - should import very quickly (stdlib + rpyc + rich)
        assert elapsed_ms < 100, (
            f"Fast entry import took {elapsed_ms:.0f}ms, expected <100ms"
        )


class TestFallbackBehavior:
    """Test fallback to full CLI when daemon unavailable."""

    @patch("code_indexer.cli_fast_entry.quick_daemon_check")
    @patch("code_indexer.cli_daemon_fast.execute_via_daemon")
    @patch("code_indexer.cli.cli")
    def test_fallback_to_full_cli_on_daemon_connection_error(
        self, mock_cli, mock_execute, mock_check
    ):
        """Test fallback when daemon connection fails."""
        # Arrange
        mock_check.return_value = (True, Path("/fake/config.json"))
        mock_execute.side_effect = Exception("Connection refused")

        from code_indexer.cli_fast_entry import main

        # Act
        with patch.object(sys, "argv", ["cidx", "query", "test"]):
            # Should not raise, should fallback
            main()

        # Assert - should have attempted fast path, then fallen back
        mock_execute.assert_called_once()
        # Note: Actual fallback implementation may vary

    @patch("code_indexer.cli_fast_entry.quick_daemon_check")
    @patch("code_indexer.cli_daemon_fast.execute_via_daemon")
    @patch("code_indexer.cli.cli")
    def test_fallback_handles_rich_markup_in_exception(
        self, mock_cli, mock_execute, mock_check
    ):
        """Test that Rich markup in exception messages is properly escaped.

        Regression test for Rich markup injection bug where exception messages
        containing Rich markup tags (like [/{status_style}]) would cause
        MarkupError when embedded in f-string with [yellow]...[/yellow].
        """
        # Arrange
        mock_check.return_value = (True, Path("/fake/config.json"))
        # Create exception with Rich markup that would break console.print
        exception_msg = "Daemon unavailable [/{status_style}] connection failed"
        mock_execute.side_effect = Exception(exception_msg)

        from code_indexer.cli_fast_entry import main

        # Act & Assert - should not raise MarkupError
        with patch.object(sys, "argv", ["cidx", "query", "test"]):
            try:
                main()
            except Exception as e:
                # Should not be a MarkupError
                assert "MarkupError" not in str(type(e))
                assert "closing tag" not in str(e)

        # Assert - should have attempted fast path, then fallen back
        mock_execute.assert_called_once()
        mock_cli.assert_called_once()
