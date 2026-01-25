"""
TDD tests for Story #29 AC2: Web UI Config Sections Have Explanatory Paragraphs.

Tests written FIRST before implementation (TDD methodology).

Each config section in the Web UI should have a descriptive paragraph at the top
explaining what capabilities the settings control.
"""

import re
import pytest
from pathlib import Path


# Constants
MIN_DESCRIPTION_LENGTH = 50

# All config sections that should have documentation
# IDs match actual HTML structure in config_section.html
CONFIG_SECTIONS = [
    ("section-server", "Server Settings"),
    ("section-cache", "Cache Settings"),
    ("section-reindexing", "Reindexing Settings"),
    ("section-timeouts", "Timeout Settings"),
    ("section-password_security", "Password Security"),
    ("section-oidc", "OIDC/SSO"),
    ("section-job_queue", "Job Queue"),
    ("section-search_limits", "Search Limits"),
    ("section-file_content_limits", "File Content Limits"),
    ("section-golden_repos", "Golden Repos"),
    ("section-api-keys", "API Keys"),
    ("section-telemetry", "Telemetry"),
    ("section-claude_delegation", "Claude CLI Delegation"),
    ("section-mcp_session", "MCP Session"),
    ("section-health", "Health Monitoring"),
    ("section-scip", "SCIP Configuration"),
    ("section-git_timeouts", "Git Timeouts"),
    ("section-error_handling", "Error Handling"),
    ("section-api_limits", "API Limits"),
    ("section-web_security", "Web Security"),
    ("section-auth", "Auth"),
    ("section-provider-api-keys", "Provider API Keys"),
    ("section-multi-search", "Multi-Search Settings"),
    ("section-background-jobs", "Background Jobs"),
]


class TestAC2_WebUIConfigSectionDocumentation:
    """
    AC2: Web UI Config Sections Have Explanatory Paragraphs.

    Each configuration section should have documentation explaining:
    - What the section controls
    - What capabilities the settings affect
    - Brief guidance on when to change them
    """

    @staticmethod
    def get_config_section_path() -> Path:
        """Get the path to config_section.html using relative path from test file."""
        return (
            Path(__file__).parent.parent.parent.parent.parent
            / "src/code_indexer/server/web/templates/partials/config_section.html"
        )

    def get_config_section_content(self) -> str:
        """Load the config_section.html template content."""
        config_section_path = self.get_config_section_path()
        assert config_section_path.exists(), \
            f"Config section template not found at {config_section_path}"
        return config_section_path.read_text()

    @pytest.mark.parametrize("section_id,section_name", CONFIG_SECTIONS)
    def test_section_has_documentation(self, section_id: str, section_name: str):
        """Each config section should have an explanatory documentation paragraph."""
        content = self.get_config_section_content()

        # Section should exist
        assert f'id="{section_id}"' in content, \
            f"{section_name} section should exist (id='{section_id}')"

        # Section should have a description paragraph
        section_match = re.search(
            rf'id="{section_id}".*?<p class="section-description">(.*?)</p>',
            content,
            re.DOTALL
        )
        assert section_match is not None, \
            f"{section_name} section should have a <p class='section-description'> paragraph"

        description = section_match.group(1).strip()
        assert len(description) >= MIN_DESCRIPTION_LENGTH, \
            f"{section_name} description should be meaningful (>={MIN_DESCRIPTION_LENGTH} chars), got: {description[:100]}"

    def test_all_section_descriptions_have_meaningful_content(self):
        """
        All section descriptions should have meaningful content (not placeholders).

        This verifies that descriptions are:
        - At least MIN_DESCRIPTION_LENGTH characters long
        - Not placeholder text like "TODO" or "Description here"
        """
        content = self.get_config_section_content()

        # Find all section descriptions
        descriptions = re.findall(
            r'<p class="section-description">(.*?)</p>',
            content,
            re.DOTALL
        )

        assert len(descriptions) > 0, \
            "Should find at least one section-description paragraph"

        for i, desc in enumerate(descriptions):
            desc_text = desc.strip()
            assert len(desc_text) >= MIN_DESCRIPTION_LENGTH, \
                f"Section description #{i+1} is too short: {desc_text[:100]}"
            assert "TODO" not in desc_text.upper(), \
                f"Section description #{i+1} contains placeholder TODO"
            assert "description here" not in desc_text.lower(), \
                f"Section description #{i+1} contains placeholder text"
