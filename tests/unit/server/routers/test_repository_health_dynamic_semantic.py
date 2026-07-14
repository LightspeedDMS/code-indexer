"""
Unit tests for detect_semantic_index() in repository_health.py.

Tests that the helper correctly finds semantic indexes from any embedding provider
(not just voyage-code-3) and excludes multimodal/temporal/tantivy collections.

Also drives the real GET /api/repositories/{alias}/health endpoint against a
partially-built collection (EVO-64245): the scan itself now lives in
services/repository_health_aggregator.py, but the HTTP surface must still report
such a repository unhealthy.
"""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from code_indexer.server.routers.repository_health import detect_semantic_index


def _create_collection(index_dir: Path, name: str, with_hnsw: bool = True) -> Path:
    """Create a fake collection directory, optionally with hnsw_index.bin."""
    coll = index_dir / name
    coll.mkdir(parents=True, exist_ok=True)
    if with_hnsw:
        (coll / "hnsw_index.bin").write_bytes(b"")
    return coll


class TestDetectSemanticIndex:
    """Tests for detect_semantic_index() helper."""

    def test_detects_voyage_code_3_collection(self, tmp_path):
        """Detects voyage-code-3 hnsw_index.bin as semantic index."""
        index_dir = tmp_path / ".code-indexer" / "index"
        _create_collection(index_dir, "voyage-code-3")

        assert detect_semantic_index(index_dir) is True

    def test_detects_embed_v4_collection(self, tmp_path):
        """Detects embed-v4.0 hnsw_index.bin as semantic index (Cohere provider)."""
        index_dir = tmp_path / ".code-indexer" / "index"
        _create_collection(index_dir, "embed-v4.0")

        assert detect_semantic_index(index_dir) is True

    def test_excludes_embed_v4_multimodal_collection(self, tmp_path):
        """Excludes embed-v4.0-multimodal from semantic detection."""
        index_dir = tmp_path / ".code-indexer" / "index"
        _create_collection(index_dir, "embed-v4.0-multimodal")

        assert detect_semantic_index(index_dir) is False

    def test_excludes_voyage_multimodal_collection(self, tmp_path):
        """Excludes voyage-multimodal-3 from semantic detection."""
        index_dir = tmp_path / ".code-indexer" / "index"
        _create_collection(index_dir, "voyage-multimodal-3")

        assert detect_semantic_index(index_dir) is False

    def test_excludes_temporal_collection(self, tmp_path):
        """Excludes temporal collections from semantic detection."""
        index_dir = tmp_path / ".code-indexer" / "index"
        _create_collection(index_dir, "code-indexer-temporal")

        assert detect_semantic_index(index_dir) is False

    def test_excludes_tantivy_collection(self, tmp_path):
        """Excludes tantivy (and tantivy-v2 style names) from semantic detection."""
        index_dir = tmp_path / ".code-indexer" / "index"
        _create_collection(index_dir, "tantivy")
        _create_collection(index_dir, "tantivy-v2")

        assert detect_semantic_index(index_dir) is False

    def test_returns_false_when_no_hnsw_bin_present(self, tmp_path):
        """Returns False when collection dir exists but has no hnsw_index.bin."""
        index_dir = tmp_path / ".code-indexer" / "index"
        _create_collection(index_dir, "embed-v4.0", with_hnsw=False)

        assert detect_semantic_index(index_dir) is False

    def test_returns_false_when_index_dir_missing(self, tmp_path):
        """Returns False when .code-indexer/index doesn't exist."""
        missing_dir = tmp_path / ".code-indexer" / "index"

        assert detect_semantic_index(missing_dir) is False

    def test_detects_semantic_among_mixed_collections(self, tmp_path):
        """Detects semantic index even when multimodal and temporal also exist."""
        index_dir = tmp_path / ".code-indexer" / "index"

        # Add excluded collections
        for excluded in ["voyage-multimodal-3", "code-indexer-temporal", "tantivy"]:
            _create_collection(index_dir, excluded)

        # Add a valid semantic collection
        _create_collection(index_dir, "embed-v4.0")

        assert detect_semantic_index(index_dir) is True


