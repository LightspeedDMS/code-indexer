"""GitHub Issue #1459 AC4: ActivatedRepoIndexManager._get_temporal_status()
temporal-presence detection must route through the shared
TemporalShardResolver-based get_temporal_repo_status() helper as a
fallback when the local-clone scan finds nothing -- so an activated repo
whose backing golden repo's temporal data has relocated to Story #1457's
sister location is still reported as indexed, never "not_indexed".

Follows this test module's own established pattern (see
test_activated_repo_index_manager.py): a real ActivatedRepoIndexManager
instance with a lightweight Mock() activated_repo_manager double (never a
mock of AliasManager/TemporalShardResolver/the filesystem itself -- those
stay 100% real, Messi Rule #1).
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.services.activated_repo_index_manager import (
    ActivatedRepoIndexManager,
)


@pytest.fixture
def temp_data_dir():
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


def _write_committed_row(shard_dir: Path) -> None:
    nested = shard_dir / "a"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "vector_abc123.json").write_text('{"point_id": "p1"}')


def _make_manager(
    temp_data_dir: str, repo_path: Path, golden_repo_alias
) -> ActivatedRepoIndexManager:
    activated_repos_dir = Path(temp_data_dir) / "activated-repos"
    mock_activated_repo_manager = Mock()
    mock_activated_repo_manager.activated_repos_dir = str(activated_repos_dir)
    mock_activated_repo_manager.get_activated_repo_path = Mock(
        return_value=str(repo_path)
    )
    mock_activated_repo_manager.get_repository = Mock(
        return_value={"golden_repo_alias": golden_repo_alias}
    )
    return ActivatedRepoIndexManager(
        data_dir=temp_data_dir,
        background_job_manager=Mock(spec=BackgroundJobManager),
        activated_repo_manager=mock_activated_repo_manager,
    )


def test_local_clone_only_temporal_data_still_detected(temp_data_dir):
    """REGRESSION SAFETY: pre-relocation local-clone temporal data (with
    real metadata.json) is still correctly reported -- unaffected by the
    resolver fallback."""
    repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
    repo_path.mkdir(parents=True)
    temporal_dir = (
        repo_path / ".code-indexer" / "index" / "code-indexer-temporal-voyage_code_3"
    )
    temporal_dir.mkdir(parents=True)
    metadata = {
        "last_indexed": datetime.now(timezone.utc).isoformat(),
        "commit_count": 42,
        "date_range": {"start": "2024-01-01", "end": "2024-06-01"},
    }
    import json

    (temporal_dir / "metadata.json").write_text(json.dumps(metadata))
    # Issue #1459 Finding 1b Site B: in real production, TemporalIndexer
    # writes metadata.json as its completion marker only AFTER the HNSW
    # index is built, so hnsw_index.bin always exists whenever
    # metadata.json exists for a genuinely-completed legacy in-repo
    # temporal index. This fixture was missing the file as a testing
    # shortcut -- adding it makes the fixture represent genuinely-complete
    # local data, which is this test's actual regression-safety intent.
    (temporal_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")

    manager = _make_manager(
        temp_data_dir, repo_path, golden_repo_alias="backing-golden"
    )

    status = manager.get_index_status("test-repo", "testuser")

    assert status["temporal"]["status"] == "up_to_date"
    assert status["temporal"]["commit_count"] == 42


def test_local_dir_found_with_metadata_but_no_hnsw_index_falls_through_to_resolver(
    temp_data_dir,
):
    """Issue #1459 Finding 1b Site B: a locally-found temporal directory
    (matched by NAME only) with a real metadata.json but NO hnsw_index.bin
    must NOT be reported as a positive status ("up_to_date"/"stale") --
    it must fall through to the SAME resolver-based fallback that already
    handles "nothing found locally". With no sister pointer published for
    the backing golden repo, the fallback correctly reports "not_indexed"
    -- never a false-positive "up_to_date"."""
    repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
    repo_path.mkdir(parents=True)
    temporal_dir = (
        repo_path / ".code-indexer" / "index" / "code-indexer-temporal-voyage_code_3"
    )
    temporal_dir.mkdir(parents=True)
    metadata = {
        "last_indexed": datetime.now(timezone.utc).isoformat(),
        "commit_count": 42,
        "date_range": {"start": "2024-01-01", "end": "2024-06-01"},
    }
    import json

    (temporal_dir / "metadata.json").write_text(json.dumps(metadata))
    # Deliberately NO hnsw_index.bin -- the exact crash-window/incomplete
    # state Finding 1b guards against being reported as queryable.

    manager = _make_manager(
        temp_data_dir, repo_path, golden_repo_alias="backing-golden"
    )

    status = manager.get_index_status("test-repo", "testuser")

    assert status["temporal"]["status"] == "not_indexed"


def test_sister_relocated_temporal_data_is_detected_not_reported_missing(
    temp_data_dir,
):
    """THE ACTUAL BUG FIX: temporal data relocated to the sister location
    for the backing golden repo (real alias pointer, real hnsw_index.bin),
    ZERO local clone copy -- must NOT be reported "not_indexed"."""
    repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
    (repo_path / ".code-indexer" / "index").mkdir(parents=True)

    golden_repos_dir = Path(temp_data_dir) / "golden-repos"
    sister_version_dir = (
        golden_repos_dir
        / ".versioned"
        / "backing-golden-temporal-voyage_code_3-2024Q1"
        / "v_1700000000"
    )
    sister_version_dir.mkdir(parents=True)
    (sister_version_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")
    AliasManager(str(golden_repos_dir / "aliases")).create_alias(
        "backing-golden-temporal-voyage_code_3-2024Q1", str(sister_version_dir)
    )

    manager = _make_manager(
        temp_data_dir, repo_path, golden_repo_alias="backing-golden"
    )

    status = manager.get_index_status("test-repo", "testuser")

    assert status["temporal"]["status"] != "not_indexed"


def test_no_golden_repo_alias_falls_back_to_not_indexed(temp_data_dir):
    """When golden_repo_alias cannot be resolved (e.g. composite repo),
    behavior gracefully falls back to the pre-existing "not_indexed"
    result rather than raising."""
    repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
    (repo_path / ".code-indexer" / "index").mkdir(parents=True)

    manager = _make_manager(temp_data_dir, repo_path, golden_repo_alias=None)

    status = manager.get_index_status("test-repo", "testuser")

    assert status["temporal"]["status"] == "not_indexed"


def test_resolve_relocated_temporal_dir_get_repository_exception_returns_none(
    temp_data_dir,
):
    """Direct unit test: _resolve_relocated_temporal_dir's except-Exception
    branch (not reachable via the full get_index_status tests above, since
    those always succeed)."""
    repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
    manager = _make_manager(
        temp_data_dir, repo_path, golden_repo_alias="backing-golden"
    )
    manager.activated_repo_manager.get_repository = Mock(
        side_effect=RuntimeError("boom")
    )

    result = manager._resolve_relocated_temporal_dir(
        repo_path / ".code-indexer" / "index", "test-repo", "testuser"
    )

    assert result is None


def test_resolve_relocated_temporal_dir_falsy_metadata_returns_none(temp_data_dir):
    """Direct unit test: _resolve_relocated_temporal_dir's
    `if not metadata: return None` branch."""
    repo_path = Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo"
    manager = _make_manager(
        temp_data_dir, repo_path, golden_repo_alias="backing-golden"
    )
    manager.activated_repo_manager.get_repository = Mock(return_value=None)

    result = manager._resolve_relocated_temporal_dir(
        repo_path / ".code-indexer" / "index", "test-repo", "testuser"
    )

    assert result is None
