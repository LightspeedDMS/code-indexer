"""
Unit tests for Story #929 Item #17 — prompt template cleanup.

Tests verify that the SERVICE RESTART section has been removed from
research_assistant_prompt.md. The dead section (lines 66-68) described
systemctl restart authority that has been revoked: restart is now delegated
to the auto-updater, not the Research Assistant.
"""

from pathlib import Path

import pytest

# Resolve the prompt template path relative to this file.
_PROMPT_PATH = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src"
    / "code_indexer"
    / "server"
    / "config"
    / "research_assistant_prompt.md"
)


@pytest.fixture
def prompt_content() -> str:
    """Read the authoritative prompt template from disk."""
    assert _PROMPT_PATH.exists(), (
        f"Prompt template not found at {_PROMPT_PATH}. "
        "Ensure the path is correct relative to this test file."
    )
    return _PROMPT_PATH.read_text()


class TestServiceRestartSectionRemoved:
    """Item #17: SERVICE RESTART section must be removed from the prompt template."""

    def test_service_restart_section_header_absent(self, prompt_content):
        """The '### SERVICE RESTART' markdown header must not exist."""
        assert "### SERVICE RESTART" not in prompt_content, (
            "The '### SERVICE RESTART' section header must be removed from "
            "research_assistant_prompt.md (Item #17). "
            "Restart authority is delegated to the auto-updater."
        )

    def test_systemctl_restart_cidx_server_absent(self, prompt_content):
        """No reference to 'systemctl restart cidx-server' must remain in the template."""
        assert "systemctl restart cidx-server" not in prompt_content, (
            "'systemctl restart cidx-server' must be removed from "
            "research_assistant_prompt.md (Item #17). "
            "The RA no longer has restart authority."
        )
