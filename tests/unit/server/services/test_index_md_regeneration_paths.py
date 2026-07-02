"""
Tests for _index.md regeneration in refinement and delta paths.

Regression guard against two corruption bugs:
1. Refinement path called _generate_index_md with repo_list=[] wiping repo data.
2. Delta path never called regeneration at all, leaving _index.md stale.

Both paths must use IndexRegenerator.regenerate() for correct output.
"""

import json
from pathlib import Path
from unittest.mock import Mock, patch

from code_indexer.server.services.dependency_map_service import DependencyMapService
from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

FULL_DOMAIN_BODY = """\
# Domain Analysis: auth-domain

## Overview

The auth-domain provides authentication and authorization services.
It handles JWT token issuance, validation, and session management.
This is a substantial body of documentation exceeding five hundred characters to
ensure that the truncation guard logic is exercised correctly in tests.
The domain has two repositories: auth-service and token-validator.
Both play critical roles in the security infrastructure of the platform.

## Repository Roles

### auth-service
Issues JWT tokens for authenticated users.

### token-validator
Validates JWT tokens across service boundaries.

## Cross-Domain Connections

No verified cross-domain dependencies.
"""

FULL_DOMAIN_CONTENT = (
    "---\n"
    "domain: auth-domain\n"
    "last_analyzed: 2024-01-01T00:00:00+00:00\n"
    "participating_repos:\n"
    "  - auth-service\n"
    "  - token-validator\n"
    "---\n\n" + FULL_DOMAIN_BODY
)

SAMPLE_DOMAINS_JSON = [
    {
        "name": "auth-domain",
        "description": "Authentication and authorization",
        "participating_repos": ["auth-service", "token-validator"],
    },
]


def _make_config(
    refinement_enabled: bool = True,
    refinement_domains_per_run: int = 1,
) -> ClaudeIntegrationConfig:
    return ClaudeIntegrationConfig(
        dependency_map_enabled=True,
        dependency_map_interval_hours=24,
        dependency_map_pass_timeout_seconds=300,
        dependency_map_delta_max_turns=30,
        refinement_enabled=refinement_enabled,
        refinement_interval_hours=24,
        refinement_domains_per_run=refinement_domains_per_run,
    )


def _make_service(
    tmp_path: Path,
    mock_analyzer: Mock,
    config: ClaudeIntegrationConfig,
) -> DependencyMapService:
    config_manager = Mock()
    config_manager.get_claude_integration_config.return_value = config

    golden_repos_manager = Mock()
    golden_repos_manager.golden_repos_dir = str(tmp_path / "golden-repos")

    tracking_backend = Mock()
    tracking_backend.get_tracking.return_value = {
        "id": 1,
        "last_run": None,
        "next_run": None,
        "status": "pending",
        "commit_hashes": None,
        "error_message": None,
        "refinement_cursor": 0,
        "refinement_next_run": None,
    }
    tracking_backend.update_tracking = Mock()

    return DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=config_manager,
        tracking_backend=tracking_backend,
        analyzer=mock_analyzer,
    )


def _make_dep_map_dir(tmp_path: Path) -> Path:
    versioned_dir = (
        tmp_path / "golden-repos" / ".versioned" / "cidx-meta" / "v_20240101000000"
    )
    dep_map = versioned_dir / "dependency-map"
    dep_map.mkdir(parents=True)
    return dep_map


def _make_live_dep_map(tmp_path: Path) -> Path:
    live = tmp_path / "golden-repos" / "cidx-meta" / "dependency-map"
    live.mkdir(parents=True)
    return live


# ---------------------------------------------------------------------------
# Test 1: Refinement path uses IndexRegenerator
# ---------------------------------------------------------------------------


