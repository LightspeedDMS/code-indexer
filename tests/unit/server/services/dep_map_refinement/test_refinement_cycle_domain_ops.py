"""
Unit tests for Story #359: Domain operations in the refinement cycle.

Tests cover:
- Component 6: last_refined timestamp set; last_analyzed preserved
- Component 7: Orphaned domain file creation
- Component 8: refinement_enabled=False means no-op
- Component 10: _index.md regeneration after batch with changes

TDD RED PHASE: Tests written before production code exists.
"""

from pathlib import Path
from unittest.mock import Mock

import pytest

from .conftest import (
    FULL_DOMAIN_BODY,
    FULL_DOMAIN_CONTENT,
    SAMPLE_DOMAINS_JSON,
    make_config,
    make_dependency_map_dir,
    make_live_dep_map,
    make_service,
)


class TestOrphanedDomainCreation:
    """refine_or_create_domain creates .md file when it doesn't exist (Component 7)."""

    def test_missing_md_uses_new_domain_prompt(self, tmp_path: Path):
        """
        Given a domain in SAMPLE_DOMAINS_JSON with no .md file anywhere
        When refine_or_create_domain is called
        Then build_new_domain_prompt is called (not build_refinement_prompt).
        """
        dep_map = make_dependency_map_dir(tmp_path)
        live_dep_map = make_live_dep_map(tmp_path)

        new_domain_result = (
            "# Domain Analysis: auth-domain\n\n## Overview\n\nNewly created.\n"
        )

        mock_analyzer = Mock()
        mock_analyzer.build_new_domain_prompt.return_value = "new domain prompt"
        mock_analyzer.build_refinement_prompt.return_value = "refinement prompt"
        mock_analyzer.invoke_refinement.return_value = new_domain_result

        config = make_config()
        service = make_service(tmp_path, mock_analyzer, config)

        result = service.refine_or_create_domain(
            domain_name="auth-domain",
            domain_info=SAMPLE_DOMAINS_JSON[0],
            dependency_map_dir=live_dep_map,
            dependency_map_read_dir=dep_map,
            config=config,
        )

        mock_analyzer.build_new_domain_prompt.assert_called_once()
        mock_analyzer.build_refinement_prompt.assert_not_called()
        assert (live_dep_map / "auth-domain.md").exists()
        assert result is True

    def test_created_file_has_frontmatter(self, tmp_path: Path):
        """
        When an orphaned domain file is created
        Then it has proper YAML frontmatter including the domain name.
        """
        dep_map = make_dependency_map_dir(tmp_path)
        live_dep_map = make_live_dep_map(tmp_path)

        new_domain_result = (
            "# Domain Analysis: auth-domain\n\n## Overview\n\nContent here.\n"
        )

        mock_analyzer = Mock()
        mock_analyzer.build_new_domain_prompt.return_value = "new domain prompt"
        mock_analyzer.invoke_refinement.return_value = new_domain_result

        config = make_config()
        service = make_service(tmp_path, mock_analyzer, config)

        service.refine_or_create_domain(
            domain_name="auth-domain",
            domain_info=SAMPLE_DOMAINS_JSON[0],
            dependency_map_dir=live_dep_map,
            dependency_map_read_dir=dep_map,
            config=config,
        )

        written = (live_dep_map / "auth-domain.md").read_text()
        assert written.startswith("---"), "Created file must start with YAML frontmatter"
        assert "domain: auth-domain" in written

    def test_created_file_has_last_refined(self, tmp_path: Path):
        """
        When an orphaned domain file is created
        Then the frontmatter includes last_refined timestamp.
        """
        dep_map = make_dependency_map_dir(tmp_path)
        live_dep_map = make_live_dep_map(tmp_path)

        new_domain_result = (
            "# Domain Analysis: auth-domain\n\n## Overview\n\nContent here.\n"
        )

        mock_analyzer = Mock()
        mock_analyzer.build_new_domain_prompt.return_value = "new domain prompt"
        mock_analyzer.invoke_refinement.return_value = new_domain_result

        config = make_config()
        service = make_service(tmp_path, mock_analyzer, config)

        service.refine_or_create_domain(
            domain_name="auth-domain",
            domain_info=SAMPLE_DOMAINS_JSON[0],
            dependency_map_dir=live_dep_map,
            dependency_map_read_dir=dep_map,
            config=config,
        )

        written = (live_dep_map / "auth-domain.md").read_text()
        assert "last_refined:" in written


