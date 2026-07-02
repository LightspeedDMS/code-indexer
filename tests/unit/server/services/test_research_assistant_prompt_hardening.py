"""
Unit tests for Story #554: Research Assistant Security Hardening - Prompt Changes.

Tests verify that research_assistant_prompt.md is updated to:
- Remove dangerous capabilities (sudo, service restart, package install)
- Remove file-based report writing instructions
- Add OUTPUT RULES section (inline reporting)
- Add OPERATIONAL BOUNDARIES section (non-disclosing response policy)
- NOT enumerate specific blocked tools/commands (avoid giving attackers a roadmap)

Acceptance Criteria covered:
- AC6: Inline reporting - no file-based reports
- AC7: Prompt removes dangerous capabilities without disclosing security details
- AC8: Template variables preserved

Following TDD methodology: Tests written FIRST before implementing.
"""

import re
import pytest
from pathlib import Path

# Named constant for how far to scan within a section
SECTION_LOOKAHEAD_CHARS = 2000

# Resolve prompt path relative to this test file's location.
# Path: tests/unit/server/services/ -> src/code_indexer/server/config/
_TESTS_DIR = Path(__file__).parent
_PROJECT_ROOT = _TESTS_DIR.parents[
    3
]  # up 4 levels: services -> server -> unit -> tests -> root
_PROMPT_PATH = (
    _PROJECT_ROOT
    / "src"
    / "code_indexer"
    / "server"
    / "config"
    / "research_assistant_prompt.md"
)


@pytest.fixture(scope="module")
def prompt_content():
    """Load raw prompt template content once for all tests in this module."""
    assert _PROMPT_PATH.exists(), (
        f"Prompt file must exist at {_PROMPT_PATH}. "
        "Ensure the path resolution from __file__ is correct."
    )
    return _PROMPT_PATH.read_text()


class TestPromptRemovesDangerousCapabilities:
    """Tests for AC7: Removed capabilities - no sudo/service restart/package install."""

    def test_prompt_does_not_have_sudo_usage_guidelines(self, prompt_content):
        """AC7: SUDO USAGE GUIDELINES section must be removed."""
        assert "SUDO USAGE GUIDELINES" not in prompt_content, (
            "Prompt must not contain SUDO USAGE GUIDELINES section. "
            "This was removed as part of security hardening."
        )

    def test_prompt_does_not_allow_sudo_systemctl_restart(self, prompt_content):
        """AC7: sudo systemctl restart/start/stop authorization must be removed."""
        forbidden_pattern = r"sudo systemctl (restart|start|stop)"
        matches = re.findall(forbidden_pattern, prompt_content)
        assert len(matches) == 0, (
            f"Prompt must not authorize sudo systemctl restart/start/stop. "
            f"Found {len(matches)} occurrences. "
            "Diagnostic 'systemctl status' is fine, but not restart/start/stop."
        )

    def test_prompt_does_not_have_allowed_remediation_operations(self, prompt_content):
        """AC7: ALLOWED REMEDIATION OPERATIONS section must be removed."""
        assert "ALLOWED REMEDIATION OPERATIONS" not in prompt_content, (
            "Prompt must not contain ALLOWED REMEDIATION OPERATIONS section. "
            "This section authorized package install, service restart, etc."
        )

    def test_prompt_does_not_allow_sudo_package_install(self, prompt_content):
        """AC7: sudo apt/pip/dnf install authorization must be removed."""
        forbidden_pattern = r"sudo (apt|pip|dnf) install"
        matches = re.findall(forbidden_pattern, prompt_content)
        assert len(matches) == 0, (
            f"Prompt must not authorize sudo package install commands. "
            f"Found {len(matches)} occurrences."
        )

    def test_prompt_has_operational_boundaries_section(self, prompt_content):
        """AC7: Prompt must have OPERATIONAL BOUNDARIES section (non-disclosing)."""
        assert "OPERATIONAL BOUNDARIES" in prompt_content, (
            "Prompt must contain OPERATIONAL BOUNDARIES section that instructs "
            "the assistant how to respond when actions are blocked, without "
            "disclosing security details."
        )

    def test_operational_boundaries_instructs_no_disclosure(self, prompt_content):
        """AC7: OPERATIONAL BOUNDARIES must instruct NOT to disclose restrictions."""
        section_start = prompt_content.find("OPERATIONAL BOUNDARIES")
        assert section_start >= 0
        section_text = prompt_content[
            section_start : section_start + SECTION_LOOKAHEAD_CHARS
        ]
        has_no_disclose = any(
            term in section_text
            for term in ("DO NOT explain WHY", "DO NOT disclose", "confidential")
        )
        assert has_no_disclose, (
            "OPERATIONAL BOUNDARIES section must instruct assistant NOT to "
            "disclose why actions are blocked or what restrictions exist."
        )

    def test_operational_boundaries_offers_alternatives(self, prompt_content):
        """AC7: OPERATIONAL BOUNDARIES must instruct offering alternatives."""
        section_start = prompt_content.find("OPERATIONAL BOUNDARIES")
        assert section_start >= 0
        section_text = prompt_content[
            section_start : section_start + SECTION_LOOKAHEAD_CHARS
        ]
        has_alternatives = any(
            term in section_text.lower()
            for term in ("alternative", "instead", "can do")
        )
        assert has_alternatives, (
            "OPERATIONAL BOUNDARIES section must instruct offering alternatives "
            "when actions cannot be performed."
        )

    def test_prompt_does_not_enumerate_blocked_tools(self, prompt_content):
        """AC7: Prompt must NOT list specific blocked tool names (security roadmap)."""
        # These tool names should NOT appear in a 'blocked' or 'cannot' context
        # that enumerates what is restricted. They may appear in diagnostic
        # commands (e.g., 'systemctl status') but not in restriction lists.
        assert "REMOVED CAPABILITIES" not in prompt_content, (
            "Prompt must NOT contain a REMOVED CAPABILITIES section that "
            "enumerates blocked tools. This gives attackers a roadmap. "
            "Use OPERATIONAL BOUNDARIES with non-disclosing language instead."
        )


