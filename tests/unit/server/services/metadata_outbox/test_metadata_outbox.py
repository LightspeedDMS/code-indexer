"""The control-plane metadata dual-write outbox (content seam step 5a).

The design's eleven tests, each named for what it proves and annotated with
the mutation it was checked against (docs/content-seam-remaining-consumers.md,
"Tests, each with its mutation"), plus the sink, gauge and rollback-window
behaviour around them.

Real SQLite registry backend, real filesystem, the real sweeper and real
AliasManager. The only fakes are the workspace-mcp content writer (its write
path does not exist yet -- these tests drive ``ContentWriterSink``, the
adapter that will carry it) and an injected clock. The PostgreSQL claim
(FOR UPDATE SKIP LOCKED) is exercised live in
tests/unit/server/storage/postgres/test_metadata_outbox_live_pg.py.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.control_plane_metadata import (
    KIND_ALIAS,
    ControlPlaneMetadata,
    MetadataFiles,
    MetadataSettings,
)
from code_indexer.server.services.metadata_outbox.sinks import (
    ContentWriterSink,
    LocalFilesSink,
    MetadataSink,
    UnavailableSink,
    build_sink,
)
from code_indexer.server.services.metadata_outbox.sweeper import (
    ABANDONED_KEY_LOG,
    UNDRAINED_KEY_LOG,
    MetadataOutboxSweeper,
    OutboxSettings,
)
from code_indexer.server.storage.sqlite_backends import (
    ControlPlaneMetadataSqliteBackend,
)
from code_indexer.server.storage.sqlite_backends import (
    control_plane_metadata_backend as sqlite_backend_module,
)
from tests.utils.control_plane_metadata_fixtures import (
    build_schema_template,
    scoped_install,
    sqlite_backend_from_template,
)

ALIAS = "example-repo-global"
WORKSPACE_MCP = "workspace-mcp"
LOCAL_FILES = "local-files"


class ContentWriterError(Exception):
    """Shape of the 0.6.0 content writer's error: a ``.code``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class FakeContentWriter:
    """Stand-in for workspace-mcp's content writer (put/delete by path)."""

    def __init__(self, failures: int = 0, fail_always: bool = False) -> None:
        self.failures = failures
        self.fail_always = fail_always
        self.files: Dict[str, bytes] = {}
        self.calls: List[Tuple[str, str]] = []

    def _maybe_fail(self) -> None:
        if self.fail_always or self.failures > 0:
            self.failures -= 1
            raise ContentWriterError("EUNAVAILABLE")

    def put(self, path: str, data: bytes) -> str:
        self.calls.append(("put", path))
        self._maybe_fail()
        self.files[path] = data
        return "blob-sha"

    def delete(self, path: str) -> None:
        self.calls.append(("delete", path))
        self._maybe_fail()
        self.files.pop(path, None)


class Switches:
    def __init__(self) -> None:
        self.read_source = "registry"
        self.file_dual_write = True
        self.sink = LOCAL_FILES
        self.drain_timeout_s = 600

    def metadata(self) -> MetadataSettings:
        return MetadataSettings(self.read_source, self.file_dual_write)

    def outbox(self) -> OutboxSettings:
        return OutboxSettings(sink=self.sink, drain_timeout_s=self.drain_timeout_s)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(scope="module")
def schema_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_schema_template(tmp_path_factory.mktemp("schema"))


@pytest.fixture
def backend(tmp_path: Path, schema_template: Path) -> ControlPlaneMetadataSqliteBackend:
    return sqlite_backend_from_template(schema_template, tmp_path)


@pytest.fixture
def switches() -> Switches:
    return Switches()


@pytest.fixture
def golden_repos_dir(tmp_path: Path) -> Path:
    path = tmp_path / "golden-repos"
    path.mkdir()
    return path


def _metadata(
    golden_repos_dir: Path, backend, switches: Switches
) -> ControlPlaneMetadata:
    component = ControlPlaneMetadata(
        golden_repos_dir, backend=backend, settings=switches.metadata
    )
    component.import_files()
    return component


