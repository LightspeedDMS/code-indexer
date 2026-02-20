"""
Unit tests for Story #234: Fix Delta Analysis Overwriting Full Domain Documentation.

Tests the truncation guard in _update_domain_file() and related behaviors:
- AC1: Delta analysis preserves full document structure
- AC3: Truncation guard prevents data loss from summary-only responses
- AC4: Legitimate short updates are not blocked
"""

import logging
from pathlib import Path
from unittest.mock import Mock

import pytest

from code_indexer.server.services.dependency_map_service import DependencyMapService
from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig


FULL_DOMAIN_CONTENT = """\
---
domain: cidx-platform
last_analyzed: 2024-01-01T00:00:00Z
participating_repos:
  - repo-a
  - repo-b
---

# Domain Analysis: cidx-platform

## Overview

The cidx-platform domain provides the core code indexing and search infrastructure.
It handles vector embedding, HNSW index management, and semantic search queries.
This domain serves as the foundation for all search capabilities in the system.

## Repository Roles

### repo-a
Provides vector embedding pipeline using VoyageAI models. Handles batch processing
of source code files, token counting, and embedding storage in the filesystem vector store.

### repo-b
Manages the HNSW index for approximate nearest-neighbor search. Provides query
execution, result ranking, and score filtering for semantic code queries.

## Intra-Domain Dependencies

| Consumer | Provider | Dependency Type | Evidence |
|----------|----------|-----------------|---------|
| repo-b | repo-a | Code-level | repo-b imports EmbeddingStore from repo-a |
| repo-b | repo-a | Data contracts | Shared vector format JSON schema |

## Cross-Domain Connections

### Outgoing Dependencies

| From Repo | To Domain | To Repo | Dependency Type | Evidence |
|-----------|-----------|---------|-----------------|---------|
| repo-a | auth-domain | auth-service | Service integration | repo-a calls auth-service for API key validation |

### Incoming Dependencies

| From Domain | From Repo | To Repo | Dependency Type | Evidence |
|-------------|-----------|---------|-----------------|---------|
| frontend | web-app | repo-b | Service integration | web-app calls repo-b REST API for search queries |
"""

SMALL_DOMAIN_CONTENT = """\
---
domain: tiny-domain
last_analyzed: 2024-01-01T00:00:00Z
participating_repos:
  - solo-repo
---

# Domain Analysis: tiny-domain

## Overview

Small domain with single repo.

## Repository Roles

### solo-repo
Standalone utility library.
"""

CHANGE_SUMMARY_RESPONSE = "Summary of changes: repo-a added new embedding model support."

UPDATED_FULL_RESPONSE = """\
# Domain Analysis: cidx-platform

## Overview

The cidx-platform domain provides the core code indexing and search infrastructure.
It handles vector embedding, HNSW index management, and semantic search queries.
This domain serves as the foundation for all search capabilities in the system.
Updated to reflect new voyage-3-large model support in repo-a.

## Repository Roles

### repo-a
Provides vector embedding pipeline using VoyageAI models. Now supports voyage-3-large
with 1536 dimensions. Handles batch processing of source code files, token counting,
and embedding storage in the filesystem vector store.

### repo-b
Manages the HNSW index for approximate nearest-neighbor search. Provides query
execution, result ranking, and score filtering for semantic code queries.

## Intra-Domain Dependencies

| Consumer | Provider | Dependency Type | Evidence |
|----------|----------|-----------------|---------|
| repo-b | repo-a | Code-level | repo-b imports EmbeddingStore from repo-a |
| repo-b | repo-a | Data contracts | Shared vector format JSON schema (updated for 1536-dim) |

## Cross-Domain Connections

### Outgoing Dependencies

| From Repo | To Domain | To Repo | Dependency Type | Evidence |
|-----------|-----------|---------|-----------------|---------|
| repo-a | auth-domain | auth-service | Service integration | repo-a calls auth-service for API key validation |

### Incoming Dependencies

| From Domain | From Repo | To Repo | Dependency Type | Evidence |
|-------------|-----------|---------|-----------------|---------|
| frontend | web-app | repo-b | Service integration | web-app calls repo-b REST API for search queries |
"""


def _make_service_with_mock_analyzer(tmp_path: Path, mock_analyzer: Mock) -> DependencyMapService:
    """Create a DependencyMapService with a mock analyzer for unit testing."""
    config_manager = Mock()
    config = ClaudeIntegrationConfig(
        dependency_map_enabled=True,
        dependency_map_interval_hours=24,
        dependency_map_pass_timeout_seconds=300,
        dependency_map_delta_max_turns=30,
    )
    config_manager.get_claude_integration_config.return_value = config

    golden_repos_manager = Mock()
    golden_repos_manager.golden_repos_dir = tmp_path / "golden-repos"

    return DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=config_manager,
        tracking_backend=Mock(),
        analyzer=mock_analyzer,
    )


