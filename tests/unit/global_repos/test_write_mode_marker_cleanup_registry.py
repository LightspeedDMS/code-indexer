"""Stale write-mode marker cleanup over the registry (content seam step 5a).

Markers are control-plane metadata. In SOLO mode (SQLite, one node) a restart
still means every marker is orphaned, so startup cleanup removes them all, as
Bug #240 established. In CLUSTER mode the markers live in the shared registry:
another node's open session survives this node's restart, so startup cleanup
removes only markers past their TTL. Real SQLite registry backend; the
``cluster`` flag is the one difference between the two parametrized runs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from unittest.mock import Mock

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.control_plane_metadata import ControlPlaneMetadata
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import (
    WRITE_MODE_MARKER_TTL_SECONDS,
    RefreshScheduler,
)
from code_indexer.global_repos.write_mode_markers import WriteModeMarkers
from code_indexer.server.storage.protocols.control_plane_metadata_backend import (
    KIND_WRITE_MODE,
)
from code_indexer.server.storage.sqlite_backends import (
    ControlPlaneMetadataSqliteBackend,
)
from tests.utils.control_plane_metadata_fixtures import (
    build_schema_template,
    scoped_install,
    sqlite_backend_from_template,
)


@pytest.fixture(scope="module")
def schema_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_schema_template(tmp_path_factory.mktemp("schema"))


@pytest.fixture
def golden_repos_dir(tmp_path: Path) -> Path:
    path = tmp_path / "golden-repos"
    path.mkdir()
    return path


@pytest.fixture
def backend(tmp_path: Path, schema_template: Path) -> ControlPlaneMetadataSqliteBackend:
    return sqlite_backend_from_template(schema_template, tmp_path)


def _install(
    golden_repos_dir: Path, backend: ControlPlaneMetadataSqliteBackend, cluster: bool
) -> Iterator[None]:
    component = ControlPlaneMetadata(golden_repos_dir, backend=backend, cluster=cluster)
    component.import_files()
    with scoped_install(component):
        yield


@pytest.fixture
def solo(golden_repos_dir, backend) -> Iterator[None]:
    yield from _install(golden_repos_dir, backend, cluster=False)


@pytest.fixture
def cluster(golden_repos_dir, backend) -> Iterator[None]:
    yield from _install(golden_repos_dir, backend, cluster=True)


def _scheduler(golden_repos_dir: Path) -> RefreshScheduler:
    config = Mock()
    config.get_global_refresh_interval.return_value = 3600
    registry = Mock()
    registry.list_global_repos.return_value = []
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config,
        query_tracker=Mock(spec=QueryTracker),
        cleanup_manager=Mock(spec=CleanupManager),
        registry=registry,
    )


def _put_marker(backend, alias: str, age_seconds: float) -> None:
    entered = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    backend.put(
        KIND_WRITE_MODE,
        alias,
        {
            "alias": alias,
            "source_path": f"/live/{alias}",
            "entered_at": entered.isoformat(),
        },
    )


def test_solo_startup_cleanup_removes_every_marker(solo, golden_repos_dir, backend):
    _put_marker(backend, "fresh", age_seconds=5)
    _put_marker(backend, "stale", age_seconds=WRITE_MODE_MARKER_TTL_SECONDS + 60)

    _scheduler(golden_repos_dir).cleanup_stale_write_mode_markers(force=True)

    assert WriteModeMarkers(golden_repos_dir).names() == []


def test_cluster_startup_cleanup_keeps_another_nodes_fresh_marker(
    cluster, golden_repos_dir, backend
):
    _put_marker(backend, "fresh", age_seconds=5)
    _put_marker(backend, "stale", age_seconds=WRITE_MODE_MARKER_TTL_SECONDS + 60)

    _scheduler(golden_repos_dir).cleanup_stale_write_mode_markers(force=True)

    assert WriteModeMarkers(golden_repos_dir).names() == ["fresh"]


def test_periodic_cleanup_removes_only_stale_markers(solo, golden_repos_dir, backend):
    _put_marker(backend, "fresh", age_seconds=5)
    _put_marker(backend, "stale", age_seconds=WRITE_MODE_MARKER_TTL_SECONDS + 60)

    _scheduler(golden_repos_dir).cleanup_stale_write_mode_markers()

    assert WriteModeMarkers(golden_repos_dir).names() == ["fresh"]


def test_files_mode_still_removes_a_corrupt_marker_file(tmp_path: Path):
    """No registry: a marker file that does not parse is still a key cleanup
    must remove (Bug #240's corrupt-marker case)."""
    golden_repos_dir = tmp_path / "golden-repos"
    marker = golden_repos_dir / ".write_mode" / "broken.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{not json")

    _scheduler(golden_repos_dir).cleanup_stale_write_mode_markers()

    assert not marker.exists()
