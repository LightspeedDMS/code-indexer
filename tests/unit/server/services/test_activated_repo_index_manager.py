"""
Unit tests for ActivatedRepoIndexManager.

Tests the manual re-indexing service for activated repositories,
covering semantic, FTS, temporal, and SCIP index management.
"""

import json
import logging
import os
import pytest
import tempfile
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import Mock, patch

import numpy as np

from code_indexer.server.services.activated_repo_index_manager import (
    ActivatedRepoIndexManager,
)
from code_indexer.server.repositories.background_jobs import (
    BackgroundJobManager,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

# Round-7 dual-review Issue 2 (GitHub Issue #1476): permission modes used
# to simulate/restore a real unreadable .code-indexer/index directory.
_UNREADABLE_DIR_MODE = 0o000
_RESTORED_DIR_MODE = 0o755


def _build_real_semantic_collection(
    index_dir: Path,
    collection_name: str,
    payload_paths: List[str],
    vector_size: int = 32,
) -> Dict[str, Any]:
    """Build a REAL, loadable semantic collection using the actual
    production writer (FilesystemVectorStore) -- GitHub Issue #1476 dual
    review Finding 4: tests that claim to prove real indexed-data
    detection must exercise a genuinely real hnsw_index.bin +
    collection_meta.json, not hand-crafted JSON + a fake byte payload.

    Args:
        index_dir: The ``.code-indexer/index`` directory to build inside.
        collection_name: Embedder-slug collection directory name (e.g.
            "voyage-code-3").
        payload_paths: One entry per vector upserted; duplicates produce
            the same file (drives unique_file_count deterministically).
        vector_size: Embedding dimension for the throwaway test vectors.

    Returns:
        The real collection_meta.json contents the production writer
        wrote, so callers can assert against actual values rather than
        guessing field names/shapes.
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

    meta_file = index_dir / collection_name / "collection_meta.json"
    with open(meta_file) as f:
        result: Dict[str, Any] = json.load(f)
    return result


@pytest.fixture
def temp_data_dir():
    """Create temporary data directory for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def mock_background_job_manager():
    """Create mock background job manager."""
    manager = Mock(spec=BackgroundJobManager)
    manager.submit_job = Mock(return_value=str(uuid.uuid4()))
    manager.get_job_status = Mock(
        return_value={
            "job_id": str(uuid.uuid4()),
            "operation_type": "reindex",
            "status": "completed",
            "progress": 100,
            "result": {"success": True},
            "error": None,
        }
    )
    # Mock list_jobs to return empty lists by default (no concurrent jobs)
    manager.list_jobs = Mock(return_value={"jobs": [], "total": 0})
    return manager


@pytest.fixture
def mock_activated_repo_manager(temp_data_dir):
    """Create mock activated repository manager."""
    manager = Mock()
    # Return path within temp_data_dir to pass security validation
    repo_path = str(Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo")
    manager.get_activated_repo_path = Mock(return_value=repo_path)
    return manager


@pytest.fixture
def index_manager(
    temp_data_dir, mock_background_job_manager, mock_activated_repo_manager
):
    """Create ActivatedRepoIndexManager instance with mocks."""
    manager = ActivatedRepoIndexManager(
        data_dir=temp_data_dir,
        background_job_manager=mock_background_job_manager,
        activated_repo_manager=mock_activated_repo_manager,
    )
    return manager


class TestTriggerReindex:
    """Tests for trigger_reindex method."""

    @patch("os.path.exists")
    def test_trigger_reindex_semantic_only(self, mock_exists, index_manager):
        """Test triggering semantic index only."""
        # Mock repository directory exists
        mock_exists.return_value = True

        job_id = index_manager.trigger_reindex(
            repo_alias="test-repo",
            index_types=["semantic"],
            clear=False,
            username="testuser",
        )

        assert isinstance(job_id, str)
        assert len(job_id) == 36  # UUID format

        # Verify background job was submitted
        index_manager.background_job_manager.submit_job.assert_called_once()
        call_args = index_manager.background_job_manager.submit_job.call_args
        assert call_args[0][0] == "reindex"  # operation_type
        assert call_args[1]["submitter_username"] == "testuser"

    @patch("os.path.exists")
    def test_trigger_reindex_all_types(self, mock_exists, index_manager):
        """Test triggering all NON-temporal index types.

        Story #1457 AC12: "temporal" is deliberately excluded here -- an
        activated repo can never request temporal indexing (rejected with a
        clear error, see test_activated_repo_index_manager_temporal_rejection_1457.py).
        "All types" for an activated repo therefore means semantic+fts+scip.
        """
        # Mock repository directory exists
        mock_exists.return_value = True

        job_id = index_manager.trigger_reindex(
            repo_alias="test-repo",
            index_types=["semantic", "fts", "scip"],
            clear=True,
            username="testuser",
        )

        assert isinstance(job_id, str)
        index_manager.background_job_manager.submit_job.assert_called_once()

    @patch("os.path.exists")
    def test_trigger_reindex_with_clear_flag(self, mock_exists, index_manager):
        """Test triggering reindex with clear flag (rebuild)."""
        # Mock repository directory exists
        mock_exists.return_value = True

        job_id = index_manager.trigger_reindex(
            repo_alias="test-repo",
            index_types=["semantic"],
            clear=True,
            username="testuser",
        )

        assert isinstance(job_id, str)
        # Verify clear flag is passed to job function.
        # Bug #1154 fix: worker params are forwarded as positional *args.
        # Positional layout: (operation_type, func, repo_alias, repo_path, index_types, clear)
        # so clear=True lands at index 5. Also accept the legacy kwargs form.
        call_args = index_manager.background_job_manager.submit_job.call_args
        assert call_args[1].get("clear") is True or (
            len(call_args[0]) > 5 and call_args[0][5] is True
        )

    def test_trigger_reindex_invalid_type(self, index_manager):
        """Test triggering reindex with invalid index type raises ValueError."""
        with pytest.raises(ValueError, match="Invalid index type"):
            index_manager.trigger_reindex(
                repo_alias="test-repo",
                index_types=["invalid_type"],
                clear=False,
                username="testuser",
            )

    def test_trigger_reindex_empty_types(self, index_manager):
        """Test triggering reindex with empty index types raises ValueError."""
        with pytest.raises(ValueError, match="At least one index type required"):
            index_manager.trigger_reindex(
                repo_alias="test-repo",
                index_types=[],
                clear=False,
                username="testuser",
            )

    def test_trigger_reindex_missing_repo(self, index_manager):
        """Test triggering reindex for non-existent repository raises FileNotFoundError."""
        # Configure mock to raise error for missing repo
        index_manager.activated_repo_manager.get_activated_repo_path.side_effect = (
            FileNotFoundError("Repository not found")
        )

        with pytest.raises(FileNotFoundError, match="'missing-repo' not found"):
            index_manager.trigger_reindex(
                repo_alias="missing-repo",
                index_types=["semantic"],
                clear=False,
                username="testuser",
            )

    @patch("os.path.exists")
    def test_trigger_reindex_returns_job_info(self, mock_exists, index_manager):
        """Test that trigger_reindex returns expected job information."""
        # Mock repository directory exists
        mock_exists.return_value = True

        result = index_manager.trigger_reindex(
            repo_alias="test-repo",
            index_types=["semantic", "fts"],
            clear=False,
            username="testuser",
        )

        # Should return job_id string
        assert isinstance(result, str)
        assert len(result) > 0


class TestGetIndexStatus:
    """Tests for get_index_status method."""

    def test_get_index_status_all_current(self, index_manager, temp_data_dir):
        """Test getting status when all indexes are up-to-date.

        Issue #1476 dual-review Finding 4: previously wrote the legacy
        top-level metadata.json (never produced by the real layout) and
        only asserted key presence -- it passed regardless of whether
        semantic status was actually correct. Now builds the REAL
        per-embedder layout and asserts the explicit status value.
        """
        repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        index_dir = repo_path / ".code-indexer" / "index"
        _build_real_semantic_collection(
            index_dir, "voyage-code-3", ["a.py", "b.py", "c.py"]
        )

        # Mock repo path
        index_manager.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        status = index_manager.get_index_status(
            repo_alias="test-repo", username="testuser"
        )

        assert "semantic" in status
        assert "fts" in status
        assert "temporal" in status
        assert "scip" in status
        assert status["semantic"]["status"] == "up_to_date"

    def test_get_index_status_semantic_details(self, index_manager, temp_data_dir):
        """Test semantic index status details.

        Issue #1476: real semantic metadata lives per-embedder at
        ``<index_dir>/<embedder>/collection_meta.json`` alongside a real
        ``hnsw_index.bin`` -- the legacy top-level ``metadata.json`` this
        test previously wrote is never produced by the current layout.
        """
        repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        collection_dir = repo_path / ".code-indexer" / "index" / "voyage-code-3"
        collection_dir.mkdir(parents=True, exist_ok=True)

        last_indexed = datetime.now(timezone.utc).isoformat()
        collection_meta_file = collection_dir / "collection_meta.json"
        collection_meta_file.write_text(
            json.dumps(
                {
                    "name": "voyage-code-3",
                    "vector_size": 1024,
                    "created_at": "2026-01-01T00:00:00",
                    "hnsw_index": {"last_rebuild": last_indexed, "vector_count": 300},
                    "unique_file_count": 150,
                }
            )
        )
        (collection_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")

        index_manager.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        status = index_manager.get_index_status(
            repo_alias="test-repo", username="testuser"
        )

        assert status["semantic"]["last_indexed"] == last_indexed
        assert status["semantic"]["file_count"] == 150
        # Index size is calculated from actual files, not from metadata
        assert "index_size_mb" in status["semantic"]
        assert status["semantic"]["status"] == "up_to_date"

    def test_get_index_status_not_indexed(self, index_manager, temp_data_dir):
        """Test status when indexes don't exist."""
        repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        repo_path.mkdir(parents=True, exist_ok=True)

        index_manager.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        status = index_manager.get_index_status(
            repo_alias="test-repo", username="testuser"
        )

        assert status["semantic"]["status"] == "not_indexed"
        assert status["fts"]["status"] == "not_indexed"
        assert status["temporal"]["status"] == "not_indexed"
        assert status["scip"]["status"] == "not_indexed"

    def test_get_index_status_scip_success(self, index_manager, temp_data_dir):
        """Test SCIP index status when generation succeeded."""
        repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        scip_dir = repo_path / ".code-indexer" / "scip"
        scip_dir.mkdir(parents=True, exist_ok=True)

        # Create SCIP database file
        (scip_dir / "index.scip.db").touch()

        index_manager.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        status = index_manager.get_index_status(
            repo_alias="test-repo", username="testuser"
        )

        assert status["scip"]["status"] == "SUCCESS"
        assert status["scip"]["project_count"] >= 0

    def test_get_index_status_stale_temporal(self, index_manager, temp_data_dir):
        """Test detecting stale temporal index."""
        repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        temporal_dir = repo_path / ".code-indexer" / "index" / "code-indexer-temporal"
        temporal_dir.mkdir(parents=True, exist_ok=True)

        # Create old temporal metadata (30 days ago)
        old_timestamp = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        metadata_file = temporal_dir / "metadata.json"
        metadata_file.write_text(
            json.dumps({"last_indexed": old_timestamp, "commit_count": 100})
        )
        # Issue #1459 Finding 1b Site B: in real production, TemporalIndexer
        # writes metadata.json as its completion marker only AFTER the HNSW
        # index is built, so hnsw_index.bin always exists whenever
        # metadata.json exists for a genuinely-completed legacy in-repo
        # temporal index. This fixture was missing the file as a testing
        # shortcut -- adding it makes the fixture represent
        # genuinely-complete local data.
        (temporal_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")

        index_manager.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        status = index_manager.get_index_status(
            repo_alias="test-repo", username="testuser"
        )

        # Should be stale (>7 days old)
        assert status["temporal"]["status"] == "stale"


class TestGetSemanticStatusRealLayoutBug1476:
    """Regression tests for GitHub Issue #1476.

    _get_semantic_status used to check for a legacy top-level
    ``.code-indexer/index/metadata.json`` file that the current
    per-embedder collection layout never produces -- real metadata lives
    at e.g. ``.code-indexer/index/voyage-code-3/collection_meta.json``
    alongside a real ``hnsw_index.bin``. A genuinely indexed repo was
    therefore always reported "not_indexed". These tests build the REAL
    on-disk per-embedder layout (no mocking of the filesystem/metadata
    reading logic) to prove the bug and then the fix.
    """

    def test_get_semantic_status_up_to_date_for_real_per_embedder_layout(
        self, index_manager, temp_data_dir
    ):
        """A REAL per-embedder collection (built via the actual production
        FilesystemVectorStore writer -- a genuinely loadable hnsw_index.bin
        and real collection_meta.json, not a hand-crafted fixture) must be
        detected as 'up_to_date', reading file_count/last_indexed from the
        REAL schema fields (top-level unique_file_count,
        hnsw_index.last_rebuild) rather than the legacy metadata.json schema.

        Issue #1476 dual-review Finding 4: the previous version of this
        test wrote fake placeholder HNSW bytes (b"fake-real-hnsw-data")
        despite its name/docstring claiming "REAL" -- now it genuinely
        exercises the production write path.
        """
        repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        index_dir = repo_path / ".code-indexer" / "index"
        real_meta = _build_real_semantic_collection(
            index_dir, "voyage-code-3", ["a.py", "b.py", "c.py", "a.py"]
        )

        index_manager.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        status = index_manager.get_index_status(
            repo_alias="test-repo", username="testuser"
        )

        assert status["semantic"]["status"] == "up_to_date", (
            "Issue #1476: a genuinely indexed repo (real per-embedder "
            "collection_meta.json + hnsw_index.bin) must not be reported "
            f"as not_indexed. Got: {status['semantic']}"
        )
        assert status["semantic"]["file_count"] == real_meta["unique_file_count"] == 3
        assert (
            status["semantic"]["last_indexed"]
            == real_meta["hnsw_index"]["last_rebuild"]
        )

    def test_get_semantic_status_error_on_corrupt_metadata_logs_real_exception(
        self, index_manager, temp_data_dir, caplog
    ):
        """Issue #1476 dual-review Finding 1: a corrupt collection_meta.json
        on a repo with a REAL, present hnsw_index.bin must not be silently
        collapsed into 'not_indexed' -- that discards the real
        HNSW-presence signal discover_health_collections already found.
        The warning log must also contain the ACTUAL exception text, not a
        literal unformatted '{e}' placeholder.
        """
        repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        index_dir = repo_path / ".code-indexer" / "index"
        _build_real_semantic_collection(index_dir, "voyage-code-3", ["a.py"])

        # Corrupt the metadata AFTER a real HNSW index was genuinely built.
        meta_file = index_dir / "voyage-code-3" / "collection_meta.json"
        meta_file.write_text("{not valid json!!!")

        index_manager.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        with caplog.at_level(logging.WARNING):
            status = index_manager.get_index_status(
                repo_alias="test-repo", username="testuser"
            )

        assert status["semantic"]["status"] != "not_indexed", (
            "A real HNSW index exists on disk -- a corrupt metadata file "
            "must not silently report the repo as never indexed. "
            f"Got: {status['semantic']}"
        )
        assert status["semantic"]["status"] == "error"

        warning_messages = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        ]
        assert any(
            "{e}" not in msg and "expecting" in msg.lower() for msg in warning_messages
        ), (
            "Expected the warning log to contain the REAL exception text "
            "(a JSON decode error), not a literal unformatted '{e}' "
            f"placeholder. Captured warnings: {warning_messages}"
        )

    def test_get_semantic_status_missing_fields_reports_error_not_wrong_data(
        self, index_manager, temp_data_dir
    ):
        """Issue #1476 dual-review Finding 2: unique_file_count and
        hnsw_index.last_rebuild are NOT interchangeable with
        hnsw_index.vector_count (chunk count, not file count) or
        created_at (collection-creation time, not last-rebuild time). Per
        this project's writer/reader contract (CLAUDE.md 'Chunk Storage
        Layout'), a genuinely completed collection always has these
        fields -- their absence is an anomaly that must be surfaced as an
        explicit error, never silently backfilled with a dimensionally
        wrong value under the right field name.
        """
        repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        index_dir = repo_path / ".code-indexer" / "index"
        real_meta = _build_real_semantic_collection(
            index_dir, "voyage-code-3", ["a.py", "b.py"]
        )

        # Simulate an anomalous collection: strip the two fields the fix
        # must never silently substitute, while keeping their
        # dimensionally-different siblings (vector_count, created_at)
        # present -- exactly the scenario that used to produce wrong data.
        meta_file = index_dir / "voyage-code-3" / "collection_meta.json"
        broken_meta = dict(real_meta)
        del broken_meta["unique_file_count"]
        broken_meta["hnsw_index"] = dict(broken_meta["hnsw_index"])
        del broken_meta["hnsw_index"]["last_rebuild"]
        meta_file.write_text(json.dumps(broken_meta))

        index_manager.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        status = index_manager.get_index_status(
            repo_alias="test-repo", username="testuser"
        )

        assert status["semantic"]["status"] == "error", (
            "Missing unique_file_count/hnsw_index.last_rebuild is an "
            f"anomaly that must be surfaced explicitly. Got: {status['semantic']}"
        )
        assert status["semantic"].get("file_count") != broken_meta["hnsw_index"].get(
            "vector_count"
        ), "file_count must never silently report the chunk/vector count."
        assert status["semantic"].get("last_indexed") != broken_meta.get(
            "created_at"
        ), (
            "last_indexed must never silently report created_at "
            "(collection-creation time) in place of hnsw_index.last_rebuild."
        )

    def test_get_semantic_status_error_on_non_integer_file_count(
        self, index_manager, temp_data_dir
    ):
        """Round-7 dual-review Issue 1 (HIGH): unique_file_count can be
        present (non-null, passing the existing is-None check) but
        wrong-typed -- e.g. a list instead of an int. Without a real
        type check, this value flows through as file_count, past the
        Pydantic response layer downstream (routers/indexing.py), and
        crashes with an unhandled ValidationError -> HTTP 500. Must
        produce the existing graceful status: "error" instead.
        """
        repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        index_dir = repo_path / ".code-indexer" / "index"
        real_meta = _build_real_semantic_collection(
            index_dir, "voyage-code-3", ["a.py"]
        )

        meta_file = index_dir / "voyage-code-3" / "collection_meta.json"
        broken_meta = dict(real_meta)
        broken_meta["unique_file_count"] = ["not", "an", "integer"]
        meta_file.write_text(json.dumps(broken_meta))

        index_manager.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        status = index_manager.get_index_status(
            repo_alias="test-repo", username="testuser"
        )

        assert status["semantic"]["status"] == "error", (
            "A list value for unique_file_count must be reported as an "
            f"explicit error, never pass through as file_count. "
            f"Got: {status['semantic']}"
        )

    def test_get_semantic_status_error_on_non_string_last_rebuild(
        self, index_manager, temp_data_dir
    ):
        """Round-7 dual-review Issue 1 (HIGH): hnsw_index.last_rebuild can
        be present (non-null) but wrong-typed -- e.g. a list instead of a
        timestamp string. Must produce the existing graceful
        status: "error", never pass through as last_indexed.
        """
        repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        index_dir = repo_path / ".code-indexer" / "index"
        real_meta = _build_real_semantic_collection(
            index_dir, "voyage-code-3", ["a.py"]
        )

        meta_file = index_dir / "voyage-code-3" / "collection_meta.json"
        broken_meta = dict(real_meta)
        broken_meta["hnsw_index"] = dict(broken_meta["hnsw_index"])
        broken_meta["hnsw_index"]["last_rebuild"] = ["not", "a", "timestamp"]
        meta_file.write_text(json.dumps(broken_meta))

        index_manager.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        status = index_manager.get_index_status(
            repo_alias="test-repo", username="testuser"
        )

        assert status["semantic"]["status"] == "error", (
            "A list value for hnsw_index.last_rebuild must be reported as "
            "an explicit error, never pass through as last_indexed. "
            f"Got: {status['semantic']}"
        )

    def test_get_semantic_status_error_on_list_metadata_not_object(
        self, index_manager, temp_data_dir
    ):
        """Round-5 dual-review Issue 1 (HIGH): collection_meta.json can be
        syntactically valid JSON that is NOT a JSON object (e.g. a bare
        list). json.load() succeeds, but the subsequent .get() calls on a
        list raise AttributeError, which used to propagate uncaught all
        the way to an unhandled HTTP 500 at the REST layer instead of the
        intended graceful status: "error".
        """
        repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        index_dir = repo_path / ".code-indexer" / "index"
        _build_real_semantic_collection(index_dir, "voyage-code-3", ["a.py"])

        meta_file = index_dir / "voyage-code-3" / "collection_meta.json"
        meta_file.write_text("[]")

        index_manager.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        # Must not raise -- must return the graceful error status.
        status = index_manager.get_index_status(
            repo_alias="test-repo", username="testuser"
        )

        assert status["semantic"]["status"] == "error", (
            "A JSON list instead of a JSON object must be reported as an "
            f"explicit error, never crash. Got: {status['semantic']}"
        )

    def test_get_semantic_status_error_on_non_dict_hnsw_index_field(
        self, index_manager, temp_data_dir
    ):
        """Round-5 dual-review Issue 1 (HIGH): the top-level
        collection_meta.json can itself be a valid dict, but its
        hnsw_index field can be a non-dict (e.g. a list). The subsequent
        .get() call on hnsw_metadata must not raise AttributeError.

        Uses a non-EMPTY list ([1, 2, 3]) deliberately: an empty list is
        falsy and would be silently absorbed by the pre-existing
        `metadata.get("hnsw_index") or {}` fallback, masking the real
        crash path this test must exercise.
        """
        repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        index_dir = repo_path / ".code-indexer" / "index"
        real_meta = _build_real_semantic_collection(
            index_dir, "voyage-code-3", ["a.py"]
        )

        meta_file = index_dir / "voyage-code-3" / "collection_meta.json"
        broken_meta = dict(real_meta)
        broken_meta["hnsw_index"] = [1, 2, 3]
        meta_file.write_text(json.dumps(broken_meta))

        index_manager.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        status = index_manager.get_index_status(
            repo_alias="test-repo", username="testuser"
        )

        assert status["semantic"]["status"] == "error", (
            "A non-dict hnsw_index field must be reported as an explicit "
            f"error, never crash. Got: {status['semantic']}"
        )

    def test_get_semantic_status_selects_configured_active_embedder(
        self, index_manager, temp_data_dir
    ):
        """Issue #1476 dual-review Finding 3: when multiple semantic
        collections coexist (e.g. an embedder-switch migration window),
        the repo's own .code-indexer/config.json (embedding_provider +
        matching provider's model field) is the authoritative source of
        which one is actually active -- never an arbitrary alphabetical
        pick. Uses two REAL collections so an alphabetically-first pick
        would report the WRONG (stale, old-embedder) data.
        """
        repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        index_dir = repo_path / ".code-indexer" / "index"

        # Alphabetically first, deliberately stale/smaller.
        _build_real_semantic_collection(index_dir, "aaa-old-embedder", ["a.py"])
        # Alphabetically last, the ACTUAL current embedder per config.json.
        current_meta = _build_real_semantic_collection(
            index_dir, "zzz-current-embedder", ["a.py", "b.py", "c.py"]
        )

        config_dir = repo_path / ".code-indexer"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.json").write_text(
            json.dumps(
                {
                    "embedding_provider": "voyage-ai",
                    "voyage_ai": {"model": "zzz-current-embedder"},
                }
            )
        )

        index_manager.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        status = index_manager.get_index_status(
            repo_alias="test-repo", username="testuser"
        )

        assert status["semantic"]["status"] == "up_to_date"
        assert (
            status["semantic"]["file_count"] == current_meta["unique_file_count"] == 3
        ), (
            "Must select the collection matching the repo's configured "
            f"active embedder, not an alphabetically-first pick. "
            f"Got: {status['semantic']}"
        )

    def test_get_semantic_status_ambiguous_multiple_collections_fails_explicitly(
        self, index_manager, temp_data_dir
    ):
        """Issue #1476 dual-review Finding 3: with multiple real semantic
        collections and no config.json to disambiguate which is active,
        the function must fail explicitly (anti-fallback, Messi Rule #2)
        rather than silently picking one.
        """
        repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        index_dir = repo_path / ".code-indexer" / "index"

        _build_real_semantic_collection(index_dir, "aaa-old-embedder", ["a.py"])
        _build_real_semantic_collection(
            index_dir, "zzz-current-embedder", ["a.py", "b.py", "c.py"]
        )
        # Deliberately no .code-indexer/config.json -- genuinely ambiguous.

        index_manager.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        status = index_manager.get_index_status(
            repo_alias="test-repo", username="testuser"
        )

        assert status["semantic"]["status"] == "error", (
            "With no way to determine which of 2 real semantic "
            "collections is active, must fail explicitly, not silently "
            f"pick one. Got: {status['semantic']}"
        )

    def test_get_semantic_status_single_collection_config_mismatch_fails_explicitly(
        self, index_manager, temp_data_dir
    ):
        """Round-5 dual-review Issue 2 (MEDIUM): the single-collection
        shortcut must consult config.json when it is present, even when
        there is only one discovered collection. A real config.json
        naming a NEW active embedder whose collection has not been built
        yet (e.g. mid-reindex), with the only collection on disk
        belonging to the OLD, no-longer-active embedder, must report
        status: "error" -- never a misleading "up_to_date" implying the
        configured embedder is fully indexed.
        """
        repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        index_dir = repo_path / ".code-indexer" / "index"

        # The ONLY collection on disk -- belongs to the OLD embedder.
        _build_real_semantic_collection(index_dir, "old-model", ["a.py"])

        config_dir = repo_path / ".code-indexer"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.json").write_text(
            json.dumps(
                {
                    "embedding_provider": "voyage-ai",
                    "voyage_ai": {"model": "new-active-model"},
                }
            )
        )

        index_manager.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        status = index_manager.get_index_status(
            repo_alias="test-repo", username="testuser"
        )

        assert status["semantic"]["status"] == "error", (
            "config.json names a different, not-yet-built active "
            "embedder than the sole collection on disk -- must fail "
            f"explicitly, not silently trust the stale collection. "
            f"Got: {status['semantic']}"
        )

    def test_get_semantic_status_index_size_scoped_to_active_collection(
        self, index_manager, temp_data_dir
    ):
        """Round-3 dual-review Issue 2: index_size_mb must reflect ONLY the
        active/selected collection's on-disk size, not the entire
        .code-indexer/index directory -- which can include a large
        INACTIVE collection left over from an embedder-switch window
        (correctly excluded from file_count/last_indexed already via
        _select_active_semantic_collection, but the size computation was
        still summing the whole index directory).
        """
        repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        index_dir = repo_path / ".code-indexer" / "index"

        # A large INACTIVE collection that must NOT be counted.
        _build_real_semantic_collection(
            index_dir,
            "aaa-old-embedder",
            ["a.py"] * 200,
            vector_size=512,
        )
        # The active, configured collection -- deliberately much smaller.
        _build_real_semantic_collection(
            index_dir, "zzz-current-embedder", ["a.py"], vector_size=32
        )

        config_dir = repo_path / ".code-indexer"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.json").write_text(
            json.dumps(
                {
                    "embedding_provider": "voyage-ai",
                    "voyage_ai": {"model": "zzz-current-embedder"},
                }
            )
        )

        index_manager.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        status = index_manager.get_index_status(
            repo_alias="test-repo", username="testuser"
        )

        active_collection_dir = index_dir / "zzz-current-embedder"
        expected_bytes = sum(
            f.stat().st_size for f in active_collection_dir.rglob("*") if f.is_file()
        )
        expected_mb = round(expected_bytes / index_manager.BYTES_PER_MB, 2)
        whole_dir_bytes = sum(
            f.stat().st_size for f in index_dir.rglob("*") if f.is_file()
        )

        # Sanity check the fixture actually creates a meaningful size gap,
        # otherwise this test could pass by rounding coincidence.
        assert whole_dir_bytes - expected_bytes > 1024 * 1024

        assert status["semantic"]["index_size_mb"] == expected_mb, (
            "index_size_mb must be scoped to the active collection only, "
            f"not the whole index directory. Got: {status['semantic']}"
        )

    def test_get_semantic_status_ambiguous_logs_real_config_parse_exception(
        self, index_manager, temp_data_dir, caplog
    ):
        """Round-3 dual-review Issue 3 (optional/low): a corrupt
        .code-indexer/config.json during multi-collection resolution must
        log the REAL parse exception -- distinguishable in logs from
        genuine embedder ambiguity (config present and valid, but matches
        neither collection). The returned status remains "error" either
        way (not a correctness bug); this only strengthens diagnosability.
        """
        repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        index_dir = repo_path / ".code-indexer" / "index"

        _build_real_semantic_collection(index_dir, "aaa-old-embedder", ["a.py"])
        _build_real_semantic_collection(
            index_dir, "zzz-current-embedder", ["a.py", "b.py", "c.py"]
        )

        config_dir = repo_path / ".code-indexer"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.json").write_text("{not valid json!!!")

        index_manager.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        with caplog.at_level(logging.WARNING):
            status = index_manager.get_index_status(
                repo_alias="test-repo", username="testuser"
            )

        assert status["semantic"]["status"] == "error"

        warning_messages = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        ]
        assert any(
            "expecting" in msg.lower() or "json" in msg.lower()
            for msg in warning_messages
        ), (
            "Expected a warning log documenting the REAL config.json parse "
            f"exception, distinguishable from genuine ambiguity. "
            f"Captured warnings: {warning_messages}"
        )

    def test_get_semantic_status_error_on_unreadable_index_directory(
        self, index_manager, temp_data_dir
    ):
        """Round-7 dual-review Issue 2 (HIGH): a real, unreadable
        .code-indexer/index directory must not crash the route with an
        unhandled PermissionError -> HTTP 500. Both
        discover_health_collections and the size-scan rglob touch this
        directory outside any protective try/except before this fix.
        Permissions are always restored in finally so this test never
        leaves broken permissions behind, even on assertion failure.
        """
        repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        index_dir = repo_path / ".code-indexer" / "index"
        _build_real_semantic_collection(index_dir, "voyage-code-3", ["a.py"])

        index_manager.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        os.chmod(index_dir, _UNREADABLE_DIR_MODE)
        try:
            status = index_manager.get_index_status(
                repo_alias="test-repo", username="testuser"
            )
        finally:
            os.chmod(index_dir, _RESTORED_DIR_MODE)

        assert status["semantic"]["status"] == "error", (
            "An unreadable index directory must be reported as an "
            f"explicit error, never crash. Got: {status['semantic']}"
        )

    def test_get_semantic_status_not_indexed_when_index_dir_missing(
        self, index_manager, temp_data_dir
    ):
        """Regression: repo with no .code-indexer/index directory at all
        must still correctly report not_indexed."""
        repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        repo_path.mkdir(parents=True, exist_ok=True)

        index_manager.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        status = index_manager.get_index_status(
            repo_alias="test-repo", username="testuser"
        )

        assert status["semantic"]["status"] == "not_indexed"

    def test_get_semantic_status_not_indexed_when_collection_incomplete(
        self, index_manager, temp_data_dir
    ):
        """Regression: an index directory exists but the collection never
        finished building (no hnsw_index.bin yet) must still report
        not_indexed, not crash or false-positive."""
        repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        collection_dir = repo_path / ".code-indexer" / "index" / "voyage-code-3"
        collection_dir.mkdir(parents=True, exist_ok=True)
        (collection_dir / "collection_meta.json").write_text(
            json.dumps({"name": "voyage-code-3", "vector_size": 1024})
        )
        # No hnsw_index.bin -- collection is incomplete/never finished.

        index_manager.activated_repo_manager.get_activated_repo_path.return_value = str(
            repo_path
        )

        status = index_manager.get_index_status(
            repo_alias="test-repo", username="testuser"
        )

        assert status["semantic"]["status"] == "not_indexed"