class TestRefinementPathUsesIndexRegenerator:
    """Refinement path calls IndexRegenerator.regenerate() when a domain changed."""

    def test_refinement_path_uses_index_regenerator(self, tmp_path: Path):
        """
        Given a domain that changes during refinement
        When run_refinement_cycle() completes
        Then IndexRegenerator.regenerate() is called, not _generate_index_md with [].
        """
        dep_map = _make_dep_map_dir(tmp_path)
        (dep_map / "_domains.json").write_text(json.dumps(SAMPLE_DOMAINS_JSON))
        (dep_map / "auth-domain.md").write_text(FULL_DOMAIN_CONTENT)
        _make_live_dep_map(tmp_path)

        different_result = FULL_DOMAIN_BODY + "\n\nAdditional content.\n"

        mock_analyzer = Mock()
        mock_analyzer.build_refinement_prompt.return_value = "prompt"
        mock_analyzer.invoke_refinement_file.return_value = different_result
        mock_analyzer._generate_index_md = Mock()

        config = _make_config(refinement_domains_per_run=1)
        service = _make_service(tmp_path, mock_analyzer, config)

        regenerate_calls = []

        def fake_regenerate(output_dir):
            regenerate_calls.append(output_dir)

        with patch(
            "code_indexer.server.services.dep_map_index_regenerator.IndexRegenerator.regenerate",
            side_effect=fake_regenerate,
        ):
            service.run_refinement_cycle()

        assert len(regenerate_calls) == 1, (
            "IndexRegenerator.regenerate() must be called exactly once after a changed domain"
        )
        mock_analyzer._generate_index_md.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: Delta path regenerates _index.md
# ---------------------------------------------------------------------------


class TestDeltaPathRegeneratesIndexMd:
    """Delta analysis path calls IndexRegenerator.regenerate() after domain updates."""

    def test_delta_path_regenerates_index_md(self, tmp_path: Path):
        """
        Given a delta analysis run that updates affected domains
        When run_delta_analysis() completes
        Then IndexRegenerator.regenerate() is called after _update_affected_domains().
        """
        live = _make_live_dep_map(tmp_path)
        (live / "_domains.json").write_text(json.dumps(SAMPLE_DOMAINS_JSON))
        (live / "auth-domain.md").write_text(FULL_DOMAIN_CONTENT)

        mock_analyzer = Mock()
        mock_analyzer.generate_orientation_files.return_value = None

        config = _make_config()
        service = _make_service(tmp_path, mock_analyzer, config)

        changed_repos = [{"alias": "auth-service", "clone_path": str(tmp_path)}]

        regenerate_calls = []

        def fake_regenerate(output_dir):
            regenerate_calls.append(output_dir)

        with (
            patch.object(
                service, "detect_changes", return_value=(changed_repos, [], [])
            ),
            patch.object(
                service, "identify_affected_domains", return_value={"auth-domain"}
            ),
            patch.object(service, "_update_affected_domains", return_value=[]),
            patch.object(service, "_finalize_delta_tracking"),
            patch.object(service, "_get_activated_repos", return_value=changed_repos),
            patch(
                "code_indexer.server.services.dep_map_index_regenerator.IndexRegenerator.regenerate",
                side_effect=fake_regenerate,
            ),
        ):
            service.run_delta_analysis()

        assert len(regenerate_calls) >= 1, (
            "IndexRegenerator.regenerate() must be called at least once during delta analysis"
        )


# ---------------------------------------------------------------------------
# Test 3: IndexRegenerator produces correct content
# ---------------------------------------------------------------------------


class TestIndexRegeneratorProducesCorrectContent:
    """Regression guard: IndexRegenerator produces correct _index.md from domain files."""

    def test_index_regenerator_produces_correct_content(self, tmp_path: Path):
        """
        Given domain files and _domains.json in a tmpdir
        When IndexRegenerator.regenerate() is called
        Then _index.md has non-zero repos_analyzed_count, populated repos_analyzed,
        and non-empty Repo-to-Domain Matrix rows.
        """
        from code_indexer.server.services.dep_map_index_regenerator import (
            IndexRegenerator,
        )

        (tmp_path / "_domains.json").write_text(json.dumps(SAMPLE_DOMAINS_JSON))
        (tmp_path / "auth-domain.md").write_text(FULL_DOMAIN_CONTENT)

        regenerator = IndexRegenerator()
        index_path = regenerator.regenerate(tmp_path)

        assert index_path.exists()
        text = index_path.read_text(encoding="utf-8")

        assert "repos_analyzed_count: 2" in text, (
            "repos_analyzed_count must be 2, not 0"
        )
        assert "auth-service" in text, "repos_analyzed list must contain auth-service"
        assert "token-validator" in text, (
            "repos_analyzed list must contain token-validator"
        )
        assert "| auth-service | auth-domain |" in text, (
            "Repo-to-Domain Matrix must have non-empty rows"
        )
        assert "| token-validator | auth-domain |" in text, (
            "Repo-to-Domain Matrix must have non-empty rows"
        )