@pytest.fixture
def metadata(golden_repos_dir, backend, switches) -> Iterator[ControlPlaneMetadata]:
    with scoped_install(_metadata(golden_repos_dir, backend, switches)) as component:
        yield component


def make_sweeper(
    metadata: ControlPlaneMetadata,
    backend,
    switches: Switches,
    sinks: Optional[Dict[str, MetadataSink]] = None,
    clock: Optional[Callable[[], float]] = None,
) -> MetadataOutboxSweeper:
    table = sinks if sinks is not None else {LOCAL_FILES: LocalFilesSink()}
    return MetadataOutboxSweeper(
        metadata=metadata,
        backend=backend,
        settings=switches.outbox,
        sink_factory=lambda name: table[name],
        now_fn=clock if clock is not None else Clock(),
    )


def alias_file(golden_repos_dir: Path, name: str = ALIAS) -> Path:
    return golden_repos_dir / "aliases" / f"{name}.json"


def manager_for(golden_repos_dir: Path) -> AliasManager:
    return AliasManager(golden_repos_dir / "aliases")


# ===========================================================================
# The design's eleven tests
# ===========================================================================


def test_1_alias_change_on_mountless_server_lands_in_file_via_workspace_mcp(
    tmp_path, backend, switches
):
    """Mutation: skip the file write -> the writer never receives the file.

    Mount-less: the golden-repos directory does not exist on this pod, so
    the ONLY way the file can land is the content writer.
    """
    golden_repos_dir = tmp_path / "not-mounted" / "golden-repos"
    switches.sink = WORKSPACE_MCP
    writer = FakeContentWriter()
    with scoped_install(_metadata(golden_repos_dir, backend, switches)) as metadata:
        manager = manager_for(golden_repos_dir)
        manager.create_alias(ALIAS, "/snapshots/v_1")

        make_sweeper(
            metadata, backend, switches, {WORKSPACE_MCP: ContentWriterSink(writer)}
        ).sweep_once()

        path = str(alias_file(golden_repos_dir))
        assert json.loads(writer.files[path]) == manager.read_record(ALIAS)
        assert backend.outbox_depth() == 0
        assert not golden_repos_dir.exists()


def test_2_workspace_mcp_failure_keeps_the_row_and_a_later_sweep_lands_it(
    metadata, backend, switches, golden_repos_dir
):
    """Mutation: delete the row on failure -> depth drops to 0 on the failed
    sweep and the file never lands."""
    switches.sink = WORKSPACE_MCP
    writer = FakeContentWriter(failures=1)
    sweeper = make_sweeper(
        metadata, backend, switches, {WORKSPACE_MCP: ContentWriterSink(writer)}
    )
    manager_for(golden_repos_dir).create_alias(ALIAS, "/snapshots/v_1")

    first = sweeper.sweep_once()
    assert first.failed == [(KIND_ALIAS, ALIAS)]
    assert backend.outbox_depth() == 1

    sweeper.sweep_once()
    assert str(alias_file(golden_repos_dir)) in writer.files
    assert backend.outbox_depth() == 0


def test_3_files_read_source_serves_the_dual_written_aliases(
    metadata, backend, switches, golden_repos_dir
):
    """Mutation: ignore the switch -> reads keep coming from the registry,
    which here (deliberately) no longer agrees with the file."""
    manager = manager_for(golden_repos_dir)
    manager.create_alias(ALIAS, "/snapshots/v_1")
    make_sweeper(metadata, backend, switches).sweep_once()
    # The registry moves on without dual-write (a row the rollback never saw).
    backend.put(KIND_ALIAS, ALIAS, {"target_path": "/snapshots/registry-only"})

    switches.read_source = "files"

    assert manager.read_alias(ALIAS) == "/snapshots/v_1"