class TestPromptInlineReporting:
    """Tests for AC6: Inline reporting - no file-based reports."""

    def test_prompt_has_output_rules_section(self, prompt_content):
        """AC6: Prompt must contain OUTPUT RULES section."""
        assert "OUTPUT RULES" in prompt_content, (
            "Prompt must contain OUTPUT RULES section instructing "
            "the assistant to deliver findings inline in chat responses."
        )

    def test_prompt_instructs_no_file_based_reports(self, prompt_content):
        """AC6: Prompt must explicitly prohibit writing reports to files."""
        no_file_phrases = [
            r"never write reports to files",
            r"do not write.*report.*file",
            r"not write.*report",
        ]
        found = any(
            re.search(phrase, prompt_content, re.IGNORECASE)
            for phrase in no_file_phrases
        )
        assert found, (
            "Prompt must instruct assistant NOT to write reports to files. "
            "Users only see chat responses in the Web UI."
        )

    def test_prompt_instructs_final_response_inline(self, prompt_content):
        """AC6: Prompt must instruct that the FINAL response contains complete analysis."""
        output_section_start = prompt_content.find("OUTPUT RULES")
        assert output_section_start >= 0
        section_text = prompt_content[
            output_section_start : output_section_start + SECTION_LOOKAHEAD_CHARS
        ]
        has_final = any(
            term in section_text
            for term in ("FINAL", "final", "final message", "complete analysis")
        )
        assert has_final, (
            "OUTPUT RULES section must instruct that the final response "
            "contains the complete analysis inline."
        )

    def test_old_write_reports_to_session_folder_removed(self, prompt_content):
        """AC6: Old 'Write analysis reports to the session folder' must be removed."""
        assert "Write analysis reports to the session folder" not in prompt_content, (
            "Old instruction to write reports to session folder must be removed. "
            "Reports are delivered inline in chat responses now."
        )

    def test_output_rules_mentions_markdown_structure(self, prompt_content):
        """
        AC6: OUTPUT RULES section should mention structuring responses with
        markdown headers and code blocks for evidence.
        """
        output_section_start = prompt_content.find("OUTPUT RULES")
        assert output_section_start >= 0
        section_text = prompt_content[
            output_section_start : output_section_start + SECTION_LOOKAHEAD_CHARS
        ]
        has_structure = any(
            term in section_text.lower()
            for term in ("markdown", "header", "code block", "structure")
        )
        assert has_structure, (
            "OUTPUT RULES section should mention structuring responses with "
            "markdown headers/code blocks for evidence."
        )


class TestPromptTemplateVariablesPreserved:
    """Tests for AC8: Template variables must remain intact for runtime substitution."""

    def test_hostname_variable_preserved(self, prompt_content):
        """AC8: {hostname} template variable must be preserved."""
        assert "{hostname}" in prompt_content, (
            "{hostname} template variable must remain in prompt for runtime substitution"
        )

    def test_server_version_variable_preserved(self, prompt_content):
        """AC8: {server_version} template variable must be preserved."""
        assert "{server_version}" in prompt_content, (
            "{server_version} template variable must remain in prompt"
        )

    def test_server_data_dir_variable_preserved(self, prompt_content):
        """AC8: {server_data_dir} template variable must be preserved."""
        assert "{server_data_dir}" in prompt_content, (
            "{server_data_dir} template variable must remain in prompt"
        )

    def test_golden_repos_dir_variable_preserved(self, prompt_content):
        """AC8: {golden_repos_dir} template variable must be preserved."""
        assert "{golden_repos_dir}" in prompt_content, (
            "{golden_repos_dir} template variable must remain in prompt"
        )

    def test_db_path_variable_preserved(self, prompt_content):
        """AC8: {db_path} template variable must be preserved."""
        assert "{db_path}" in prompt_content, (
            "{db_path} template variable must remain in prompt"
        )