class TestLastRefinedTimestampSet:
    """refine_or_create_domain sets last_refined in frontmatter (Component 6)."""

    def test_last_refined_added_to_frontmatter(self, tmp_path: Path):
        """
        When refine_or_create_domain updates a domain file with new content
        Then the output has last_refined in the YAML frontmatter.
        """
        dep_map = make_dependency_map_dir(tmp_path)
        live_dep_map = make_live_dep_map(tmp_path)

        (dep_map / "auth-domain.md").write_text(FULL_DOMAIN_CONTENT)

        # Different from FULL_DOMAIN_BODY to avoid no-op detection
        different_result = FULL_DOMAIN_BODY + "\n\nAdditional fact-checked content.\n"

        mock_analyzer = Mock()
        mock_analyzer.build_refinement_prompt.return_value = "prompt"
        mock_analyzer.invoke_refinement.return_value = different_result

        config = make_config()
        service = make_service(tmp_path, mock_analyzer, config)

        service.refine_or_create_domain(
            domain_name="auth-domain",
            domain_info=SAMPLE_DOMAINS_JSON[0],
            dependency_map_dir=live_dep_map,
            dependency_map_read_dir=dep_map,
            config=config,
        )

        written = (live_dep_map / "auth-domain.md").read_text()
        assert "last_refined:" in written, (
            "Updated file must have last_refined timestamp in frontmatter"
        )

    def test_last_refined_is_recent_timestamp(self, tmp_path: Path):
        """
        The last_refined timestamp must be a non-empty ISO timestamp string.
        """
        dep_map = make_dependency_map_dir(tmp_path)
        live_dep_map = make_live_dep_map(tmp_path)

        (dep_map / "auth-domain.md").write_text(FULL_DOMAIN_CONTENT)

        different_result = FULL_DOMAIN_BODY + "\n\nModified content.\n"

        mock_analyzer = Mock()
        mock_analyzer.build_refinement_prompt.return_value = "prompt"
        mock_analyzer.invoke_refinement.return_value = different_result

        config = make_config()
        service = make_service(tmp_path, mock_analyzer, config)

        service.refine_or_create_domain(
            domain_name="auth-domain",
            domain_info=SAMPLE_DOMAINS_JSON[0],
            dependency_map_dir=live_dep_map,
            dependency_map_read_dir=dep_map,
            config=config,
        )

        written = (live_dep_map / "auth-domain.md").read_text()
        # Extract last_refined value
        for line in written.splitlines():
            if line.startswith("last_refined:"):
                value = line.split(":", 1)[1].strip()
                assert value, "last_refined must have a non-empty timestamp value"
                # Must look like an ISO timestamp (starts with year)
                assert value[:4].isdigit(), (
                    f"last_refined must be an ISO timestamp, got: {value!r}"
                )
                break
        else:
            pytest.fail("last_refined key not found in frontmatter")


class TestLastAnalyzedPreserved:
    """refine_or_create_domain preserves existing last_analyzed timestamp (Component 6)."""

    def test_last_analyzed_not_changed_by_refinement(self, tmp_path: Path):
        """
        Given a domain file with last_analyzed: 2024-01-01T00:00:00+00:00
        When refine_or_create_domain updates the file
        Then last_analyzed is preserved (not overwritten with current time).
        """
        dep_map = make_dependency_map_dir(tmp_path)
        live_dep_map = make_live_dep_map(tmp_path)

        (dep_map / "auth-domain.md").write_text(FULL_DOMAIN_CONTENT)

        different_result = FULL_DOMAIN_BODY + "\n\nExtra content to make it different.\n"

        mock_analyzer = Mock()
        mock_analyzer.build_refinement_prompt.return_value = "prompt"
        mock_analyzer.invoke_refinement.return_value = different_result

        config = make_config()
        service = make_service(tmp_path, mock_analyzer, config)

        service.refine_or_create_domain(
            domain_name="auth-domain",
            domain_info=SAMPLE_DOMAINS_JSON[0],
            dependency_map_dir=live_dep_map,
            dependency_map_read_dir=dep_map,
            config=config,
        )

        written = (live_dep_map / "auth-domain.md").read_text()
        assert "last_analyzed: 2024-01-01T00:00:00+00:00" in written, (
            "last_analyzed must be preserved from original file, "
            "not changed by refinement"
        )