def _make_config() -> ClaudeIntegrationConfig:
    """Create a ClaudeIntegrationConfig for testing."""
    return ClaudeIntegrationConfig(
        dependency_map_enabled=True,
        dependency_map_interval_hours=24,
        dependency_map_pass_timeout_seconds=300,
        dependency_map_delta_max_turns=30,
    )


class TestTruncationGuardAC3:
    """AC3: Truncation guard prevents data loss from summary-only responses."""

    def test_short_response_does_not_overwrite_long_existing_content(self, tmp_path: Path):
        """
        Given a domain file with 2000+ chars of documentation
        And Claude CLI returns only a short change summary (~200 chars)
        When _update_domain_file processes the response
        Then the short response is detected as suspicious
        And the file is NOT overwritten (existing content preserved)
        """
        # Arrange: write a large domain file (should be >> 500 chars body)
        domain_file = tmp_path / "cidx-platform.md"
        domain_file.write_text(FULL_DOMAIN_CONTENT)

        # Confirm existing body is large (> 500 chars)
        existing_body = FULL_DOMAIN_CONTENT.split("---\n\n", 1)[-1]
        assert len(existing_body) > 500, f"Test setup: body should be >500 chars, got {len(existing_body)}"
        assert len(CHANGE_SUMMARY_RESPONSE) < len(existing_body) * 0.5, \
            "Test setup: short response should be < 50% of existing body"

        mock_analyzer = Mock()
        mock_analyzer.build_delta_merge_prompt.return_value = "test prompt"
        mock_analyzer.invoke_delta_merge.return_value = CHANGE_SUMMARY_RESPONSE

        service = _make_service_with_mock_analyzer(tmp_path, mock_analyzer)

        original_content = domain_file.read_text()

        # Act
        service._update_domain_file(
            domain_name="cidx-platform",
            domain_file=domain_file,
            changed_repos=["repo-a"],
            new_repos=[],
            removed_repos=[],
            domain_list=["cidx-platform"],
            config=_make_config(),
        )

        # Assert: file is NOT overwritten with the short summary
        result_content = domain_file.read_text()
        assert result_content == original_content, (
            "File should not be overwritten when delta response is suspiciously short. "
            f"Expected preserved content ({len(original_content)} chars), "
            f"but file changed to ({len(result_content)} chars)"
        )
        assert CHANGE_SUMMARY_RESPONSE not in result_content, \
            "Short summary-only response should NOT appear in file after truncation guard fires"

    def test_truncation_guard_logs_warning(self, tmp_path: Path, caplog):
        """
        Given a suspiciously short delta response
        When _update_domain_file processes it
        Then a warning is logged with the length discrepancy
        """
        domain_file = tmp_path / "cidx-platform.md"
        domain_file.write_text(FULL_DOMAIN_CONTENT)

        mock_analyzer = Mock()
        mock_analyzer.build_delta_merge_prompt.return_value = "test prompt"
        mock_analyzer.invoke_delta_merge.return_value = CHANGE_SUMMARY_RESPONSE

        service = _make_service_with_mock_analyzer(tmp_path, mock_analyzer)

        with caplog.at_level(logging.WARNING):
            service._update_domain_file(
                domain_name="cidx-platform",
                domain_file=domain_file,
                changed_repos=["repo-a"],
                new_repos=[],
                removed_repos=[],
                domain_list=["cidx-platform"],
                config=_make_config(),
            )

        # Should have logged a warning mentioning "short" or length discrepancy
        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(
            "short" in msg.lower() or "truncat" in msg.lower() or "preserv" in msg.lower()
            for msg in warning_messages
        ), f"Expected truncation warning in logs. Got warnings: {warning_messages}"

    def test_truncation_guard_threshold_exactly_50_percent_of_body(self, tmp_path: Path):
        """
        The threshold is response_len < existing_body_len * 0.5 AND existing_body > 500.
        A response at exactly 50% should NOT trigger the guard (boundary test).
        """
        domain_file = tmp_path / "cidx-platform.md"
        domain_file.write_text(FULL_DOMAIN_CONTENT)

        # Strip frontmatter to get body length
        parts = FULL_DOMAIN_CONTENT.split("---\n\n", 1)
        existing_body = parts[-1] if len(parts) > 1 else FULL_DOMAIN_CONTENT
        existing_body_len = len(existing_body)
        assert existing_body_len > 500

        # Response at exactly 50% — should NOT trigger guard (boundary: < 0.5, not <=)
        response_at_50_pct = "x" * int(existing_body_len * 0.5)

        mock_analyzer = Mock()
        mock_analyzer.build_delta_merge_prompt.return_value = "test prompt"
        mock_analyzer.invoke_delta_merge.return_value = response_at_50_pct

        service = _make_service_with_mock_analyzer(tmp_path, mock_analyzer)

        service._update_domain_file(
            domain_name="cidx-platform",
            domain_file=domain_file,
            changed_repos=["repo-a"],
            new_repos=[],
            removed_repos=[],
            domain_list=["cidx-platform"],
            config=_make_config(),
        )

        # At exactly 50%, guard should NOT fire — file should be updated
        result_content = domain_file.read_text()
        assert response_at_50_pct in result_content, \
            "At exactly 50% threshold, truncation guard should NOT fire"


