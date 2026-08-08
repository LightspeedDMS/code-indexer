"""Real process/filesystem boundaries for temporal legacy migration."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from code_indexer.server.services.temporal_legacy_migration.verification import (
    verify_shard_copy,
)


def test_crash_restart_after_staging_before_publish(tmp_path: Path) -> None:
    legacy = tmp_path / "repo" / ".code-indexer" / "index"
    fixed = tmp_path / ".temporal" / "repo"
    shard = legacy / "code-indexer-temporal-voyage-2026Q1"
    shard.mkdir(parents=True)
    (shard / "vector_p1.json").write_text(
        json.dumps({"id": "p1", "vector": [1.0, 2.0], "payload": {"x": 1}})
    )
    marker = tmp_path / "pause.marker"
    child_code = (
        "from pathlib import Path; "
        "from code_indexer.server.services.temporal_legacy_migration.mover "
        "import migrate_temporal_shards; "
        f"migrate_temporal_shards(Path({str(legacy)!r}), Path({str(fixed)!r}), "
        "relocation_enabled=True)"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parents[3] / "src")
    env["CIDX_MIGRATION_PAUSE_BEFORE_TEMPORAL_PUBLISH"] = str(marker)
    child = subprocess.Popen([sys.executable, "-c", child_code], env=env)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.02)
    assert marker.exists(), "child never reached the pre-publish crash boundary"
    child.send_signal(signal.SIGKILL)
    assert child.wait(timeout=10) == -signal.SIGKILL

    from code_indexer.server.services.temporal_legacy_migration.mover import (
        migrate_temporal_shards,
    )

    result = migrate_temporal_shards(legacy, fixed, relocation_enabled=True)
    assert result.published == 1
    verify_shard_copy(shard, fixed / shard.name)
    resumed = migrate_temporal_shards(legacy, fixed, relocation_enabled=True)
    assert resumed.already_complete == 1
    assert len(list((fixed / shard.name).glob("vector_*.json"))) == 1


def test_concurrent_reader_observes_only_complete_source_or_published_target(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "legacy"
    fixed = tmp_path / "fixed"
    shard = legacy / "code-indexer-temporal-voyage-2026Q1"
    shard.mkdir(parents=True)
    record = {"id": "p1", "vector": [1.0], "payload": {"text": "complete"}}
    (shard / "vector_p1.json").write_text(json.dumps(record))
    observations: list[str] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            for root in (shard, fixed / shard.name):
                vector = root / "vector_p1.json"
                if vector.exists():
                    observations.append(vector.read_text())

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        from code_indexer.server.services.temporal_legacy_migration.mover import (
            migrate_temporal_shards,
        )

        result = migrate_temporal_shards(legacy, fixed, relocation_enabled=True)
    finally:
        stop.set()
        thread.join(timeout=10)
    assert result.published == 1
    assert observations
    assert all(json.loads(value) == record for value in observations)


def test_real_storage_query_round_trip_across_migration(tmp_path: Path) -> None:
    from code_indexer.server.services.temporal_legacy_migration.mover import (
        migrate_temporal_shards,
    )
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    legacy = tmp_path / "repo" / ".code-indexer" / "index"
    fixed = tmp_path / ".temporal" / "repo"
    collection = "code-indexer-temporal-voyage-2026Q1"
    store = FilesystemVectorStore(
        legacy, use_chunks_db_for_new_collections=False, project_root=tmp_path
    )
    store.create_collection(collection, vector_size=3)
    points = [
        {"id": "p1", "vector": [1.0, 0.0, 0.0], "payload": {"rank": 1}},
        {"id": "p2", "vector": [0.9, 0.1, 0.0], "payload": {"rank": 2}},
        {"id": "p3", "vector": [0.0, 1.0, 0.0], "payload": {"rank": 3}},
    ]
    store.begin_indexing(collection)
    store.upsert_points(collection, points)
    store.end_indexing(collection)

    class QueryEmbedder:
        def get_embedding(self, _query: str, **_kwargs: object) -> list[float]:
            return [1.0, 0.0, 0.0]

        def get_provider_name(self) -> str:
            return "real-test"

    before = store.search("query", QueryEmbedder(), collection, limit=3)
    result = migrate_temporal_shards(legacy, fixed, relocation_enabled=True)
    assert result.published == 1
    after_store = FilesystemVectorStore(
        fixed, use_chunks_db_for_new_collections=False, project_root=tmp_path
    )
    after = after_store.search("query", QueryEmbedder(), collection, limit=3)
    assert [row["id"] for row in before] == [row["id"] for row in after]
    assert [row["payload"] for row in before] == [row["payload"] for row in after]


def test_real_hnsw_binary_is_copied_and_queryable_after_migration(
    tmp_path: Path,
) -> None:
    import hnswlib

    from code_indexer.server.services.temporal_legacy_migration.mover import (
        migrate_temporal_shards,
    )
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    legacy = tmp_path / "repo" / ".code-indexer" / "index"
    fixed = tmp_path / ".temporal" / "repo"
    collection = "code-indexer-temporal-voyage-2026Q1"
    store = FilesystemVectorStore(
        legacy, use_chunks_db_for_new_collections=False, project_root=tmp_path
    )
    store.create_collection(collection, vector_size=3)
    store.begin_indexing(collection)
    store.upsert_points(
        collection,
        [
            {"id": "p1", "vector": [1.0, 0.0, 0.0], "payload": {}},
            {"id": "p2", "vector": [0.0, 1.0, 0.0], "payload": {}},
        ],
    )
    store.end_indexing(collection)
    source_bin = legacy / collection / "hnsw_index.bin"
    result = migrate_temporal_shards(legacy, fixed, relocation_enabled=True)
    assert result.published == 1
    target_bin = fixed / collection / "hnsw_index.bin"
    assert target_bin.read_bytes() == source_bin.read_bytes()
    index = hnswlib.Index(space="cosine", dim=3)
    index.load_index(str(target_bin), max_elements=100)
    labels, distances = index.knn_query([[1.0, 0.0, 0.0]], k=1)
    assert labels.tolist() == [[0]]
    assert distances[0][0] <= 0.01
