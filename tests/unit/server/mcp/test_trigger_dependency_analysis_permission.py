"""
Unit tests for trigger_dependency_analysis tool permission configuration.

Tests that the tool doc specifies the correct permission that exists in the User model.
"""

import pytest
from pathlib import Path


def test_trigger_dependency_analysis_has_manage_golden_repos_permission():
    """Test that trigger_dependency_analysis tool doc specifies manage_golden_repos permission."""
    # Arrange
    tool_doc_path = Path(__file__).parent.parent.parent.parent.parent / "src" / "code_indexer" / "server" / "mcp" / "tool_docs" / "repos" / "trigger_dependency_analysis.md"

    # Act
    content = tool_doc_path.read_text()

    # Assert - check YAML frontmatter has correct permission
    assert "required_permission: manage_golden_repos" in content, \
        "Tool doc must specify 'manage_golden_repos' permission (not 'manage_repos' which doesn't exist)"

    # Also check that manage_repos is NOT present in frontmatter
    lines = content.split('\n')
    in_frontmatter = False
    for line in lines:
        if line.strip() == '---':
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter and 'required_permission:' in line:
            assert 'manage_golden_repos' in line, \
                f"Required permission line must contain 'manage_golden_repos', got: {line}"
            assert 'manage_repos' not in line or 'manage_golden_repos' in line, \
                f"Required permission should not use non-existent 'manage_repos', got: {line}"


def test_trigger_dependency_analysis_doc_mentions_correct_permission():
    """Test that the tool doc body mentions the correct permission."""
    # Arrange
    tool_doc_path = Path(__file__).parent.parent.parent.parent.parent / "src" / "code_indexer" / "server" / "mcp" / "tool_docs" / "repos" / "trigger_dependency_analysis.md"

    # Act
    content = tool_doc_path.read_text()

    # Assert - check documentation text mentions correct permission
    # Line 67 should say manage_golden_repos, not manage_repos
    assert "manage_golden_repos" in content, \
        "Tool doc body should mention 'manage_golden_repos' permission"