def _create_shard(collection_dir: Path, name: str = "vector_0001.json") -> Path:
    """Write a vector shard file inside a collection directory."""
    collection_dir.mkdir(parents=True, exist_ok=True)
    shard = collection_dir / name
    shard.write_text('{"id": "1", "vector": [0.0]}')
    return shard


class TestRepositoryHealthEndpointIncompleteCollection:
    """EVO-64245 integration: drive the REAL get_repository_health() router loop
    (via TestClient) against a golden repo whose collection has vector shards but
    no hnsw_index.bin, and assert the endpoint reports it unhealthy. This
    exercises the actual scan loop, not a hand-copied reproduction of it."""

    def _make_golden_repo_manager(self, clone_path: Path):
        """Stub the golden-repo manager the router resolves off app.state."""
        manager = MagicMock()
        manager.get_golden_repo.return_value = MagicMock()  # truthy -> resolved
        manager.get_actual_repo_path.return_value = str(clone_path)
        return manager

    def _client(self, golden_repo_manager):
        from code_indexer.server.app import app
        from code_indexer.server.auth.dependencies import get_current_user_hybrid
        from code_indexer.server.auth.user_manager import User, UserRole

        user = User(
            username="tester",
            password_hash="x",
            role=UserRole.NORMAL_USER,
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        app.dependency_overrides[get_current_user_hybrid] = lambda: user
        prev = getattr(app.state, "golden_repo_manager", None)
        app.state.golden_repo_manager = golden_repo_manager
        return app, prev

    def test_partial_collection_endpoint_reports_unhealthy(self, tmp_path):
        """Shards written but hnsw_index.bin never renamed in -> endpoint 200
        with overall_healthy False and one unhealthy 'incomplete' collection."""
        from fastapi.testclient import TestClient
        from code_indexer.server.app import app
        from code_indexer.server.auth.dependencies import get_current_user_hybrid

        # Golden repo clone dir with a partially-built semantic collection.
        clone_path = tmp_path / "clone"
        coll = clone_path / ".code-indexer" / "index" / "voyage-code-3"
        _create_shard(coll)
        (coll / "collection_meta.json").write_text('{"vector_dim": 1536}')

        app_obj, prev = self._client(self._make_golden_repo_manager(clone_path))
        try:
            resp = TestClient(app_obj).get("/api/repositories/backend/health")

            assert resp.status_code == 200
            body = resp.json()
            assert body["overall_healthy"] is False
            assert body["total_collections"] == 1
            assert body["unhealthy_count"] == 1
            assert body["healthy_count"] == 0
            coll_result = body["collections"][0]
            assert coll_result["collection_name"] == "voyage-code-3"
            assert coll_result["valid"] is False
            assert coll_result["index_type"] == "semantic"
            assert any("HNSW graph missing" in e for e in coll_result["errors"])
        finally:
            app.dependency_overrides.pop(get_current_user_hybrid, None)
            app_obj.state.golden_repo_manager = prev

    def test_empty_collection_endpoint_reports_healthy(self, tmp_path):
        """A genuinely empty (never-indexed) collection must NOT be flagged:
        the endpoint reports healthy with zero collections recorded."""
        from fastapi.testclient import TestClient
        from code_indexer.server.app import app
        from code_indexer.server.auth.dependencies import get_current_user_hybrid

        clone_path = tmp_path / "clone"
        coll = clone_path / ".code-indexer" / "index" / "voyage-code-3"
        coll.mkdir(parents=True)  # empty: no shards, no hnsw

        app_obj, prev = self._client(self._make_golden_repo_manager(clone_path))
        try:
            resp = TestClient(app_obj).get("/api/repositories/backend/health")

            assert resp.status_code == 200
            body = resp.json()
            assert body["overall_healthy"] is True
            assert body["total_collections"] == 0
            assert body["unhealthy_count"] == 0
        finally:
            app.dependency_overrides.pop(get_current_user_hybrid, None)
            app_obj.state.golden_repo_manager = prev
