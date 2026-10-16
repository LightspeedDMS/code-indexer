"""Bug #2017: configuration saves made by different processes must never
revert each other (lost update).

Two independent ``ConfigService`` instances stand for two processes (two
SQLite workers on one node, or two cluster nodes) sharing ONE committed
runtime store.  Every change must apply its mutation to the LATEST
COMMITTED runtime configuration, never to the stale in-memory copy of the
process that serves the request.  Real SQLite and real PostgreSQL; no
mocks.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Iterator, List, Tuple

import pytest

from code_indexer.server.services import audit_capture
from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.services.siem_delivery import capture
from tests.unit.server.siem.backends import (  # noqa: F401  (fixtures)
    SiemBackendHarness,
    _pg_session_pool,
    pg_pool,
    siem_backend,
)

SIEM = "siem_delivery_config"
GOLDEN = "golden_repos_config"


def _cached_project(service: ConfigService) -> str:
    """The SIEM project id in *service*'s CACHED configuration."""
    section = service.get_config().siem_delivery_config
    assert section is not None
    return section.project_id


def _attach(service: ConfigService, b: SiemBackendHarness, db_path: Path) -> None:
    service.load_config()
    if b.name == "postgres":
        service.set_connection_pool(b.pool)
    else:
        service.initialize_runtime_db(str(db_path))


@pytest.fixture()
def two_processes(
    siem_backend: SiemBackendHarness,  # noqa: F811  (the imported fixture)
    tmp_path: Path,
) -> Iterator[Tuple[ConfigService, ConfigService]]:
    """Two services over ONE store: same node directory on SQLite (two
    workers), separate node directories on PostgreSQL (two nodes)."""
    from code_indexer.server.storage.database_manager import DatabaseSchema

    dir_a, dir_b = tmp_path / "node-a", tmp_path / "node-b"
    dir_a.mkdir()
    dir_b.mkdir()
    db_path = dir_a / "cidx_server.db"
    if siem_backend.name == "sqlite":
        DatabaseSchema(str(db_path)).initialize_database()
        dir_b = dir_a
    a = ConfigService(server_dir_path=str(dir_a))
    _attach(a, siem_backend, db_path)
    b = ConfigService(server_dir_path=str(dir_b))
    _attach(b, siem_backend, db_path)
    capture.reset_capture_state_for_tests()
    audit_capture.mark_server_process()
    audit_capture.bind_audit_service(siem_backend.audit, node_id=None)
    try:
        yield a, b
    finally:
        audit_capture.clear_audit_service()
        audit_capture.reset_server_process_mark()
        capture.reset_capture_state_for_tests()


def test_stale_process_save_keeps_the_other_process_section(
    two_processes: Tuple[ConfigService, ConfigService],
) -> None:
    a, b = two_processes
    b.get_config()  # B's cache now predates A's save
    a.update_settings_atomic([("siem_delivery", "project_id", "example-project")])
    assert _cached_project(b) == ""  # stale cache

    b.update_settings_atomic([("golden_repos", "refresh_interval_seconds", 7200)])

    _v, siem = a.read_committed_section(SIEM)
    _v, golden = a.read_committed_section(GOLDEN)
    assert siem["project_id"] == "example-project"  # X survived B's save
    assert golden["refresh_interval_seconds"] == 7200  # Y applied
    # B's own cache was refreshed from what it committed (X included)
    assert _cached_project(b) == "example-project"


def test_save_all_settings_keeps_the_other_process_section(
    two_processes: Tuple[ConfigService, ConfigService],
) -> None:
    a, b = two_processes
    b.get_config()
    a.update_settings_atomic([("siem_delivery", "project_id", "example-project")])
    b.save_all_settings({"server": {"log_level": "DEBUG"}})
    _v, siem = a.read_committed_section(SIEM)
    version, runtime = a._read_committed_runtime()
    assert siem["project_id"] == "example-project"
    assert runtime["log_level"] == "DEBUG"


def test_audited_before_is_the_committed_pre_image(
    two_processes: Tuple[ConfigService, ConfigService],
) -> None:
    a, b = two_processes
    b.get_config()
    a.update_settings_atomic([("siem_delivery", "project_id", "example-project")])
    seen: List[Any] = []

    def _mutate(candidate: Any) -> None:
        seen.append(candidate.siem_delivery_config.project_id)
        candidate.golden_repos_config.refresh_interval_seconds = 7200

    b.apply_audited_change(_mutate, actor="admin", target_id="golden_repos")
    assert seen == ["example-project"]  # mutated the committed configuration