def test_4_with_dual_write_off_nothing_is_written_to_the_files(
    metadata, backend, switches, golden_repos_dir
):
    """Mutation: ignore the flag -> an outbox row is queued and the sweep
    writes the file."""
    switches.file_dual_write = False
    manager_for(golden_repos_dir).create_alias(ALIAS, "/snapshots/v_1")

    assert backend.outbox_depth() == 0
    make_sweeper(metadata, backend, switches).sweep_once()
    assert not alias_file(golden_repos_dir).exists()


def test_5_transaction_failed_after_registry_write_leaves_neither(
    metadata, backend, golden_repos_dir, monkeypatch
):
    """Mutation: insert the row after commit -> the registry change survives
    the failed outbox insert."""

    def broken_enqueue(conn, kind, name):
        raise RuntimeError("outbox insert failed")

    monkeypatch.setattr(sqlite_backend_module, "_enqueue", broken_enqueue)

    with pytest.raises(RuntimeError):
        manager_for(golden_repos_dir).create_alias(ALIAS, "/snapshots/v_1")

    assert backend.get(KIND_ALIAS, ALIAS) is None
    assert backend.outbox_depth() == 0


class _FailFirstPut:
    """A local-files sink whose first write fails."""

    name = LOCAL_FILES

    def __init__(self) -> None:
        self.inner = LocalFilesSink()
        self.failed_once = False
        self.puts: List[bytes] = []

    def put(self, path: str, data: bytes) -> None:
        if not self.failed_once:
            self.failed_once = True
            raise OSError("transient write failure")
        self.puts.append(data)
        self.inner.put(path, data)

    def delete(self, path: str) -> None:
        self.inner.delete(path)


def test_6_two_changes_to_one_key_whose_first_replay_fails_end_with_the_second(
    metadata, backend, switches, golden_repos_dir
):
    """Mutation: replay the row's payload in arrival order -> the failed
    first row, retried after the second succeeded, rewrites the stale v_1."""
    manager = manager_for(golden_repos_dir)
    manager.create_alias(ALIAS, "/snapshots/v_1")
    manager.swap_alias(ALIAS, "/snapshots/v_2", "/snapshots/v_1")
    sink = _FailFirstPut()
    sweeper = make_sweeper(metadata, backend, switches, {LOCAL_FILES: sink})

    for _ in range(3):
        sweeper.sweep_once()

    on_disk = json.loads(alias_file(golden_repos_dir).read_text())
    assert on_disk["target_path"] == "/snapshots/v_2"
    assert backend.outbox_depth() == 0


class _BlockingSink:
    """Holds the first write open until released; counts writes per path."""

    name = LOCAL_FILES

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


def test_7_two_concurrent_sweepers_write_each_key_once(
    metadata, backend, switches, golden_repos_dir
):
    """Mutation: drop the claim -> the second sweeper replays the keys the
    first is still holding. (SQLite lease here; PostgreSQL SKIP LOCKED in the
    live-PG module.)"""
    manager = manager_for(golden_repos_dir)
    for index in range(3):
        manager.create_alias(f"repo-{index}-global", f"/snapshots/{index}")
    sink = _BlockingSink()
    first = make_sweeper(metadata, backend, switches, {LOCAL_FILES: sink})
    second = make_sweeper(metadata, backend, switches, {LOCAL_FILES: sink})

    holder = threading.Thread(target=first.sweep_once)
    holder.start()
    assert sink.entered.wait(timeout=30)
    second.sweep_once()  # runs while the first holds its claim
    sink.release.set()
    holder.join(timeout=30)

    assert sorted(sink.writes.values()) == [1, 1, 1]
    assert backend.outbox_depth() == 0


def test_8_files_read_source_write_is_visible_to_the_next_read(
    metadata, backend, switches, golden_repos_dir
):
    """Mutation: registry first, file later (via the outbox) -> the next read
    from the files misses the write until a sweep runs."""
    switches.read_source = "files"
    manager = manager_for(golden_repos_dir)

    manager.create_alias(ALIAS, "/snapshots/v_1")

    assert manager.read_alias(ALIAS) == "/snapshots/v_1"
    assert backend.get(KIND_ALIAS, ALIAS)["target_path"] == "/snapshots/v_1"


