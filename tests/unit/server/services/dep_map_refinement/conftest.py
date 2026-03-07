"""
Shared fixtures and helpers for Story #359 refinement cycle tests.

Provides common domain content, config builders, and service factories
used across the refinement test suite.
"""

from pathlib import Path
from unittest.mock import Mock

from code_indexer.server.services.dependency_map_service import DependencyMapService
from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig


# ---------------------------------------------------------------------------
# Shared domain content
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
    "---\n\n"
    + FULL_DOMAIN_BODY
)

SAMPLE_DOMAINS_JSON = [
    {
        "name": "auth-domain",
        "description": "Authentication and authorization",
        "participating_repos": ["auth-service", "token-validator"],
    },
    {
        "name": "data-pipeline",
        "description": "ETL pipeline",
        "participating_repos": ["etl-service", "loader"],
    },
    {
        "name": "api-gateway",
        "description": "API gateway",
        "participating_repos": ["gateway-service"],
    },
]


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def make_config(
    refinement_enabled: bool = True,
    refinement_interval_hours: int = 24,
    refinement_domains_per_run: int = 2,
    dependency_map_pass_timeout_seconds: int = 300,
    dependency_map_delta_max_turns: int = 30,
) -> ClaudeIntegrationConfig:
    """Build ClaudeIntegrationConfig with refinement fields."""
    return ClaudeIntegrationConfig(
        dependency_map_enabled=True,
        dependency_map_interval_hours=24,
        dependency_map_pass_timeout_seconds=dependency_map_pass_timeout_seconds,
        dependency_map_delta_max_turns=dependency_map_delta_max_turns,
        refinement_enabled=refinement_enabled,
        refinement_interval_hours=refinement_interval_hours,
        refinement_domains_per_run=refinement_domains_per_run,
    )


def make_service(
    tmp_path: Path,
    mock_analyzer: Mock = None,
    config: ClaudeIntegrationConfig = None,
    tracking_data: dict = None,
) -> DependencyMapService:
    """Create a DependencyMapService with mock dependencies for unit testing."""
    if mock_analyzer is None:
        mock_analyzer = Mock()
        mock_analyzer.invoke_refinement.return_value = FULL_DOMAIN_BODY
        mock_analyzer.build_refinement_prompt.return_value = "refinement prompt"
        mock_analyzer.build_new_domain_prompt.return_value = "new domain prompt"

    if config is None:
        config = make_config()

    config_manager = Mock()
    config_manager.get_claude_integration_config.return_value = config

    golden_repos_manager = Mock()
    golden_repos_manager.golden_repos_dir = str(tmp_path / "golden-repos")

    tracking_backend = Mock()
    default_tracking = {
        "id": 1,
        "last_run": None,
        "next_run": None,
        "status": "pending",
        "commit_hashes": None,
        "error_message": None,
        "refinement_cursor": 0,
        "refinement_next_run": None,
    }
    if tracking_data:
        default_tracking.update(tracking_data)
    tracking_backend.get_tracking.return_value = default_tracking
    tracking_backend.update_tracking = Mock()

    return DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=config_manager,
        tracking_backend=tracking_backend,
        analyzer=mock_analyzer,
    )


def make_dependency_map_dir(tmp_path: Path) -> Path:
    """Create a versioned cidx-meta path with dependency-map structure.

    Returns the versioned dep-map dir (read path). The live write path
    must be created separately by each test that needs it.
    """
    versioned_dir = (
        tmp_path / "golden-repos" / ".versioned" / "cidx-meta" / "v_20240101000000"
    )
    dep_map = versioned_dir / "dependency-map"
    dep_map.mkdir(parents=True)
    return dep_map


def make_live_dep_map(tmp_path: Path) -> Path:
    """Create and return the live dependency-map directory (write path)."""
    live_dep_map = tmp_path / "golden-repos" / "cidx-meta" / "dependency-map"
    live_dep_map.mkdir(parents=True)
    return live_dep_map
