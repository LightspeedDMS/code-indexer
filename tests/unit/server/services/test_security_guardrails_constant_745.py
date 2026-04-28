"""
Unit tests for Story #929 Item #5 — SECURITY_GUARDRAILS fallback handling.

Option B chosen: FAIL CLOSED.

Rationale for Option B over Option A:
  - Option A (sync constant) keeps a large duplicated blob in source — any future
    template change must be mirrored in two places, so drift is virtually guaranteed.
  - Option B (fail closed) is simpler: if the template cannot be loaded, raise
    immediately so the operator sees a loud startup error rather than running with
    a silently stale, weaker prompt.
  - The Research Assistant is double-MFA gated. A startup failure is preferable to
    silent capability regression.
  - The fail-closed path already exists structurally; this test verifies it raises
    rather than returning the stale SECURITY_GUARDRAILS constant.

This choice is documented here per story requirement (Item #5).
"""

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def service(tmp_path):
    """Real ResearchAssistantService backed by a temporary SQLite DB."""
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.services.research_assistant_service import (
        ResearchAssistantService,
    )

    db_path = str(tmp_path / "data" / "cidx_server.db")
    (tmp_path / "data").mkdir(parents=True)
    DatabaseSchema(db_path=db_path).initialize_database()
    return ResearchAssistantService(db_path=db_path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSecurityGuardrailsFailClosed:
    """
    Item #5 Option B: load_research_prompt must raise when template load fails,
    NOT silently return the stale SECURITY_GUARDRAILS constant.
    """

    def test_raises_when_template_file_missing(self, service, tmp_path, monkeypatch):
        """
        When CIDX_REPO_ROOT points to a directory with no prompt template,
        load_research_prompt must raise rather than returning the stale constant.

        The real external boundary used here is the CIDX_REPO_ROOT environment
        variable: setting it to a path that lacks the expected config file causes
        the real _get_config_dir() to resolve a nonexistent prompt path.
        """
        # Point CIDX_REPO_ROOT at tmp_path — no config/research_assistant_prompt.md
        # exists there, so the real code will see a missing file.
        monkeypatch.setenv("CIDX_REPO_ROOT", str(tmp_path))

        with pytest.raises(Exception):
            service.load_research_prompt()

    def test_raises_when_template_file_unreadable(self, service, tmp_path, monkeypatch):
        """
        When the prompt template file exists but open() raises PermissionError
        (OS-level read failure), load_research_prompt must raise (fail closed).

        builtins.open is an OS boundary, not an SUT method.
        """
        from unittest.mock import patch

        # Create the expected directory structure so the file-existence check passes
        config_dir = tmp_path / "src" / "code_indexer" / "server" / "config"
        config_dir.mkdir(parents=True)
        prompt_file = config_dir / "research_assistant_prompt.md"
        prompt_file.write_text("template content")

        monkeypatch.setenv("CIDX_REPO_ROOT", str(tmp_path))

        with patch(
            "pathlib.Path.read_text", side_effect=PermissionError("no read access")
        ):
            with pytest.raises(Exception):
                service.load_research_prompt()