def test_9_local_files_drain_writes_directly_while_workspace_mcp_fails(
    metadata, backend, switches, golden_repos_dir
):
    """Mutation: replay through workspace-mcp regardless of the sink -> every
    replay fails and depth never reaches 0."""
    manager = manager_for(golden_repos_dir)
    manager.create_alias(ALIAS, "/snapshots/v_1")
    manager.create_alias("other-global", "/snapshots/o")
    failing = FakeContentWriter(fail_always=True)
    sinks: Dict[str, MetadataSink] = {
        WORKSPACE_MCP: ContentWriterSink(failing),
        LOCAL_FILES: LocalFilesSink(),
    }
    switches.sink = LOCAL_FILES

    make_sweeper(metadata, backend, switches, sinks).sweep_once()

    assert backend.outbox_depth() == 0
    assert alias_file(golden_repos_dir).exists()
    assert alias_file(golden_repos_dir, "other-global").exists()
    assert failing.calls == []


class _FailsForName:
    """A local-files sink that can never write one key."""

    name = LOCAL_FILES

    def __init__(self, stuck_name: str) -> None:
        self.inner = LocalFilesSink()
        self.stuck_name = stuck_name
        self.attempts: List[str] = []

    def put(self, path: str, data: bytes) -> None:
        self.attempts.append(Path(path).stem)
        if Path(path).stem == self.stuck_name:
            raise OSError("permission denied")
        self.inner.put(path, data)

    def delete(self, path: str) -> None:
        self.inner.delete(path)


def _messages(caplog: pytest.LogCaptureFixture, marker: str) -> List[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(marker)]


def test_10_drain_stopped_at_its_bound_logs_exactly_the_undrained_keys_and_stops(
    metadata, backend, switches, golden_repos_dir, caplog
):
    """Mutations: drop the log (no key logged); drain past the bound (the
    sink is tried after the bound); keep retrying after the bound (later
    sweeps still try)."""
    manager = manager_for(golden_repos_dir)
    manager.create_alias("stuck-global", "/snapshots/s")
    manager.create_alias(ALIAS, "/snapshots/v_1")
    sink = _FailsForName("stuck-global")
    clock = Clock()
    switches.drain_timeout_s = 600
    sweeper = make_sweeper(metadata, backend, switches, {LOCAL_FILES: sink}, clock)

    sweeper.sweep_once()  # clock starts; ALIAS drains, stuck-global fails
    clock.now += 599
    sweeper.sweep_once()  # still inside the bound: retried
    attempts_inside_bound = len(sink.attempts)

    clock.now += 2
    caplog.set_level(logging.ERROR)
    report = sweeper.sweep_once()
    clock.now += 600
    later = sweeper.sweep_once()

    assert report.stopped == "drain_timeout"
    assert report.undrained == [(KIND_ALIAS, "stuck-global")]
    logged = _messages(caplog, UNDRAINED_KEY_LOG)
    assert len(logged) == 1 and "'stuck-global'" in logged[0]
    assert len(sink.attempts) == attempts_inside_bound
    assert later.stopped == "drain_timeout"
    assert backend.outbox_depth() == 1  # rollback gate stays red


def test_healthy_sweeper_never_reaches_the_drain_bound(
    metadata, backend, switches, golden_repos_dir
):
    """The default local-files sink on a healthy server: a change made long
    after the leader's first sweep is replayed, not counted as undrained.
    Mutation: start the clock on the first sweep and never reset it (the
    change is reported undrained and the sweeper stops)."""
    clock = Clock()
    switches.drain_timeout_s = 600
    sweeper = make_sweeper(metadata, backend, switches, clock=clock)
    sweeper.sweep_once()  # empty outbox
    assert backend.outbox_depth() == 0

    clock.now += 601
    sweeper.sweep_once()
    manager_for(golden_repos_dir).create_alias(ALIAS, "/snapshots/v_1")
    report = sweeper.sweep_once()

    assert report.stopped is None
    assert report.replayed == [(KIND_ALIAS, ALIAS)]
    assert alias_file(golden_repos_dir).exists()
    assert backend.get_outbox_state().stopped_at is None