class TestLegitimateShortUpdatesAC4:
    """AC4: Legitimate short updates are not blocked."""

    def test_small_domain_accepts_proportional_response(self, tmp_path: Path):
        """
        Given a domain file with 500 chars (small domain)
        And Claude CLI returns 400 chars (updated analysis)
        When _update_domain_file processes the response
        Then the update proceeds normally (guard does not fire for small domains)
        """
        domain_file = tmp_path / "tiny-domain.md"
        domain_file.write_text(SMALL_DOMAIN_CONTENT)

        # Verify small domain body is <= 500 chars (guard should not fire)
        parts = SMALL_DOMAIN_CONTENT.split("---\n\n", 1)
        existing_body = parts[-1] if len(parts) > 1 else SMALL_DOMAIN_CONTENT
        assert len(existing_body) <= 500, (
            f"Test setup: SMALL_DOMAIN_CONTENT body should be <=500 chars, got {len(existing_body)}"
        )

        short_but_valid_response = "# Domain Analysis: tiny-domain\n\n## Overview\n\nUpdated content."

        mock_analyzer = Mock()
        mock_analyzer.build_delta_merge_prompt.return_value = "test prompt"
        mock_analyzer.invoke_delta_merge.return_value = short_but_valid_response

        service = _make_service_with_mock_analyzer(tmp_path, mock_analyzer)

        service._update_domain_file(
            domain_name="tiny-domain",
            domain_file=domain_file,
            changed_repos=["solo-repo"],
            new_repos=[],
            removed_repos=[],
            domain_list=["tiny-domain"],
            config=_make_config(),
        )

        # File should be updated (guard did not fire)
        result_content = domain_file.read_text()
        assert short_but_valid_response in result_content, \
            "Small domains should accept short responses (guard only applies when body > 500 chars)"

    def test_large_domain_accepts_proportional_long_response(self, tmp_path: Path):
        """
        Given a domain file with 2000+ chars
        And Claude CLI returns a proportional response (> 50% of body length)
        When _update_domain_file processes the response
        Then the update proceeds normally
        """
        domain_file = tmp_path / "cidx-platform.md"
        domain_file.write_text(FULL_DOMAIN_CONTENT)

        mock_analyzer = Mock()
        mock_analyzer.build_delta_merge_prompt.return_value = "test prompt"
        mock_analyzer.invoke_delta_merge.return_value = UPDATED_FULL_RESPONSE

        service = _make_service_with_mock_analyzer(tmp_path, mock_analyzer)

        service._update_domain_file(
            domain_name="cidx-platform",
            domain_file=domain_file,
            changed_repos=["repo-a"],
            new_repos=[],
            removed_repos=[],
            domain_list=["cidx-platform"],
            config=_make_config(),
        )

        result_content = domain_file.read_text()
        assert UPDATED_FULL_RESPONSE in result_content, \
            "Full proportional response should update the file normally"
        # Confirm key sections of updated response are present
        assert "voyage-3-large" in result_content, \
            "Updated content should contain the new analysis details"

    def test_empty_existing_content_does_not_crash(self, tmp_path: Path):
        """
        Edge case: existing domain file has no frontmatter or body.
        Guard should handle gracefully (no crash).
        """
        domain_file = tmp_path / "empty-domain.md"
        domain_file.write_text("")

        mock_analyzer = Mock()
        mock_analyzer.build_delta_merge_prompt.return_value = "test prompt"
        mock_analyzer.invoke_delta_merge.return_value = "# Domain Analysis: empty-domain\n\nNew content."

        service = _make_service_with_mock_analyzer(tmp_path, mock_analyzer)

        # Should not raise
        service._update_domain_file(
            domain_name="empty-domain",
            domain_file=domain_file,
            changed_repos=[],
            new_repos=[],
            removed_repos=[],
            domain_list=["empty-domain"],
            config=_make_config(),
        )

    def test_no_frontmatter_does_not_trigger_false_positive(self, tmp_path: Path):
        """
        Edge case: existing content has no frontmatter (plain markdown).
        Guard should measure the full content length, not crash.
        """
        # Large content without frontmatter
        large_content_no_frontmatter = "# Domain\n\n" + "Content line.\n" * 80
        domain_file = tmp_path / "no-frontmatter.md"
        domain_file.write_text(large_content_no_frontmatter)

        # A proportional response (> 50%)
        proportional_response = "# Domain\n\n" + "Updated content.\n" * 50

        mock_analyzer = Mock()
        mock_analyzer.build_delta_merge_prompt.return_value = "test prompt"
        mock_analyzer.invoke_delta_merge.return_value = proportional_response

        service = _make_service_with_mock_analyzer(tmp_path, mock_analyzer)

        service._update_domain_file(
            domain_name="no-frontmatter",
            domain_file=domain_file,
            changed_repos=[],
            new_repos=[],
            removed_repos=[],
            domain_list=["no-frontmatter"],
            config=_make_config(),
        )

        result_content = domain_file.read_text()
        assert proportional_response in result_content, \
            "Proportional update to no-frontmatter file should succeed"