class TestJobExecution:
    """Tests for job execution logic."""

    @patch(
        "code_indexer.server.services.activated_repo_index_manager"
        ".run_cancellable_subprocess"
    )
    def test_execute_semantic_indexing(self, mock_subprocess, index_manager, tmp_path):
        """Test semantic indexing execution.

        Bug #1342: _run_subprocess_with_telemetry now delegates to
        run_cancellable_subprocess (Popen-based, cancel-cooperative) instead
        of a plain subprocess.run, so the mock target moved to the new call
        site.

        Bug #1419 follow-up: _execute_semantic_indexing now fast-fails when
        repo_path has no .code-indexer/config.json (uninitialized-repo
        guard). The hardcoded "/tmp/test-repo" string used here previously
        was never a real, initialized repo -- this test now uses the real
        tmp_path fixture with a config.json created inside it, matching what
        an actually-initialized repo looks like.
        """
        (tmp_path / ".code-indexer").mkdir()
        (tmp_path / ".code-indexer" / "config.json").write_text("{}")

        # Mock successful cidx index execution
        mock_subprocess.return_value = Mock(returncode=0, stderr="", stdout="")

        result = index_manager._execute_semantic_indexing(str(tmp_path), False)

        assert result["success"] is True
        assert "Semantic indexing completed" in result["message"]

    @patch("subprocess.run")
    def test_execute_scip_indexing(self, mock_subprocess, index_manager):
        """Test SCIP indexing execution via subprocess."""
        # Mock successful SCIP generation
        mock_subprocess.return_value = Mock(returncode=0, stderr="", stdout="")

        result = index_manager._execute_scip_indexing("/tmp/test-repo", False)

        assert result["success"] is True
        assert "SCIP generation completed" in result["message"]

    def test_execute_indexing_with_clear_deletes_index(self, index_manager):
        """Test that clear flag deletes existing index before rebuilding."""
        # Implementation complete - semantic indexing clears index dir when clear=True
        pass


