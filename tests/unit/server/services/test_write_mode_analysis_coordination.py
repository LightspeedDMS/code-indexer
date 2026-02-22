"""
Unit tests for Bug #245: Write mode / DependencyMapService analysis lock coordination.

Verifies that _setup_analysis() returns early (skips analysis) when the write-mode
marker file for cidx-meta exists, preventing data-loss from concurrent analysis
overwriting files being edited via MCP CRUD operations.

Marker file path: golden_repos_dir/.write_mode/cidx-meta.json
(same pattern as file_crud_service.py line 159)
"""
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.dependency_map_service import DependencyMapService


def _make_service(golden_repos_dir: str):
    """
    Create a DependencyMapService with mocked dependencies pointing at tmp dir.

    golden_repos_dir must be a string path (matching golden_repos_manager.golden_repos_dir).
    """
    golden_repos_manager = MagicMock()
    golden_repos_manager.golden_repos_dir = golden_repos_dir

    config_manager = MagicMock()
    # dependency_map_enabled=True so the first early-return check does NOT fire
    config = MagicMock()
    config.dependency_map_enabled = True
    config_manager.get_claude_integration_config.return_value = config

    tracking_backend = MagicMock()
    analyzer = MagicMock()

    service = DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=config_manager,
        tracking_backend=tracking_backend,
        analyzer=analyzer,
    )
    return service, config_manager, golden_repos_manager


class TestWriteModeAnalysisCoordination:
    """Tests for Bug #245 write-mode / analysis coordination in _setup_analysis()."""

    # ------------------------------------------------------------------
    # Test 1: Analysis skipped when write-mode marker exists
    # ------------------------------------------------------------------
    def test_analysis_skipped_when_write_mode_active(self, tmp_path: Path):
        """
        When write-mode marker file golden_repos_dir/.write_mode/cidx-meta.json exists,
        _setup_analysis() must return early_return=True with status='skipped'.
        """
        golden_repos_dir = str(tmp_path / "golden-repos")
        Path(golden_repos_dir).mkdir(parents=True)

        # Create the write-mode marker file
        write_mode_dir = Path(golden_repos_dir) / ".write_mode"
        write_mode_dir.mkdir()
        marker_file = write_mode_dir / "cidx-meta.json"
        marker_file.write_text('{"active": true}')

        service, _, _ = _make_service(golden_repos_dir)

        result = service._setup_analysis()

        assert result["early_return"] is True
        assert result["status"] == "skipped"

    # ------------------------------------------------------------------
    # Test 2: Analysis proceeds when write-mode marker does NOT exist
    # ------------------------------------------------------------------
    def test_analysis_proceeds_when_write_mode_not_active(self, tmp_path: Path):
        """
        When no write-mode marker file exists, _setup_analysis() must NOT return
        early_return=True due to write-mode (it may return early for other reasons
        such as no activated repos, but the status must not be 'skipped' due to write mode).
        """
        golden_repos_dir = str(tmp_path / "golden-repos")
        Path(golden_repos_dir).mkdir(parents=True)
        # No .write_mode directory or marker file created

        service, _, golden_repos_manager = _make_service(golden_repos_dir)

        # _get_activated_repos() is called after the write-mode check.
        # Return an empty list so _setup_analysis() returns early for "no repos" reason,
        # not write-mode. This lets us confirm write-mode early-return did NOT fire.
        service._get_activated_repos = MagicMock(return_value=[])

        result = service._setup_analysis()

        # Must not have returned with "skipped" (write-mode reason)
        # It may return early with "no repos" status, which is fine.
        assert not (result["early_return"] is True and result.get("status") == "skipped" and
                    "Write mode" in result.get("message", ""))

    # ------------------------------------------------------------------
    # Test 3: Log message emitted when skipping due to write mode
    # ------------------------------------------------------------------
    def test_log_message_emitted_when_write_mode_active(self, tmp_path: Path, caplog):
        """
        When write-mode causes early return, an INFO log mentioning 'Write mode' must
        be emitted so operators can diagnose skipped analyses.
        """
        golden_repos_dir = str(tmp_path / "golden-repos")
        Path(golden_repos_dir).mkdir(parents=True)

        write_mode_dir = Path(golden_repos_dir) / ".write_mode"
        write_mode_dir.mkdir()
        marker_file = write_mode_dir / "cidx-meta.json"
        marker_file.write_text('{"active": true}')

        service, _, _ = _make_service(golden_repos_dir)

        with caplog.at_level(logging.INFO, logger="code_indexer.server.services.dependency_map_service"):
            service._setup_analysis()

        assert any(
            "Write mode" in record.message
            for record in caplog.records
        ), f"Expected 'Write mode' in log records, got: {[r.message for r in caplog.records]}"

    # ------------------------------------------------------------------
    # Test 4: Marker file path matches file_crud_service.py pattern
    # ------------------------------------------------------------------
    def test_marker_file_path_matches_file_crud_service_pattern(self, tmp_path: Path):
        """
        The marker file checked must be exactly:
            golden_repos_dir / '.write_mode' / 'cidx-meta.json'

        This matches file_crud_service.py line 159 pattern where alias_without_global
        for 'cidx-meta-global' is 'cidx-meta'.

        Verify: placing the file at a WRONG path does NOT trigger early return.
        """
        golden_repos_dir = str(tmp_path / "golden-repos")
        Path(golden_repos_dir).mkdir(parents=True)

        # Place marker at wrong path (e.g. without stripping -global)
        wrong_dir = Path(golden_repos_dir) / ".write_mode"
        wrong_dir.mkdir()
        wrong_marker = wrong_dir / "cidx-meta-global.json"  # wrong filename
        wrong_marker.write_text('{"active": true}')

        service, _, _ = _make_service(golden_repos_dir)
        service._get_activated_repos = MagicMock(return_value=[])

        result = service._setup_analysis()

        # With wrong path the write-mode check should NOT have fired
        assert not (result.get("status") == "skipped" and
                    "Write mode" in result.get("message", ""))

    # ------------------------------------------------------------------
    # Test 5: message field populated when skipping due to write mode
    # ------------------------------------------------------------------
    def test_skipped_result_contains_message(self, tmp_path: Path):
        """
        When write-mode causes early return, the result dict must contain
        a non-empty 'message' field describing why analysis was skipped.
        """
        golden_repos_dir = str(tmp_path / "golden-repos")
        Path(golden_repos_dir).mkdir(parents=True)

        write_mode_dir = Path(golden_repos_dir) / ".write_mode"
        write_mode_dir.mkdir()
        (write_mode_dir / "cidx-meta.json").write_text('{"active": true}')

        service, _, _ = _make_service(golden_repos_dir)

        result = service._setup_analysis()

        assert "message" in result
        assert len(result["message"]) > 0