class TestFrontmatterTimestampAC1:
    """AC1: Delta analysis preserves full document structure."""

    def test_update_frontmatter_timestamp_preserves_body_structure(self, tmp_path: Path):
        """
        _update_frontmatter_timestamp must replace the body with new_body
        and only update the last_analyzed timestamp in frontmatter.
        The rest of the frontmatter (domain, participating_repos) must be preserved.
        """
        service = _make_service_with_mock_analyzer(tmp_path, Mock())

        existing = """\
---
domain: cidx-platform
last_analyzed: 2024-01-01T00:00:00Z
participating_repos:
  - repo-a
  - repo-b
---

# Domain Analysis: cidx-platform

## Overview

Old overview content.

## Repository Roles

Old repo roles.
"""
        new_body = """\
# Domain Analysis: cidx-platform

## Overview

Updated overview with new repo details.

## Repository Roles

Updated repo roles with evidence.

## Intra-Domain Dependencies

| Consumer | Provider | Dependency Type | Evidence |
|----------|----------|-----------------|---------|
| repo-b | repo-a | Code-level | Imports EmbeddingStore |
"""
        result = service._update_frontmatter_timestamp(existing, new_body, "cidx-platform")

        # Frontmatter fields (other than last_analyzed) must be preserved
        assert "domain: cidx-platform" in result
        assert "participating_repos:" in result
        assert "repo-a" in result
        assert "repo-b" in result

        # last_analyzed must be updated (not the old 2024-01-01 value)
        assert "2024-01-01T00:00:00Z" not in result

        # New body must be present
        assert "Updated overview with new repo details." in result
        assert "Intra-Domain Dependencies" in result
        assert "Imports EmbeddingStore" in result

        # Old body must NOT be present
        assert "Old overview content." not in result
        assert "Old repo roles." not in result

    def test_delta_update_with_full_response_preserves_all_sections(self, tmp_path: Path):
        """
        AC1: When delta response is a full document, all sections are preserved in output.
        """
        domain_file = tmp_path / "cidx-platform.md"
        domain_file.write_text(FULL_DOMAIN_CONTENT)

        mock_analyzer = Mock()
        mock_analyzer.build_delta_merge_prompt.return_value = "test prompt"
        mock_analyzer.invoke_delta_merge.return_value = UPDATED_FULL_RESPONSE

        service = _make_service_with_mock_analyzer(tmp_path, mock_analyzer)

        service._update_domain_file(
            domain_name="cidx-platform",
            domain_file=domain_file,
            changed_repos=["repo-a"],
            new_repos=[],
            removed_repos=[],
            domain_list=["cidx-platform"],
            config=_make_config(),
        )

        result = domain_file.read_text()

        # All major sections must be present
        assert "## Overview" in result
        assert "## Repository Roles" in result
        assert "## Intra-Domain Dependencies" in result
        assert "## Cross-Domain Connections" in result

        # Frontmatter must still be present
        assert "---" in result
        assert "domain: cidx-platform" in result