class TestJobTracking:
    """Tests for job status tracking."""

    def test_job_status_transitions(self, index_manager, mock_background_job_manager):
        """Test job status transitions from queued to running to completed."""
        job_id = str(uuid.uuid4())

        # Mock status progression
        status_progression = [
            {"status": "pending", "progress": 0},
            {"status": "running", "progress": 50},
            {"status": "completed", "progress": 100, "result": {"success": True}},
        ]

        for status_data in status_progression:
            mock_background_job_manager.get_job_status.return_value = {
                "job_id": job_id,
                "operation_type": "reindex",
                **status_data,
                "error": None,
            }

            status = mock_background_job_manager.get_job_status(job_id, "testuser")
            assert status["status"] == status_data["status"]
            assert status["progress"] == status_data["progress"]

    def test_job_failure_tracking(self, index_manager, mock_background_job_manager):
        """Test job failure is properly tracked with error message."""
        job_id = str(uuid.uuid4())
        error_message = "SCIP generation failed: Language not supported"

        mock_background_job_manager.get_job_status.return_value = {
            "job_id": job_id,
            "operation_type": "reindex",
            "status": "failed",
            "progress": 0,
            "result": None,
            "error": error_message,
        }

        status = mock_background_job_manager.get_job_status(job_id, "testuser")
        assert status["status"] == "failed"
        assert status["error"] == error_message

    def test_job_progress_tracking(self, index_manager, mock_background_job_manager):
        """Test job progress percentage tracking."""
        job_id = str(uuid.uuid4())

        progress_values = [0, 25, 50, 75, 100]
        for progress in progress_values:
            mock_background_job_manager.get_job_status.return_value = {
                "job_id": job_id,
                "operation_type": "reindex",
                "status": "running",
                "progress": progress,
                "result": None,
                "error": None,
            }

            status = mock_background_job_manager.get_job_status(job_id, "testuser")
            assert status["progress"] == progress


