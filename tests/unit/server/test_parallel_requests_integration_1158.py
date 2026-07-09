"""Integration tests for Story #1158 - Configurable parallel requests.

Uses real SQLite (no mocks) to exercise the full config_service -> seed_provider_config
pipeline and verify that all 3 new fields are correctly stored and propagated.

Key scenarios:
- Save all 3 fields, seed to a temp repo: config.json must reflect saved values.
- Save temporal_parallel_requests=None, re-seed: config.json must contain JSON null
  (key must be PRESENT, not absent) -- Scenario 8 null-propagation regression guard.
"""

import json
from pathlib import Path
from typing import Any, Dict

import pytest

# Module-level imports kept at top to avoid per-test repetition.
from code_indexer.server.services.config_service import (
    ConfigService,
    set_config_service,
    reset_config_service,
)
from code_indexer.server.services import config_seeding
from code_indexer.server.utils.config_manager import ServerConfigManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_PROVIDER_CONFIG: Dict[str, Any] = {
    "voyage_ai": {"parallel_requests": 8, "timeout": 30},
    "cohere": {"parallel_requests": 8, "timeout": 30},
}


def _make_cidx_config(repo_dir: Path, content: Dict[str, Any]) -> Path:
    """Write .code-indexer/config.json under repo_dir."""
    cidx_dir = repo_dir / ".code-indexer"
    cidx_dir.mkdir(parents=True, exist_ok=True)
    (cidx_dir / "config.json").write_text(json.dumps(content, indent=2))
    return repo_dir


def _make_service(server_dir: Path) -> ConfigService:
    """Create a ConfigService backed by real SQLite at server_dir."""
    mgr = ServerConfigManager(server_dir_path=str(server_dir))
    return ConfigService(config_manager=mgr)


def _seed_and_read(svc: ConfigService, repo_path: Path) -> Dict[str, Any]:
    """Wire svc as singleton, seed config.json, return parsed content."""
    set_config_service(svc)
    config_seeding.seed_provider_config(str(repo_path))
    raw: Dict[str, Any] = json.loads(
        (repo_path / ".code-indexer" / "config.json").read_text()
    )
    return raw


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIntegrationParallelRequestsSeed:
    """Full pipeline: config_service save -> seed_provider_config -> config.json."""

    @pytest.fixture(autouse=True)
    def reset_singleton(self):
        """Ensure the config_service singleton is clean before and after each test."""
        reset_config_service()
        yield
        reset_config_service()

    @pytest.fixture()
    def seeding_env(self, tmp_path: Path):
        """Provide (svc, repo_path) with a pre-built ConfigService and temp repo."""
        server_dir = tmp_path / "server"
        server_dir.mkdir()
        repo_path = _make_cidx_config(tmp_path / "repo", _DEFAULT_PROVIDER_CONFIG)
        svc = _make_service(server_dir)
        return svc, repo_path

    def test_all_three_fields_seeded_correctly(self, seeding_env) -> None:
        """Save all 3 new fields to config_service, seed to temp repo, verify config.json."""
        svc, repo_path = seeding_env
        svc._update_indexing_setting("voyage_ai_parallel_requests", "16")
        svc._update_indexing_setting("cohere_parallel_requests", "12")
        svc._update_indexing_setting("temporal_parallel_requests", "4")

        written = _seed_and_read(svc, repo_path)

        assert written["voyage_ai"]["parallel_requests"] == 16
        assert written["cohere"]["parallel_requests"] == 12
        assert written["voyage_ai"]["temporal_parallel_requests"] == 4
        assert written["cohere"]["temporal_parallel_requests"] == 4

    def test_temporal_none_writes_null_not_absent(self, seeding_env) -> None:
        """Scenario 8 regression guard: temporal_parallel_requests=None -> JSON null present."""
        svc, repo_path = seeding_env
        # Empty string clears temporal to None
        svc._update_indexing_setting("temporal_parallel_requests", "")

        cfg = svc.get_config()
        assert cfg.indexing_config.temporal_parallel_requests is None

        written = _seed_and_read(svc, repo_path)

        # Key MUST be present with null value, not absent
        voyage_ai_section = written.get("voyage_ai", {})
        assert "temporal_parallel_requests" in voyage_ai_section, (
            "temporal_parallel_requests key must be present in voyage_ai even when null"
        )
        assert voyage_ai_section["temporal_parallel_requests"] is None

        cohere_section = written.get("cohere", {})
        assert "temporal_parallel_requests" in cohere_section, (
            "temporal_parallel_requests key must be present in cohere even when null"
        )
        assert cohere_section["temporal_parallel_requests"] is None

    def test_temporal_set_then_cleared_writes_null(self, seeding_env) -> None:
        """Set temporal to 4, then clear it -- re-seed must write null."""
        svc, repo_path = seeding_env
        svc._update_indexing_setting("temporal_parallel_requests", "4")
        svc._update_indexing_setting("temporal_parallel_requests", "")

        written = _seed_and_read(svc, repo_path)

        assert written["voyage_ai"]["temporal_parallel_requests"] is None
        assert written["cohere"]["temporal_parallel_requests"] is None

    def test_indexing_config_fields_persisted_in_sqlite(self, tmp_path: Path) -> None:
        """After save, a fresh ConfigService reading the same db sees the new fields."""
        server_dir = tmp_path / "server"
        server_dir.mkdir()

        svc1 = _make_service(server_dir)
        svc1._update_indexing_setting("voyage_ai_parallel_requests", "20")
        svc1._update_indexing_setting("cohere_parallel_requests", "10")
        svc1._update_indexing_setting("temporal_parallel_requests", "6")

        # Fresh instance on same server_dir
        svc2 = _make_service(server_dir)
        cfg = svc2.get_config()

        assert cfg.indexing_config.voyage_ai_parallel_requests == 20
        assert cfg.indexing_config.cohere_parallel_requests == 10
        assert cfg.indexing_config.temporal_parallel_requests == 6
