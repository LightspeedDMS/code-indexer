"""
Router-level regression test for GitHub Issue #1476 round-3 dual-review
remediation (Defect 1, HIGH).

GET /api/v1/repos/{alias}/index-status's transform_index_status mapped ANY
service status other than "not_indexed" to exists=True with zero diagnostic
signal in the response model. For a repo with a REAL HNSW index but a
corrupted collection_meta.json, the service layer (_get_semantic_status,
Issue #1476 round-2 remediation) correctly reports status="error", but the
REST response silently claimed {"exists": true, "document_count": 0, ...} --
a misleadingly successful response with zero diagnostic signal, defeating
the purpose of the service-layer fix for any REST consumer (the MCP handler
passes the status dict through unchanged and does not have this problem).

This test hits the ACTUAL route via TestClient. app.state.activated_repo_manager
is mocked ONLY as the path-resolution boundary (get_activated_repo_path ->
a real on-disk temp directory) -- the same established pattern already used
by test_indexing_reindex_response_1248.py and by
test_activated_repo_index_manager.py's own service-level fixtures. The real
system under test -- the unmocked ActivatedRepoIndexManager class, its real
_get_semantic_status logic, the real FilesystemVectorStore-written
collection files on disk, and the real router transform_index_status code --
is all exercised for real, proving the error signal survives all the way to
the REST response.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.routers.indexing import router
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore


def _build_real_semantic_collection(
    index_dir: Path,
    collection_name: str,
    payload_paths: List[str],
    vector_size: int = 32,
) -> None:
    """Build a REAL, loadable semantic collection using the actual
    production writer (FilesystemVectorStore), mirroring the helper in
    test_activated_repo_index_manager.py -- a genuinely real
    hnsw_index.bin + collection_meta.json, not a hand-crafted fixture.
    """
    index_dir.mkdir(parents=True, exist_ok=True)
    store = FilesystemVectorStore(index_dir, project_root=index_dir)
    store.create_collection(collection_name, vector_size=vector_size)

    points = [
        {
            "id": f"vec_{i}",
            "vector": np.random.randn(vector_size).tolist(),
            "payload": {"path": path},
        }
        for i, path in enumerate(payload_paths)
    ]
    store.begin_indexing(collection_name)
    store.upsert_points(collection_name, points)
    store.end_indexing(collection_name)


@pytest.fixture
def app_with_router():
    """Minimal FastAPI app exposing only the indexing router.

    Mirrors the pattern in test_indexing_reindex_response_1248.py: a bare
    app with just the router under test, avoiding the cost/fragility of
    booting the full server app for a router-construction-only test.
    """
    app = FastAPI()
    app.include_router(router)

    app.state.background_job_manager = MagicMock()
    app.state.activated_repo_manager = MagicMock()
    # No golden_repo_alias tracked -- the temporal-status resolver-fallback
    # path (_resolve_relocated_temporal_dir) must degrade gracefully, not
    # crash the whole endpoint.
    app.state.activated_repo_manager.get_repository.return_value = None

    test_user = User(
        username="alice",
        password_hash="hashed",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    app.dependency_overrides[get_current_user] = lambda: test_user
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def client(app_with_router):
    return TestClient(app_with_router, raise_server_exceptions=False)


class TestIndexStatusErrorSignal1476:
    """Round-3 dual-review Issue 1: the REST route must not silently
    swallow the service layer's explicit "error" status."""

    def test_index_status_route_surfaces_error_for_corrupt_metadata_with_real_hnsw(
        self, client, app_with_router, tmp_path
    ):
        """A repo with a REAL, present hnsw_index.bin but a corrupted
        collection_meta.json must NOT come back from the REST route as a
        misleadingly healthy {"exists": true, ...} -- the error signal
        must survive from the service layer to the HTTP response.
        """
        repo_path = tmp_path / "test-repo"
        index_dir = repo_path / ".code-indexer" / "index"
        _build_real_semantic_collection(index_dir, "voyage-code-3", ["a.py"])

        # Corrupt the metadata AFTER a real HNSW index was genuinely built.
        meta_file = index_dir / "voyage-code-3" / "collection_meta.json"
        meta_file.write_text("{not valid json!!!")

        app_with_router.state.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        response = client.get("/api/v1/repos/my-repo/index-status")

        assert response.status_code == 200, response.text
        body = response.json()
        semantic = body["semantic"]

        assert semantic.get("error") not in (None, ""), (
            "REST response must surface a diagnostic error signal when "
            f"the service reports status='error'. Got: {semantic}"
        )
        assert semantic["exists"] is False, (
            "A metadata-read failure on a repo whose real health could "
            "not be verified must not be silently reported as a healthy "
            f"exists=True index. Got: {semantic}"
        )

    def test_index_status_route_healthy_repo_has_no_error_field(
        self, client, app_with_router, tmp_path
    ):
        """Regression: a genuinely healthy, fully-indexed repo must still
        report exists=True with no error signal -- the fix must not
        regress the good path.
        """
        repo_path = tmp_path / "healthy-repo"
        index_dir = repo_path / ".code-indexer" / "index"
        _build_real_semantic_collection(index_dir, "voyage-code-3", ["a.py", "b.py"])

        app_with_router.state.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        response = client.get("/api/v1/repos/my-repo/index-status")

        assert response.status_code == 200, response.text
        body = response.json()
        semantic = body["semantic"]

        assert semantic["exists"] is True, semantic
        assert semantic.get("error") in (None, ""), semantic

    def test_index_status_route_no_500_for_list_metadata_not_object(
        self, client, app_with_router, tmp_path
    ):
        """Round-5 dual-review Issue 1: this is the EXACT original
        reproduction path -- collection_meta.json containing a JSON list
        instead of an object used to crash the route with an unhandled
        HTTP 500 (AttributeError: 'list' object has no attribute 'get'),
        since json.load() succeeds but the .get() call right after it
        does not. Must now return HTTP 200 with the diagnostic error
        signal, exactly like the malformed-JSON case.
        """
        repo_path = tmp_path / "list-metadata-repo"
        index_dir = repo_path / ".code-indexer" / "index"
        _build_real_semantic_collection(index_dir, "voyage-code-3", ["a.py"])

        meta_file = index_dir / "voyage-code-3" / "collection_meta.json"
        meta_file.write_text("[]")

        app_with_router.state.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        response = client.get("/api/v1/repos/my-repo/index-status")

        assert response.status_code == 200, (
            "A JSON list instead of a JSON object must never crash the "
            f"route with an unhandled 500. Got: {response.status_code} "
            f"{response.text}"
        )
        body = response.json()
        semantic = body["semantic"]

        assert semantic["exists"] is False, semantic
        assert semantic.get("error") not in (None, ""), semantic