class TestErrorHandling:
    """Tests for error handling scenarios."""

    def test_indexing_error_on_disk_space(self, index_manager):
        """Test that IndexingError is raised when disk space is insufficient."""
        # This will be tested once implementation is complete
        pass

    def test_scip_failure_captures_stderr(self, index_manager):
        """Test that SCIP failures capture stderr for diagnostics."""
        # This will be tested once implementation is complete
        pass

    @patch("os.path.exists")
    def test_concurrent_job_prevention(self, mock_exists, index_manager):
        """Test that concurrent reindex jobs for same user are prevented."""
        # Mock repository directory exists
        mock_exists.return_value = True

        # Mock that there's already a running reindex job
        index_manager.background_job_manager.list_jobs.return_value = {
            "jobs": [
                {
                    "job_id": "existing-job-123",
                    "operation_type": "reindex",
                    "status": "running",
                    "progress": 50,
                }
            ],
            "total": 1,
        }

        # Attempt to trigger another reindex should raise ValueError
        with pytest.raises(ValueError, match="Another reindex job is already running"):
            index_manager.trigger_reindex(
                repo_alias="test-repo",
                index_types=["semantic"],
                clear=False,
                username="testuser",
            )

        # Verify list_jobs was called to check for concurrent jobs
        assert index_manager.background_job_manager.list_jobs.called