def _commit(service: ConfigService, runtime: dict, expected: Any) -> Any:
    if service._pool is not None:
        return service._commit_runtime_row_pg(runtime, expected)
    return service._commit_runtime_row_sqlite(runtime, expected)


def test_commit_with_a_stale_version_is_refused_and_leaves_the_row(
    two_processes: Tuple[ConfigService, ConfigService],
) -> None:
    a, b = two_processes
    stale_version, runtime = a._read_committed_runtime()
    a.update_settings_atomic([("siem_delivery", "project_id", "example-project")])
    committed_version, _ = a._read_committed_runtime()
    assert committed_version == stale_version + 1
    assert a._db_config_version == committed_version

    stale = dict(runtime)
    stale.pop("launch_restart_generation", None)
    assert _commit(b, stale, stale_version) is None  # refused
    version, section = b.read_committed_section(SIEM)
    assert (version, section["project_id"]) == (committed_version, "example-project")

    new_version = _commit(b, stale, committed_version)  # current: accepted
    assert new_version == committed_version + 1 == b._db_config_version


def test_launch_restart_generation_survives_a_change(
    two_processes: Tuple[ConfigService, ConfigService],
) -> None:
    a, b = two_processes
    b.bump_launch_restart_generation()
    a.update_settings_atomic([("golden_repos", "refresh_interval_seconds", 7200)])
    assert a._read_raw_launch_generation() == 1


def test_change_that_always_loses_the_race_fails_loudly_and_publishes_nothing(
    two_processes: Tuple[ConfigService, ConfigService],
) -> None:
    from code_indexer.server.services.config_service import (
        _CHANGE_ATTEMPTS,
        ConfigChangeConflict,
    )

    a, b = two_processes
    competing: List[int] = []

    def _commit_elsewhere(_candidate: Any) -> None:  # before every publish
        competing.append(len(competing))
        b.update_settings_atomic(
            [("siem_delivery", "max_batch_events", 200 + len(competing))]
        )

    def _mutate(candidate: Any) -> None:
        candidate.siem_delivery_config.project_id = "example-lost"

    with pytest.raises(ConfigChangeConflict):
        a.apply_audited_change(
            _mutate,
            actor="admin",
            target_id="siem_delivery",
            before_publish=_commit_elsewhere,
        )
    assert len(competing) == _CHANGE_ATTEMPTS  # bounded
    _v, siem = a.read_committed_section(SIEM)
    assert siem["project_id"] == ""  # nothing published
    assert siem["max_batch_events"] == 200 + _CHANGE_ATTEMPTS  # theirs survived
    assert _cached_project(a) == ""


def _increment(service: ConfigService) -> Callable[[Any], None]:
    def _mutate(candidate: Any) -> None:
        candidate.siem_delivery_config.max_batch_events += 1

    return _mutate


def test_concurrent_saves_from_two_processes_lose_nothing(
    two_processes: Tuple[ConfigService, ConfigService],
) -> None:
    from code_indexer.server.services.config_service import _CHANGE_ATTEMPTS

    a, b = two_processes
    a.update_settings_atomic([("siem_delivery", "max_batch_events", 100)])
    # Bounded, never flaky: one save of a writer retries only when the OTHER
    # writer commits in between, and the other writer commits at most
    # `rounds` times in total -- fewer than the attempt budget.  Discriminating
    # all the same: a write of a process's stale copy loses deterministically
    # (each process would increment its own cached value).
    rounds = _CHANGE_ATTEMPTS - 1
    errors: List[BaseException] = []
    start = threading.Barrier(2)

    def _worker(service: ConfigService) -> None:
        try:
            start.wait(timeout=30)
            for _ in range(rounds):
                service.apply_audited_change(
                    _increment(service), actor="admin", target_id="siem_delivery"
                )
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(s,)) for s in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert not errors, errors
    _v, siem = a.read_committed_section(SIEM)
    assert siem["max_batch_events"] == 100 + 2 * rounds
