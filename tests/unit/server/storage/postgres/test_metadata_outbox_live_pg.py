"""Live-PostgreSQL tests for the control-plane metadata outbox (step 5a).

The cluster half of the design tests whose behaviour depends on PostgreSQL:

* design test 5 -- a transaction failed after the registry write leaves
  neither the change nor an outbox row (mutation: insert the row after
  commit);
* design test 7 -- two concurrent sweepers write each key once (mutation:
  drop the claim, i.e. FOR UPDATE SKIP LOCKED);
* the rows/abandon/state round trip.

Real migrations 054 + 055 in a private schema, a real psycopg pool, the real
sweeper. Skips without TEST_POSTGRES_DSN; guarded against non-disposable
databases (tests/utils/live_pg.py).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Dict, Iterator

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.control_plane_metadata import ControlPlaneMetadata
from code_indexer.server.services.metadata_outbox.sinks import LocalFilesSink
from code_indexer.server.services.metadata_outbox.sweeper import (
    MetadataOutboxSweeper,
    OutboxSettings,
)
from code_indexer.server.storage.protocols.control_plane_metadata_backend import (
    KIND_ALIAS,
    OutboxState,
)
from tests.utils.control_plane_metadata_fixtures import scoped_install
from tests.utils.live_pg import live_pg_dsn, migration_ddl, private_schema


@pytest.fixture
def backend() -> Iterator[object]:
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
    from code_indexer.server.storage.postgres.control_plane_metadata_backend import (
        ControlPlaneMetadataPostgresBackend,
    )

    dsn = live_pg_dsn()
    ddl = migration_ddl("054_control_plane_metadata.sql", "055_metadata_outbox.sql")
    with private_schema(dsn, "cidx_outbox_", ddl) as scoped:
        pool = ConnectionPool(scoped, min_size=0, max_size=6, name="test")
        try:
            yield ControlPlaneMetadataPostgresBackend(pool)
        finally:
            pool.close()


@pytest.fixture
def metadata(tmp_path: Path, backend) -> Iterator[ControlPlaneMetadata]:
    component = ControlPlaneMetadata(
        tmp_path / "golden-repos", backend=backend, cluster=True
    )
    component.import_files()
    with scoped_install(component):
        yield component


def _manager(metadata: ControlPlaneMetadata) -> AliasManager:
    return AliasManager(metadata.golden_repos_dir / "aliases")


def _sweeper(metadata, backend, sink) -> MetadataOutboxSweeper:
    return MetadataOutboxSweeper(
        metadata=metadata,
        backend=backend,
        settings=lambda: OutboxSettings(sink="local-files"),
        sink_factory=lambda name: sink,
    )


def test_5_failed_outbox_insert_rolls_back_the_registry_change(
    metadata, backend, monkeypatch
):
    from code_indexer.server.storage.postgres import (
        control_plane_metadata_backend as pg_module,
    )

    def broken_enqueue(conn, kind, name):
        raise RuntimeError("outbox insert failed")

    monkeypatch.setattr(pg_module, "_enqueue", broken_enqueue)

    with pytest.raises(RuntimeError):
        _manager(metadata).create_alias("example-repo-global", "/snapshots/v_1")

    assert backend.get(KIND_ALIAS, "example-repo-global") is None
    assert backend.outbox_depth() == 0


class _BlockingSink:
    name = "local-files"

    def __init__(self) -> None:
        self.inner = LocalFilesSink()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.writes: Dict[str, int] = {}
        self._lock = threading.Lock()

    def put(self, path: str, data: bytes) -> None:
        with self._lock:
            self.writes[path] = self.writes.get(path, 0) + 1
            first = not self.entered.is_set()
            self.entered.set()
        if first:
            assert self.release.wait(timeout=30)
        self.inner.put(path, data)

    def delete(self, path: str) -> None:
        self.inner.delete(path)


def test_7_two_concurrent_sweepers_write_each_key_once(metadata, backend):
    manager = _manager(metadata)
    for index in range(3):
        manager.create_alias(f"repo-{index}-global", f"/snapshots/{index}")
    sink = _BlockingSink()

    holder = threading.Thread(target=_sweeper(metadata, backend, sink).sweep_once)
    holder.start()
    assert sink.entered.wait(timeout=30)
    _sweeper(metadata, backend, sink).sweep_once()
    sink.release.set()
    holder.join(timeout=30)

    assert sorted(sink.writes.values()) == [1, 1, 1]
    assert backend.outbox_depth() == 0


def test_rows_abandon_and_state_round_trip(metadata, backend):
    manager = _manager(metadata)
    manager.create_alias("first-global", "/snapshots/1")
    manager.swap_alias("first-global", "/snapshots/2", "/snapshots/1")

    rows = backend.outbox_rows()
    assert [row.name for row in rows] == ["first-global", "first-global"]
    assert backend.delete_outbox_rows([row.id for row in rows]) == 2

    backend.save_outbox_state(OutboxState(stopped_at="t", stopped_reason="abandoned"))
    assert backend.get_outbox_state().stopped_reason == "abandoned"


def test_sweep_replays_current_value_and_empties_outbox(metadata, backend):
    manager = _manager(metadata)
    manager.create_alias("example-repo-global", "/snapshots/v_1")
    manager.swap_alias("example-repo-global", "/snapshots/v_2", "/snapshots/v_1")

    report = _sweeper(metadata, backend, LocalFilesSink()).sweep_once()

    assert report.replayed == [(KIND_ALIAS, "example-repo-global")]
    assert backend.outbox_depth() == 0
    assert (
        "/snapshots/v_2"
        in (
            metadata.golden_repos_dir / "aliases" / "example-repo-global.json"
        ).read_text()
    )