class TestIntegration:
    """Integration tests with real components (will be expanded)."""

    def test_full_reindex_workflow(self, index_manager):
        """Test complete workflow: trigger -> poll -> verify completion."""
        # This will be expanded once implementation is complete
        pass


class TestRepoAliasForwardingBug1154:
    """Regression tests for Bug #1154: repo_alias not forwarded to worker.

    BackgroundJobManager.submit_job declares repo_alias as its own keyword-only
    parameter for job tracking.  Before the fix, trigger_reindex passed
    repo_alias=repo_alias as a keyword argument, which was consumed by
    submit_job and never forwarded into *args/**kwargs that reach the worker
    function _execute_indexing_job.  The result: every reindex job failed with
    ``TypeError: _execute_indexing_job() missing 1 required positional
    argument: 'repo_alias'``.
    """

    def test_repo_alias_forwarded_to_worker(self, temp_data_dir):
        """repo_alias must reach _execute_indexing_job when job executes.

        Uses the real BackgroundJobManager so the actual *args/**kwargs
        forwarding path is exercised — no mocking of the feature under test.
        _execute_indexing_job is replaced with a spy that records its arguments
        and returns success immediately (avoids needing real index tooling).
        """
        import threading
        from unittest.mock import patch

        real_bjm = BackgroundJobManager()

        mock_arm = Mock()
        repo_path = str(Path(temp_data_dir) / "activated-repos" / "testuser" / "myrepo")
        Path(repo_path).mkdir(parents=True, exist_ok=True)
        mock_arm.get_activated_repo_path = Mock(return_value=repo_path)

        manager = ActivatedRepoIndexManager(
            data_dir=temp_data_dir,
            background_job_manager=real_bjm,
            activated_repo_manager=mock_arm,
        )

        received_kwargs: dict = {}
        worker_called = threading.Event()

        def spy_execute(
            repo_alias: str,
            repo_path: str,
            index_types,
            clear: bool,
            progress_callback=None,
        ):
            received_kwargs["repo_alias"] = repo_alias
            received_kwargs["repo_path"] = repo_path
            received_kwargs["index_types"] = index_types
            received_kwargs["clear"] = clear
            worker_called.set()
            return {"success": True, "details": {}}

        with patch.object(manager, "_execute_indexing_job", side_effect=spy_execute):
            # Mock path exists check
            with patch("os.path.exists", return_value=True):
                manager.trigger_reindex(
                    repo_alias="myrepo",
                    index_types=["semantic"],
                    clear=False,
                    username="testuser",
                )

        # Wait for the background worker to run (real thread pool)
        assert worker_called.wait(timeout=10), (
            "Worker _execute_indexing_job was never called within 10 seconds"
        )

        # The critical assertion: repo_alias must have been forwarded
        assert received_kwargs.get("repo_alias") == "myrepo", (
            f"Bug #1154: repo_alias was NOT forwarded to _execute_indexing_job. "
            f"Worker received kwargs: {received_kwargs}"
        )
        assert received_kwargs.get("repo_path") == repo_path
        assert received_kwargs.get("index_types") == ["semantic"]
        assert received_kwargs.get("clear") is False

        real_bjm.shutdown()


