"""Unit tests for Bug #1387: check_hnsw_health hardcodes a wrong collection path.

The handler used to hardcode ``.code-indexer/index/default/index.bin`` for
every repository. Real collections are named after the resolved embedding
model (e.g. ``voyage-code-3``) and the real HNSW binary filename is
``hnsw_index.bin`` (``HNSWIndexManager.INDEX_FILENAME``), not ``index.bin``.
As a result the handler false-negatived ("Index file not found") on every
real, healthy repository.

These tests use REAL on-disk HNSW collections -- a real, loadable hnswlib
index plus a real ``collection_meta.json`` sidecar, written under a
non-"default" provider-model directory name matching production layout.
Nothing here mocks the filesystem or the discovery walk
(``iter_index_files_for_repo``); only the DB-backed ``golden_repo_manager``
lookup is patched, exactly like the other tests in this module family.
"""

import json
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.services.hnsw_health_service import HNSWHealthService
from tests.utils.hnsw_orphan_corpus import build_hnsw_index

# Matches the calibrated healthy-index fixture already proven in
# tests/unit/services/test_hnsw_health_service_1359_orphan_count.py
# (200 vectors, dim=1024, seed=7, single-threaded build) -- reused verbatim
# so this new test does not need to re-derive orphan-free parameters.
_CORPUS_SIZE = 200
_CORPUS_DIM = 1024
_CORPUS_SEED = 7


def _make_real_collection(base: Path, *segments: str) -> Path:
    """Build a REAL, loadable HNSW index + collection_meta.json under
    base/segments -- the on-disk shape the query path actually produces
    (provider-model-named directory, never "default")."""
    coll = base.joinpath(*segments)
    coll.mkdir(parents=True, exist_ok=True)

    vectors = (
        np.random.RandomState(_CORPUS_SEED)
        .randn(_CORPUS_SIZE, _CORPUS_DIM)
        .astype(np.float32)
    )
    index = build_hnsw_index(vectors, num_threads=1)
    index_path = coll / "hnsw_index.bin"
    index.save_index(str(index_path))
    (coll / "collection_meta.json").write_text(json.dumps({"vector_dim": _CORPUS_DIM}))
    return coll


@pytest.fixture
def mock_regular_user():
    user = Mock(spec=User)
    user.username = "alice"
    user.role = UserRole.NORMAL_USER
    user.has_permission = Mock(return_value=True)
    return user


def _invoke_with_repo(params, user, clone_path: Path):
    """Call check_hnsw_health with golden_repo_manager patched to a repo
    rooted at clone_path, using a fresh (uncached) REAL HNSWHealthService --
    not a mock -- so the on-disk index is actually parsed/validated."""
    from code_indexer.server.mcp.handlers import check_hnsw_health

    mock_repo = Mock()
    mock_repo.clone_path = str(clone_path)

    real_service = HNSWHealthService(cache_ttl_seconds=300)

    with patch("code_indexer.server.app.golden_repo_manager") as mock_manager:
        mock_manager.get_golden_repo = Mock(return_value=mock_repo)
        with patch(
            "code_indexer.server.mcp.handlers._get_hnsw_health_service",
            return_value=real_service,
        ):
            return check_hnsw_health(params, user)


class TestCheckHnswHealthSingleRealCollection:
    """(a) A single real collection at a non-"default" name must be found
    and reported healthy, matching its real on-disk state."""

    def test_single_collection_at_non_default_name_is_found_and_reported(
        self, tmp_path, mock_regular_user
    ):
        clone_path = tmp_path / "repo"
        _make_real_collection(clone_path, ".code-indexer", "index", "voyage-code-3")

        params = {"repository_alias": "test-repo", "force_refresh": True}
        result = _invoke_with_repo(params, mock_regular_user, clone_path)

        response = json.loads(result["content"][0]["text"])
        assert response["success"] is True
        health = response["health"]
        # Under the old hardcoded "default"/"index.bin" bug this would be
        # file_exists=False with errors=["Index file not found"], even
        # though the collection genuinely exists and is healthy.
        assert health["file_exists"] is True
        assert health["valid"] is True
        assert health["errors"] == []
        assert "voyage-code-3" in health["index_path"]
        assert "collections" not in response


class TestCheckHnswHealthZeroCollections:
    """(b) Zero collections found must still return today's not-found
    behavior (same response shape, error communicated via the health
    payload, handler execution itself still succeeds)."""

    def test_zero_collections_found_reports_not_found_like_today(
        self, tmp_path, mock_regular_user
    ):
        clone_path = tmp_path / "repo-not-indexed"
        clone_path.mkdir()

        params = {"repository_alias": "test-repo", "force_refresh": True}
        result = _invoke_with_repo(params, mock_regular_user, clone_path)

        response = json.loads(result["content"][0]["text"])
        assert response["success"] is True
        health = response["health"]
        assert health["file_exists"] is False
        assert health["valid"] is False
        assert any("not found" in e.lower() for e in health["errors"])
        assert "collections" not in response


class TestCheckHnswHealthMultipleRealCollections:
    """(c) Multiple real collections (multi-provider / temporal shards) must
    ALL be reported -- none silently dropped."""

    def test_multiple_collections_are_all_reported_not_silently_dropped(
        self, tmp_path, mock_regular_user
    ):
        clone_path = tmp_path / "repo-multi"
        _make_real_collection(clone_path, ".code-indexer", "index", "voyage-code-3")
        _make_real_collection(clone_path, ".code-indexer", "index", "embed-v4.0")

        params = {"repository_alias": "test-repo", "force_refresh": True}
        result = _invoke_with_repo(params, mock_regular_user, clone_path)

        response = json.loads(result["content"][0]["text"])
        assert response["success"] is True
        # Backward-compat single "health" field stays populated (common-case
        # callers reading only "health" still see a real, healthy result).
        assert "health" in response
        assert response["health"]["file_exists"] is True

        collections = response["collections"]
        assert len(collections) == 2
        found_names = {Path(c["collection_path"]).parent.name for c in collections}
        assert found_names == {"voyage-code-3", "embed-v4.0"}
        for entry in collections:
            assert entry["health"]["file_exists"] is True
            assert entry["health"]["valid"] is True