def test_a_successful_sweep_restarts_the_drain_bound(
    metadata, backend, switches, golden_repos_dir
):
    """The bound measures CONTINUOUS failure: a sweep that drains everything
    in between resets it. Mutation: do not clear the clock on a clean sweep
    (the second, short run of failures stops the sweeper)."""
    manager = manager_for(golden_repos_dir)
    sink = _FailsForName("stuck-global")
    clock = Clock()
    switches.drain_timeout_s = 600
    sweeper = make_sweeper(metadata, backend, switches, {LOCAL_FILES: sink}, clock)
    manager.create_alias("stuck-global", "/snapshots/s")
    sweeper.sweep_once()  # fails: the clock starts

    clock.now += 300
    sink.stuck_name = "nothing-is-stuck"
    assert sweeper.sweep_once().failed == []  # drained: the clock clears

    clock.now += 200
    sink.stuck_name = "stuck-global"
    manager.create_alias("stuck-global", "/snapshots/s2")
    sweeper.sweep_once()  # fails again: a new clock starts here
    clock.now += 400  # 900 s after the first failure, 400 s into this run
    report = sweeper.sweep_once()

    assert report.stopped is None
    assert report.failed == [(KIND_ALIAS, "stuck-global")]


def test_11_override_logs_and_deletes_exactly_the_undrained_rows_and_stops(
    metadata, backend, switches, golden_repos_dir, caplog
):
    """Mutations: flip the read source without logging the keys (no key
    logged, rows remain); delete the rows but leave the sweeper running (a
    later change is still replayed)."""
    switches.sink = WORKSPACE_MCP
    manager = manager_for(golden_repos_dir)
    manager.create_alias("first-global", "/snapshots/1")
    manager.create_alias("second-global", "/snapshots/2")
    writer = FakeContentWriter()
    sweeper = make_sweeper(
        metadata, backend, switches, {WORKSPACE_MCP: ContentWriterSink(writer)}
    )

    caplog.set_level(logging.WARNING)
    abandoned = sweeper.abandon(requested_by="admin")

    assert abandoned == [(KIND_ALIAS, "first-global"), (KIND_ALIAS, "second-global")]
    logged = _messages(caplog, ABANDONED_KEY_LOG)
    assert sorted(m.split("'")[1] for m in logged) == ["first-global", "second-global"]
    assert backend.outbox_depth() == 0

    manager.create_alias("third-global", "/snapshots/3")
    report = sweeper.sweep_once()
    assert report.stopped == "abandoned"
    assert writer.calls == []
    assert backend.outbox_depth() == 1


# ===========================================================================
# Around the design tests
# ===========================================================================


def test_abandon_deletes_only_the_rows_it_logged(metadata, backend, golden_repos_dir):
    """Exactly those rows: one written after the abandon's snapshot stays."""
    manager_for(golden_repos_dir).create_alias("first-global", "/snapshots/1")
    rows_before = backend.outbox_rows()

    manager_for(golden_repos_dir).create_alias("late-global", "/snapshots/l")
    deleted = backend.delete_outbox_rows([row.id for row in rows_before])

    assert deleted == 1
    assert [row.name for row in backend.outbox_rows()] == ["late-global"]


def test_changing_the_sink_resumes_a_stopped_sweeper(
    metadata, backend, switches, golden_repos_dir
):
    switches.sink = WORKSPACE_MCP
    sweeper = make_sweeper(metadata, backend, switches)
    sweeper.abandon(requested_by="admin")
    manager_for(golden_repos_dir).create_alias(ALIAS, "/snapshots/v_1")

    switches.sink = LOCAL_FILES
    sweeper.sweep_once()

    assert alias_file(golden_repos_dir).exists()
    assert backend.outbox_depth() == 0