class TestDeltaAnalysisWriteModeCoordination:
    """Tests that run_delta_analysis() also respects write mode markers (Bug #245)."""

    def test_delta_analysis_skipped_when_write_mode_active(self, tmp_path: Path):
        """
        run_delta_analysis() must return status='skipped' when the write-mode
        marker file golden_repos_dir/.write_mode/cidx-meta.json exists.

        This covers the 60-second scheduler path which is the most dangerous
        because it runs automatically without user interaction.
        """
        golden_repos_dir = str(tmp_path / "golden-repos")
        Path(golden_repos_dir).mkdir(parents=True)

        # Create write mode marker
        write_mode_dir = Path(golden_repos_dir) / ".write_mode"
        write_mode_dir.mkdir()
        (write_mode_dir / "cidx-meta.json").write_text("{}")

        service, config_manager, _ = _make_service(golden_repos_dir)
        # dependency_map_enabled=True so the first enabled check does NOT fire
        config = MagicMock()
        config.dependency_map_enabled = True
        config_manager.get_claude_integration_config.return_value = config

        result = service.run_delta_analysis()

        assert result is not None
        assert result["status"] == "skipped"
        assert "Write mode" in result["message"]

    def test_delta_analysis_proceeds_when_write_mode_not_active(self, tmp_path: Path):
        """
        run_delta_analysis() must NOT skip due to write mode when the marker
        file does not exist. It may still return early for other reasons
        (e.g. no changes), but the status must not be 'skipped' with a
        'Write mode' message.
        """
        golden_repos_dir = str(tmp_path / "golden-repos")
        Path(golden_repos_dir).mkdir(parents=True)
        # No .write_mode directory or marker file

        service, config_manager, _ = _make_service(golden_repos_dir)
        config = MagicMock()
        config.dependency_map_enabled = True
        config.dependency_map_interval_hours = 1
        config_manager.get_claude_integration_config.return_value = config

        # Stub detect_changes so it returns no changes (avoids filesystem/git work)
        service.detect_changes = MagicMock(return_value=([], [], []))
        service._tracking_backend.update_tracking = MagicMock()

        result = service.run_delta_analysis()

        # Must not have been skipped due to write mode
        assert not (
            result is not None
            and result.get("status") == "skipped"
            and "Write mode" in result.get("message", "")
        )
