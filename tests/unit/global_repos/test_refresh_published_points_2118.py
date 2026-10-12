"""Story #2118 slice 1b: the volume alarm's reference size.

After a VERIFIED publish the refresh scheduler records the published
snapshot's point count (every CHUNKS_DB collection's ``chunks.db``, opened
read-only) as the golden repo's ``published_points``. A refresh that does
not publish records nothing (NULL stays NULL: the alarm's floor applies).

Real scheduler, snapshot and metadata stores (SQLite always, PostgreSQL
with ``TEST_POSTGRES_DSN``); only the embedding child (``_index_source``)
is replaced.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

import pytest

from code_indexer.server.services.index_spend_alarm import count_published_points
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from tests.utils.golden_repo_metadata_stores import (
    STORE_KINDS,
    golden_repo_metadata_store,
)
from tests.utils.refresh_fatal_store_harness import (
    ALIAS,
    REPO,
    Harness,
    build_harness,
)

POINTS_IN_FIXTURE = 40  # make_chunks_db_repo writes 40 points
DIM = 8


@pytest.fixture(params=STORE_KINDS)
def metadata(request, tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store(request.param, tmp_path) as backend:
        yield backend


def _collection(index_dir: Path, name: str, points: int) -> None:
    store = FilesystemVectorStore(
        base_path=index_dir,
        project_root=index_dir.parent.parent,
        use_chunks_db_for_new_collections=True,
    )
    assert store.create_collection(name, DIM)
    store.begin_indexing(name)
    store.upsert_points(
        name,
        [
            {
                "id": f"{name}-{i}",
                "vector": [0.1 * (i + 1)] * DIM,
                "payload": {"path": f"f{i}.py", "content": "x"},
            }
            for i in range(points)
        ],
    )
    store.end_indexing(name)


def test_counts_every_chunks_db_collection_of_a_snapshot(tmp_path: Path) -> None:
    snapshot = tmp_path / "snap"
    index_dir = snapshot / ".code-indexer" / "index"
    _collection(index_dir, "provider-a", 7)
    _collection(index_dir, "provider-b", 5)
    (index_dir / "tantivy_index").mkdir()  # not a collection

    assert count_published_points(snapshot) == 12


def test_snapshot_without_chunks_db_collection_has_no_count(tmp_path: Path) -> None:
    snapshot = tmp_path / "snap"
    (snapshot / ".code-indexer" / "index" / "tantivy_index").mkdir(parents=True)

    assert count_published_points(snapshot) is None


def _register(harness: Harness, metadata: Any) -> None:
    metadata.add_repo(
        alias=REPO,
        repo_url=f"local://{REPO}",
        default_branch="main",
        clone_path=str(harness.source),
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _refresh(harness: Harness) -> Any:
    with patch.object(harness.scheduler, "_index_source"):
        return harness.scheduler._execute_refresh(ALIAS)


def test_verified_publish_records_published_points(
    tmp_path: Path, metadata: Any
) -> None:
    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    _register(harness, metadata)
    # An alarming alias's system trigger was deferred; an admin refresh
    # runs anyway (the pending trigger makes the cycle publish).
    metadata.mark_volume_alarm_deferral(ALIAS, time.time() + 3600)

    result = _refresh(harness)

    assert result.get("message") == "Refresh complete", result
    target = harness.scheduler.alias_manager.read_alias(ALIAS)
    assert target is not None
    published = Path(target)
    assert published != harness.snapshot
    assert count_published_points(published) == POINTS_IN_FIXTURE
    assert metadata.get_published_points(REPO) == POINTS_IN_FIXTURE
    assert metadata.get_refresh_failure_backoff_state(ALIAS) is None


def test_refresh_without_publish_records_nothing(tmp_path: Path, metadata: Any) -> None:
    from tests.utils.fatal_chunk_store_fixtures import (
        diverge_store,
        make_journal_path_a_directory,
    )

    harness = build_harness(tmp_path, metadata, snapshot_mode="clean")
    _register(harness, metadata)
    # The publish gate cannot verify the store: the cycle stays unpublished.
    diverge_store(harness.source_db)
    make_journal_path_a_directory(harness.source_db)

    result = _refresh(harness)

    assert result["success"] is False, result
    assert harness.scheduler.alias_manager.read_alias(ALIAS) == str(harness.snapshot)
    assert metadata.get_published_points(REPO) is None