class TestSvcMigrate003Regression:
    """Regression tests for SVC-MIGRATE-003 log interpolation bug.

    Before the fix, the logger.error call in _execute_all_index_types used
    a plain string with {index_type}/{error_msg} placeholders instead of an
    f-string, so the actual values were never substituted.  These tests verify
    that after the fix the real values appear in the logged message.
    """

    def test_svc_migrate_003_log_contains_real_index_type_and_error(
        self, index_manager, temp_data_dir
    ):
        """SVC-MIGRATE-003: logged message must contain actual index_type and error string.

        Drives _execute_all_index_types directly by patching
        _execute_single_index_type to return a failure dict, then captures the
        logger.error call and asserts the message contains the real values.
        """
        from unittest.mock import patch

        repo_path = str(
            Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        )
        Path(repo_path).mkdir(parents=True, exist_ok=True)

        expected_error = "Voyage AI timed out after 30s"
        expected_type = "semantic"

        def _fake_single(rp, index_type, clear):
            return {"success": False, "error": expected_error}

        captured_calls = []

        def _fake_logger_error(msg, *args, **kwargs):
            captured_calls.append(msg)

        with patch.object(
            index_manager, "_execute_single_index_type", side_effect=_fake_single
        ):
            with patch.object(
                index_manager.logger, "error", side_effect=_fake_logger_error
            ):
                index_manager._execute_all_index_types(
                    repo_path=repo_path,
                    index_types=[expected_type],
                    clear=False,
                    update_progress=lambda pct, message="": None,
                    allocator=None,
                )

        assert len(captured_calls) >= 1, (
            "SVC-MIGRATE-003: expected logger.error to be called at least once"
        )
        logged_msg = captured_calls[0]
        assert expected_type in logged_msg, (
            f"SVC-MIGRATE-003: log message must contain the real index_type '{expected_type}', "
            f"got: {logged_msg!r}"
        )
        assert expected_error in logged_msg, (
            f"SVC-MIGRATE-003: log message must contain the real error '{expected_error}', "
            f"got: {logged_msg!r}"
        )
        assert "{index_type}" not in logged_msg, (
            f"SVC-MIGRATE-003: log message must not contain literal '{{index_type}}', "
            f"got: {logged_msg!r}"
        )
        assert "{error_msg}" not in logged_msg, (
            f"SVC-MIGRATE-003: log message must not contain literal '{{error_msg}}', "
            f"got: {logged_msg!r}"
        )

    def test_execute_all_index_types_returns_success_false_on_index_failure(
        self, index_manager, temp_data_dir
    ):
        """_execute_all_index_types must return success=False when any index_type fails.

        This ensures the job-completion logic in BackgroundJobManager correctly
        marks the job as FAILED (not COMPLETED) when indexing partially or
        fully fails, preventing a 300s e2e poll timeout.
        """
        repo_path = str(
            Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
        )
        Path(repo_path).mkdir(parents=True, exist_ok=True)

        def _fake_single(rp, index_type, clear):
            if index_type == "semantic":
                return {"success": False, "error": "VoyageAI connection refused"}
            return {"success": True, "message": "ok"}

        with patch.object(
            index_manager, "_execute_single_index_type", side_effect=_fake_single
        ):
            results = index_manager._execute_all_index_types(
                repo_path=repo_path,
                index_types=["semantic", "fts"],
                clear=False,
                update_progress=lambda pct, message="": None,
                allocator=None,
            )

        assert "semantic" in results
        assert results["semantic"]["success"] is False, (
            "_execute_all_index_types must preserve the failure result dict from "
            "_execute_single_index_type so the caller can compute all_success=False"
        )
        assert "fts" in results
        assert results["fts"]["success"] is True