def test_delete_replays_as_file_removal(metadata, backend, switches, golden_repos_dir):
    manager = manager_for(golden_repos_dir)
    manager.create_alias(ALIAS, "/snapshots/v_1")
    sweeper = make_sweeper(metadata, backend, switches)
    sweeper.sweep_once()
    assert alias_file(golden_repos_dir).exists()

    manager.delete_alias(ALIAS)
    sweeper.sweep_once()

    assert not alias_file(golden_repos_dir).exists()
    assert backend.outbox_depth() == 0


def test_replayed_file_holds_exactly_the_registry_document(
    metadata, backend, switches, golden_repos_dir
):
    manager = manager_for(golden_repos_dir)
    manager.create_alias(ALIAS, "/snapshots/v_1")
    make_sweeper(metadata, backend, switches).sweep_once()

    assert alias_file(golden_repos_dir).read_bytes() == MetadataFiles.encode(
        backend.get(KIND_ALIAS, ALIAS)
    )


def test_dual_written_file_is_not_reimported_at_next_boot(
    metadata, backend, switches, golden_repos_dir
):
    manager_for(golden_repos_dir).create_alias(ALIAS, "/snapshots/v_1")
    make_sweeper(metadata, backend, switches).sweep_once()

    assert metadata.import_files()["imported"] == 0


def test_workspace_mcp_sink_fails_loud_and_keeps_every_row(
    metadata, backend, switches, golden_repos_dir, caplog
):
    """Today's workspace-mcp sink: ERROR at build, every replay raises, rows
    are kept and depth stays visible."""
    caplog.set_level(logging.ERROR)
    sink = build_sink(WORKSPACE_MCP)
    assert isinstance(sink, UnavailableSink)
    assert any(
        "no workspace-mcp write path yet" in r.getMessage() for r in caplog.records
    )

    switches.sink = WORKSPACE_MCP
    manager_for(golden_repos_dir).create_alias(ALIAS, "/snapshots/v_1")
    report = make_sweeper(
        metadata, backend, switches, {WORKSPACE_MCP: sink}
    ).sweep_once()

    assert report.failed == [(KIND_ALIAS, ALIAS)]
    assert backend.outbox_depth() == 1


def test_default_sink_builder_selects_local_files():
    assert isinstance(build_sink(LOCAL_FILES), LocalFilesSink)


def test_write_mode_markers_go_through_the_outbox(
    metadata, backend, switches, golden_repos_dir
):
    from code_indexer.global_repos.write_mode_markers import WriteModeMarkers

    WriteModeMarkers(golden_repos_dir).enter("cidx-meta-global", "/live/cidx-meta")
    make_sweeper(metadata, backend, switches).sweep_once()

    marker = golden_repos_dir / ".write_mode" / "cidx-meta.json"
    assert json.loads(marker.read_text())["source_path"] == "/live/cidx-meta"


def test_depth_gauge_reports_the_outbox_depth(metadata, backend, golden_repos_dir):
    from code_indexer.server.services.metadata_outbox.depth_gauge import (
        OUTBOX_DEPTH_METRIC,
        register_outbox_depth_gauge,
    )

    manager_for(golden_repos_dir).create_alias(ALIAS, "/snapshots/v_1")
    registered = {}

    class _Meter:
        def create_observable_gauge(self, name, description, unit, callbacks):
            registered[name] = callbacks
            return object()

    register_outbox_depth_gauge(_Meter(), backend.outbox_depth)

    observations = list(registered[OUTBOX_DEPTH_METRIC][0](None))
    assert [o.value for o in observations] == [1]


def test_depth_gauge_yields_nothing_when_depth_is_unreadable():
    from code_indexer.server.services.metadata_outbox.depth_gauge import (
        depth_observations,
    )

    def broken() -> int:
        raise RuntimeError("db down")

    assert list(depth_observations(broken)) == []
