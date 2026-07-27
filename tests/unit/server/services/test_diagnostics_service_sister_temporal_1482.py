"""GitHub Issue #1482 (extension): DiagnosticsService.check_vector_storage()/
_validate_hnsw_indexes() must include temporal collections relocated to the
golden-owned sister location (Story #1457 AC1) -- it previously scanned
ONLY the registered repo's own `.code-indexer/index/` directory (or its
`.versioned/{alias}/v_*/` general-snapshot equivalent), which is a
DIFFERENT physical location than the temporal-specific sister root
(`.versioned/{alias}-temporal-{slug}-{quarter}/v_*/`). A relocated
temporal shard therefore silently vanished from vector-storage
diagnostics entirely.

Fix routes through the SAME resolver-aware helper Story #1457/#1459/the
repository_health_aggregator.py fix (Bug #1482 extension site 2) already
established (`discover_sister_temporal_collections`,
`TemporalShardResolver`) -- never a parallel sister-root scan.

Real infra throughout: real SQLite golden_repos_metadata table, real
AliasManager, a real loadable HNSW index (built with actual hnswlib) at
the sister location.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.services.diagnostics_service import (
    DiagnosticStatus,
    DiagnosticsService,
)

REPO_ALIAS = "myrepo"
POINTER_NAMESPACE = "myrepo-temporal-voyage_code_3-2024Q1"
PHYSICAL_NAME = "code-indexer-temporal-voyage_code_3-2024Q1"
FIXTURE_VERSION_TIMESTAMP = "1785164318"


def _create_valid_hnsw_index(
    collection_dir: Path, vector_count: int, vector_dim: int = 1024
) -> None:
    """Create a real, loadable HNSW index for testing (mirrors
    test_diagnostics_bug_172.py's established helper)."""
    try:
        import hnswlib
    except ImportError:
        pytest.skip("hnswlib not available")

    collection_dir.mkdir(parents=True, exist_ok=True)
    index = hnswlib.Index(space="cosine", dim=vector_dim)
    index.init_index(max_elements=vector_count, M=16, ef_construction=200)

    vectors = np.random.rand(vector_count, vector_dim).astype(np.float32)
    labels = np.arange(vector_count)
    index.add_items(vectors, labels)

    hnsw_file = collection_dir / "hnsw_index.bin"
    index.save_index(str(hnsw_file))

    metadata = {
        "name": collection_dir.name,
        "vector_size": vector_dim,
        "created_at": "2026-02-05T00:00:00.000000",
        "hnsw_index": {
            "version": 1,
            "vector_count": vector_count,
            "vector_dim": vector_dim,
            "M": 16,
            "ef_construction": 200,
            "space": "cosine",
            "last_rebuild": "2026-02-05T00:00:00.000000+00:00",
            "id_mapping": {str(i): f"id_{i}" for i in range(vector_count)},
        },
    }
    (collection_dir / "collection_meta.json").write_text(json.dumps(metadata, indent=2))


def _create_database_with_registered_repos(db_path: Path, repos: list) -> None:
    """Create SQLite database with registered golden repos (mirrors
    test_diagnostics_bug_172.py's established helper)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS golden_repos_metadata (
                alias TEXT PRIMARY KEY NOT NULL,
                repo_url TEXT NOT NULL,
                default_branch TEXT NOT NULL,
                clone_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                enable_temporal INTEGER NOT NULL DEFAULT 0,
                temporal_options TEXT
            )
            """
        )
        for alias, clone_path in repos:
            conn.execute(
                """
                INSERT INTO golden_repos_metadata
                (alias, repo_url, default_branch, clone_path, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    alias,
                    f"git@github.com:test/{alias}.git",
                    "main",
                    clone_path,
                    "2025-01-01T00:00:00Z",
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _build_sister_only_repo(tmp_path: Path):
    golden_repos_dir = tmp_path / "data" / "golden-repos"
    golden_repos_dir.mkdir(parents=True)

    repo_dir = golden_repos_dir / REPO_ALIAS
    local_index_dir = repo_dir / ".code-indexer" / "index"
    local_index_dir.mkdir(parents=True)

    version_dir = (
        golden_repos_dir
        / ".versioned"
        / POINTER_NAMESPACE
        / f"v_{FIXTURE_VERSION_TIMESTAMP}"
    )
    _create_valid_hnsw_index(version_dir, vector_count=10)

    alias_manager = AliasManager(str(golden_repos_dir / "aliases"))
    alias_manager.create_alias(POINTER_NAMESPACE, str(version_dir))

    db_path = tmp_path / "cidx_server.db"
    _create_database_with_registered_repos(db_path, [(REPO_ALIAS, str(repo_dir))])

    return golden_repos_dir, db_path


@pytest.mark.asyncio
async def test_check_vector_storage_includes_sister_relocated_temporal_collection(
    tmp_path,
):
    golden_repos_dir, db_path = _build_sister_only_repo(tmp_path)

    with patch(
        "code_indexer.server.services.config_service.get_config_service"
    ) as mock_get_config_svc:
        mock_config = Mock()
        mock_config.server_dir = str(tmp_path)
        mock_get_config_svc.return_value.get_config.return_value = mock_config

        service = DiagnosticsService(db_path=str(db_path))
        result = await service.check_vector_storage()

    assert PHYSICAL_NAME in result.details["index_types_found"], (
        "check_vector_storage must discover temporal collections relocated "
        f"to the sister location (Bug #1482 extension); got: "
        f"{result.details['index_types_found']!r}"
    )
    assert result.details["repos_with_healthy_indexes"] == 1
    assert result.status == DiagnosticStatus.WORKING
    assert len(result.details["repos_with_issues"]) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