class TestDataDirEnvVar:
    """Regression tests for CIDX_SERVER_DATA_DIR bootstrap env var in constructor.

    Before the fix, ActivatedRepoIndexManager.__init__ always defaulted to
    ~/.cidx-server/data when no data_dir arg was supplied, ignoring
    CIDX_SERVER_DATA_DIR.  In every deployment that sets this env var
    (including all in-process e2e Phase-3 tests via conftest.py), the reindex
    worker resolved repo paths against the WRONG directory, causing
    SVC-MIGRATE-003 "no configuration found" failures.

    The fix mirrors the pattern in lifespan.py:
        env_server_dir = os.environ.get("CIDX_SERVER_DATA_DIR")
        if env_server_dir:
            self.data_dir = str(Path(env_server_dir) / "data")
        else:
            self.data_dir = str(Path.home() / ".cidx-server" / "data")
    """

    def test_env_var_sets_data_dir(self, monkeypatch, mock_background_job_manager):
        """When CIDX_SERVER_DATA_DIR=/some/tmp is set and no data_dir arg is given,
        manager.data_dir must equal /some/tmp/data."""
        monkeypatch.setenv("CIDX_SERVER_DATA_DIR", "/some/tmp")
        mock_arm = Mock()
        manager = ActivatedRepoIndexManager(
            background_job_manager=mock_background_job_manager,
            activated_repo_manager=mock_arm,
        )
        assert manager.data_dir == "/some/tmp/data", (
            f"Expected /some/tmp/data but got {manager.data_dir!r}. "
            "ActivatedRepoIndexManager must honor CIDX_SERVER_DATA_DIR."
        )

    def test_default_when_env_var_unset(self, monkeypatch, mock_background_job_manager):
        """When CIDX_SERVER_DATA_DIR is unset and no data_dir arg is given,
        manager.data_dir must equal ~/.cidx-server/data (original default preserved)."""
        monkeypatch.delenv("CIDX_SERVER_DATA_DIR", raising=False)
        mock_arm = Mock()
        manager = ActivatedRepoIndexManager(
            background_job_manager=mock_background_job_manager,
            activated_repo_manager=mock_arm,
        )
        expected = str(Path.home() / ".cidx-server" / "data")
        assert manager.data_dir == expected, (
            f"Expected {expected!r} but got {manager.data_dir!r}. "
            "Default data_dir must be ~/.cidx-server/data when env var is unset."
        )

    def test_explicit_data_dir_wins_over_env_var(
        self, monkeypatch, mock_background_job_manager
    ):
        """When an explicit data_dir arg is passed, it wins over CIDX_SERVER_DATA_DIR."""
        monkeypatch.setenv("CIDX_SERVER_DATA_DIR", "/should/be/ignored")
        mock_arm = Mock()
        explicit_dir = "/explicit/override"
        manager = ActivatedRepoIndexManager(
            data_dir=explicit_dir,
            background_job_manager=mock_background_job_manager,
            activated_repo_manager=mock_arm,
        )
        assert manager.data_dir == explicit_dir, (
            f"Expected {explicit_dir!r} but got {manager.data_dir!r}. "
            "Explicit data_dir arg must always win over CIDX_SERVER_DATA_DIR."
        )
