"""
Unit tests for Research Assistant GitHub integration (Story #202).

Tests GitHub token handling, subprocess environment injection,
and issue_manager.py symlink creation.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.research_assistant_service import (
    ResearchAssistantService,
)


class TestGitHubTokenInit:
    """Test __init__ accepts and stores github_token parameter (AC3)."""

    def test_init_accepts_github_token(self, tmp_path: Path) -> None:
        """Test __init__ accepts github_token parameter and stores it."""
        db_path = str(tmp_path / "test.db")
        service = ResearchAssistantService(db_path=db_path, github_token="test_token_123")

        assert hasattr(service, "_github_token")
        assert service._github_token == "test_token_123"

    def test_init_github_token_defaults_none(self, tmp_path: Path) -> None:
        """Test __init__ sets _github_token to None when not provided."""
        db_path = str(tmp_path / "test.db")
        service = ResearchAssistantService(db_path=db_path)

        assert hasattr(service, "_github_token")
        assert service._github_token is None


class TestSubprocessEnvironment:
    """Test subprocess receives GitHub token in environment (AC3)."""

    @patch("subprocess.run")
    @patch("code_indexer.server.services.research_assistant_service.ResearchAssistantService._get_or_create_claude_session_id")
    def test_run_claude_env_includes_github_token(
        self, mock_get_session_id: MagicMock, mock_subprocess: MagicMock, tmp_path: Path
    ) -> None:
        """Test subprocess.run receives GITHUB_TOKEN and GH_TOKEN in env when token is set."""
        # Setup
        db_path = str(tmp_path / "test.db")
        service = ResearchAssistantService(db_path=db_path, github_token="github_token_xyz")

        # Create a test session
        session_folder = tmp_path / "session_folder"
        session_folder.mkdir()
        service._ensure_session_folder_setup(str(session_folder))

        # Mock database operations
        with patch.object(service, "get_session") as mock_get_session:
            mock_get_session.return_value = {
                "id": "test_session",
                "folder_path": str(session_folder),
            }
            mock_get_session_id.return_value = "claude_session_123"

            # Mock successful subprocess execution
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "Claude response"
            mock_result.stderr = ""
            mock_subprocess.return_value = mock_result

            # Execute
            service._run_claude_background(
                job_id="job1",
                session_id="test_session",
                claude_prompt="Test prompt",
                is_first_prompt=True,
            )

            # Verify subprocess.run was called with env containing both tokens
            assert mock_subprocess.called
            call_kwargs = mock_subprocess.call_args[1]
            assert "env" in call_kwargs

            env = call_kwargs["env"]
            assert "GITHUB_TOKEN" in env
            assert "GH_TOKEN" in env
            assert env["GITHUB_TOKEN"] == "github_token_xyz"
            assert env["GH_TOKEN"] == "github_token_xyz"

    @patch.dict("os.environ", {}, clear=True)
    @patch("subprocess.run")
    @patch("code_indexer.server.services.research_assistant_service.ResearchAssistantService._get_or_create_claude_session_id")
    def test_run_claude_env_omits_token_when_none(
        self, mock_get_session_id: MagicMock, mock_subprocess: MagicMock, tmp_path: Path
    ) -> None:
        """Test subprocess.run does not add GITHUB_TOKEN or GH_TOKEN when token is None."""
        # Setup
        db_path = str(tmp_path / "test.db")
        service = ResearchAssistantService(db_path=db_path, github_token=None)

        # Create a test session
        session_folder = tmp_path / "session_folder"
        session_folder.mkdir()
        service._ensure_session_folder_setup(str(session_folder))

        # Mock database operations
        with patch.object(service, "get_session") as mock_get_session:
            mock_get_session.return_value = {
                "id": "test_session",
                "folder_path": str(session_folder),
            }
            mock_get_session_id.return_value = "claude_session_123"

            # Mock successful subprocess execution
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "Claude response"
            mock_result.stderr = ""
            mock_subprocess.return_value = mock_result

            # Execute
            service._run_claude_background(
                job_id="job1",
                session_id="test_session",
                claude_prompt="Test prompt",
                is_first_prompt=True,
            )

            # Verify subprocess.run was called
            assert mock_subprocess.called
            call_kwargs = mock_subprocess.call_args[1]

            # Env should be empty (we mocked os.environ to empty)
            # Our code should NOT add GITHUB_TOKEN/GH_TOKEN when _github_token is None
            env = call_kwargs.get("env", {})
            assert "GITHUB_TOKEN" not in env, "GITHUB_TOKEN should not be added when token is None"
            assert "GH_TOKEN" not in env, "GH_TOKEN should not be added when token is None"


class TestSessionFolderSymlinks:
    """Test session folder creates issue_manager.py symlink (AC4, AC6)."""

    def test_session_folder_creates_issue_manager_symlink(self, tmp_path: Path) -> None:
        """Test _ensure_session_folder_setup creates issue_manager.py symlink."""
        # Setup
        db_path = str(tmp_path / "test.db")
        service = ResearchAssistantService(db_path=db_path)

        # Create fake issue_manager.py source
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        issue_manager_source = fake_home / ".claude" / "scripts" / "utils" / "issue_manager.py"
        issue_manager_source.parent.mkdir(parents=True)
        issue_manager_source.write_text("# Fake issue_manager.py")

        # Create session folder
        session_folder = tmp_path / "session_folder"

        # Mock Path.home() to return our fake home
        with patch("pathlib.Path.home", return_value=fake_home):
            service._ensure_session_folder_setup(str(session_folder))

            # Verify symlink was created
            issue_manager_link = session_folder / "issue_manager.py"
            assert issue_manager_link.exists()
            assert issue_manager_link.is_symlink()
            assert issue_manager_link.resolve() == issue_manager_source.resolve()

    def test_session_folder_handles_missing_issue_manager(
        self, tmp_path: Path, caplog
    ) -> None:
        """Test _ensure_session_folder_setup handles missing issue_manager.py gracefully (AC6)."""
        # Setup
        db_path = str(tmp_path / "test.db")
        service = ResearchAssistantService(db_path=db_path)

        # Create fake home WITHOUT issue_manager.py
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()

        # Create session folder
        session_folder = tmp_path / "session_folder"

        # Mock Path.home() to return our fake home
        with patch("pathlib.Path.home", return_value=fake_home):
            # Should not raise exception
            service._ensure_session_folder_setup(str(session_folder))

            # Verify symlink was NOT created
            issue_manager_link = session_folder / "issue_manager.py"
            assert not issue_manager_link.exists()

            # Verify warning was logged
            assert any("issue_manager.py not found" in record.message for record in caplog.records)

    def test_session_folder_skips_existing_symlink(self, tmp_path: Path) -> None:
        """Test _ensure_session_folder_setup does not recreate existing symlink."""
        # Setup
        db_path = str(tmp_path / "test.db")
        service = ResearchAssistantService(db_path=db_path)

        # Create fake issue_manager.py source
        fake_home = tmp_path / "fake_home"
        fake_home.mkdir()
        issue_manager_source = fake_home / ".claude" / "scripts" / "utils" / "issue_manager.py"
        issue_manager_source.parent.mkdir(parents=True)
        issue_manager_source.write_text("# Fake issue_manager.py")

        # Create session folder with existing symlink
        session_folder = tmp_path / "session_folder"
        session_folder.mkdir()
        issue_manager_link = session_folder / "issue_manager.py"
        issue_manager_link.symlink_to(issue_manager_source)

        # Record the original symlink target
        original_target = issue_manager_link.resolve()

        # Mock Path.home() to return our fake home
        with patch("pathlib.Path.home", return_value=fake_home):
            service._ensure_session_folder_setup(str(session_folder))

            # Verify symlink still exists and points to same target
            assert issue_manager_link.exists()
            assert issue_manager_link.is_symlink()
            assert issue_manager_link.resolve() == original_target


class TestPromptTemplate:
    """Test prompt template contains GitHub bug report instructions (AC2)."""

    def test_prompt_contains_github_issue_section(self) -> None:
        """Test prompt template contains GITHUB BUG REPORT CREATION section."""
        prompt_path = Path(__file__).parent.parent.parent.parent.parent / "src" / "code_indexer" / "server" / "config" / "research_assistant_prompt.md"

        assert prompt_path.exists(), f"Prompt template not found at {prompt_path}"

        content = prompt_path.read_text()
        assert "GITHUB BUG REPORT CREATION" in content or "GitHub Bug Report" in content

    def test_prompt_contains_bug_report_format(self) -> None:
        """Test prompt template specifies required bug report sections (AC2)."""
        prompt_path = Path(__file__).parent.parent.parent.parent.parent / "src" / "code_indexer" / "server" / "config" / "research_assistant_prompt.md"

        content = prompt_path.read_text()

        # Check for required sections
        required_sections = [
            "Bug Description",
            "Steps to Reproduce",
            "Expected Behavior",
            "Actual Behavior",
            "Error Messages",
            "Root Cause",
            "Affected Files",
        ]

        for section in required_sections:
            assert section in content, f"Missing required section: {section}"