class TestRefinementDisabledSkips:
    """run_refinement_cycle skips processing when refinement_enabled=False (Component 8)."""

    def test_refinement_disabled_does_not_invoke_refinement(self, tmp_path: Path):
        """
        Given refinement_enabled=False
        When run_refinement_cycle() is called
        Then invoke_refinement is never called.
        """
        dep_map = make_dependency_map_dir(tmp_path)
        import json
        (dep_map / "_domains.json").write_text(json.dumps(SAMPLE_DOMAINS_JSON))

        mock_analyzer = Mock()
        config = make_config(refinement_enabled=False)
        service = make_service(tmp_path, mock_analyzer, config)

        service.run_refinement_cycle()

        mock_analyzer.invoke_refinement.assert_not_called()
        mock_analyzer.build_refinement_prompt.assert_not_called()

    def test_refinement_disabled_returns_none(self, tmp_path: Path):
        """
        Given refinement_enabled=False
        When run_refinement_cycle() is called
        Then it returns None (early exit).
        """
        mock_analyzer = Mock()
        config = make_config(refinement_enabled=False)
        service = make_service(tmp_path, mock_analyzer, config)

        result = service.run_refinement_cycle()
        assert result is None


class TestIndexMdRegenerationAfterBatch:
    """_index.md is regenerated when at least one domain changed (Component 10)."""

    def test_index_md_regenerated_when_domain_changed(self, tmp_path: Path):
        """
        Given at least one domain was updated by refinement
        When run_refinement_cycle() completes
        Then _generate_index_md is called on the analyzer.
        """
        import json
        dep_map = make_dependency_map_dir(tmp_path)
        (dep_map / "_domains.json").write_text(json.dumps(SAMPLE_DOMAINS_JSON[:1]))
        (dep_map / "auth-domain.md").write_text(FULL_DOMAIN_CONTENT)
        make_live_dep_map(tmp_path)

        # Return different content to trigger a change
        different_result = FULL_DOMAIN_BODY + "\n\nAdditional content here.\n"

        mock_analyzer = Mock()
        mock_analyzer.build_refinement_prompt.return_value = "prompt"
        mock_analyzer.invoke_refinement.return_value = different_result
        mock_analyzer._generate_index_md = Mock()

        config = make_config(refinement_domains_per_run=1)
        service = make_service(tmp_path, mock_analyzer, config)

        service.run_refinement_cycle()

        mock_analyzer._generate_index_md.assert_called_once()

    def test_index_md_not_regenerated_when_no_changes(self, tmp_path: Path):
        """
        Given no domains were changed (all returned identical content)
        When run_refinement_cycle() completes
        Then _generate_index_md is NOT called.
        """
        import json
        dep_map = make_dependency_map_dir(tmp_path)
        (dep_map / "_domains.json").write_text(json.dumps(SAMPLE_DOMAINS_JSON[:1]))
        (dep_map / "auth-domain.md").write_text(FULL_DOMAIN_CONTENT)
        make_live_dep_map(tmp_path)

        # Return identical body — no change
        mock_analyzer = Mock()
        mock_analyzer.build_refinement_prompt.return_value = "prompt"
        mock_analyzer.invoke_refinement.return_value = FULL_DOMAIN_BODY
        mock_analyzer._generate_index_md = Mock()

        config = make_config(refinement_domains_per_run=1)
        service = make_service(tmp_path, mock_analyzer, config)

        service.run_refinement_cycle()

        mock_analyzer._generate_index_md.assert_not_called()
