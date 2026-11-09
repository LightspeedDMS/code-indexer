"""issue-64616: a golden base clone is never a served-index checkout.

The global-activation repair (and an add) publish a repository's index in
place, from its base clone ``<golden_repos_dir>/<alias>``. Publishing marked
that clone ``CHECKED_OUT_MARKER``; the served-index cache took every marked
directory for a checkout and evicted base clones -- the refresh's incremental
base and, with the golden-repos mount on, one directory shared by every pod.

Pinned here:

* ``is_base_clone``: exactly ``<golden_repos_dir>/<alias>``;
* a publish in place (the repair's, through ``resolve_index_fields``) leaves
  no marker on the base clone; a publish from a snapshot still marks it;
* the registry reconcile clears a marker an earlier publish left on a
  registered base clone -- idempotently, never on a snapshot or a path
  outside the golden-repos directory, a failure counted and the rest done --
  and runs as part of ``reconcile_registry_from_filesystem``.
(The served-index cache side is pinned in test_served_index_cache.py.)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.services import global_activation_repair as repair
from code_indexer.server.services import index_materializer as im
from code_indexer.utils.index_store import (
    INDEX_DIRNAME,
    INDEX_VERSION_FIELD,
    DelegatedIndexStore,
)
from tests.unit.server.index_store_fakes import FakeArtifacts

REPO = "example-repo"
_STORE = "code_indexer.server.startup.index_store_wiring.server_index_store"
_REPAIR = (
    "code_indexer.server.services.global_activation_repair.repair_global_activation"
)


def _indexed(path: Path) -> Path:
    collection = path / INDEX_DIRNAME / "index" / "voyage-code-3"
    collection.mkdir(parents=True)
    (collection / "hnsw_index.bin").write_bytes(b"built here")
    (path / INDEX_DIRNAME / "config.json").write_text("{}")
    return path


def _manager(golden: Path, rows: List[Dict[str, Any]]) -> GoldenRepoManager:
    manager = GoldenRepoManager.__new__(GoldenRepoManager)
    manager.golden_repos_dir = str(golden)
    manager.list_golden_repos = MagicMock(  # type: ignore[method-assign]
        return_value=rows
    )
    return manager


# -- is_base_clone -----------------------------------------------------------------


@pytest.mark.parametrize(
    "relative, expected",
    [
        (REPO, True),
        (f"{REPO}/", True),
        (f".versioned/{REPO}/v_1", False),
        (".versioned", False),
        (".temporal", False),
        (f"{REPO}/nested", False),
    ],
)
def test_is_base_clone(tmp_path: Path, relative: str, expected: bool) -> None:
    golden = tmp_path / "golden-repos"
    assert im.is_base_clone(f"{golden}/{relative}", golden) is expected


def test_a_path_outside_the_golden_repos_dir_is_not_a_base_clone(
    tmp_path: Path,
) -> None:
    assert not im.is_base_clone(tmp_path / "elsewhere" / REPO, tmp_path / "golden")


def test_clear_checked_out_marker_is_idempotent(tmp_path: Path) -> None:
    (tmp_path / im.CHECKED_OUT_MARKER).write_text("v1")
    assert im.clear_checked_out_marker(tmp_path) is True
    assert not (tmp_path / im.CHECKED_OUT_MARKER).exists()
    assert im.clear_checked_out_marker(tmp_path) is False


# -- a publish in place leaves no marker -------------------------------------------


def test_the_repair_publishes_a_base_clone_without_marking_it(tmp_path: Path) -> None:
    golden = tmp_path / "golden-repos"
    clone = _indexed(golden / REPO)
    artifacts = FakeArtifacts(tmp_path / "store")
    manager = _manager(golden, [])

    with patch(_STORE, return_value=DelegatedIndexStore(artifacts)):
        fields, published_now = repair.resolve_index_fields(manager, str(clone))

    assert published_now is True
    assert fields is not None
    assert artifacts.current_version(REPO) == fields[INDEX_VERSION_FIELD]
    assert not (clone / im.CHECKED_OUT_MARKER).exists()
    assert (
        clone / INDEX_DIRNAME / "index" / "voyage-code-3" / "hnsw_index.bin"
    ).read_bytes() == b"built here"


def test_a_publish_from_a_snapshot_still_marks_the_snapshot(tmp_path: Path) -> None:
    golden = tmp_path / "golden-repos"
    snapshot = _indexed(golden / ".versioned" / REPO / "v_7")
    manager = _manager(golden, [])

    with patch(_STORE, return_value=DelegatedIndexStore(FakeArtifacts(tmp_path / "s"))):
        fields = manager._publish_index(str(golden / REPO), str(snapshot))

    assert fields is not None
    assert (snapshot / im.CHECKED_OUT_MARKER).read_text() == fields[INDEX_VERSION_FIELD]


# -- the reconcile clears an old marker ----------------------------------------------


def _counts() -> Dict[str, int]:
    return {"errors": 0}


def test_the_reconcile_clears_markers_on_registered_base_clones(tmp_path: Path) -> None:
    golden = tmp_path / "golden-repos"
    marked = _indexed(golden / "marked")
    (marked / im.CHECKED_OUT_MARKER).write_text("v1")
    clean = _indexed(golden / "clean")
    snapshot = _indexed(golden / ".versioned" / "snap" / "v_1")
    (snapshot / im.CHECKED_OUT_MARKER).write_text("v1")
    outside = _indexed(tmp_path / "elsewhere" / "outside")
    (outside / im.CHECKED_OUT_MARKER).write_text("v1")
    rows = [
        {"alias": "marked", "clone_path": str(marked)},
        {"alias": "clean", "clone_path": str(clean)},
        {"alias": "snap", "clone_path": str(snapshot)},
        {"alias": "outside", "clone_path": str(outside)},
        {"alias": "no-path"},
    ]
    manager = _manager(golden, rows)

    counts = _counts()
    manager._clear_base_clone_markers(counts)

    assert counts == {"errors": 0, "base_clone_markers_cleared": 1}
    assert not (marked / im.CHECKED_OUT_MARKER).exists()
    assert (marked / INDEX_DIRNAME / "index").is_dir(), "index kept"
    assert (snapshot / im.CHECKED_OUT_MARKER).is_file(), "a snapshot is a checkout"
    assert (outside / im.CHECKED_OUT_MARKER).is_file(), "not under golden-repos"

    again = _counts()
    manager._clear_base_clone_markers(again)
    assert again == {"errors": 0, "base_clone_markers_cleared": 0}


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_marker_that_cannot_be_cleared_is_counted_and_the_rest_go_on(
    tmp_path: Path,
) -> None:
    golden = tmp_path / "golden-repos"
    stuck = _indexed(golden / "stuck")
    (stuck / im.CHECKED_OUT_MARKER).write_text("v1")
    other = _indexed(golden / "other")
    (other / im.CHECKED_OUT_MARKER).write_text("v1")
    manager = _manager(
        golden,
        [
            {"alias": "stuck", "clone_path": str(stuck)},
            {"alias": "other", "clone_path": str(other)},
        ],
    )
    stuck.chmod(0o555)
    try:
        counts = _counts()
        manager._clear_base_clone_markers(counts)
    finally:
        stuck.chmod(0o755)
    assert counts == {"errors": 1, "base_clone_markers_cleared": 1}
    assert not (other / im.CHECKED_OUT_MARKER).exists()


def test_the_registry_reconcile_runs_the_marker_cleanup(tmp_path: Path) -> None:
    golden = tmp_path / "golden-repos"
    marked = _indexed(golden / "marked")
    (marked / im.CHECKED_OUT_MARKER).write_text("v1")
    manager = GoldenRepoManager(data_dir=str(tmp_path / "data"))
    manager.golden_repos_dir = str(golden)
    manager.list_golden_repos = MagicMock(  # type: ignore[method-assign]
        return_value=[{"alias": "marked", "clone_path": str(marked)}]
    )
    manager._sqlite_backend = MagicMock()

    with patch(_REPAIR, return_value=repair.ActivationRepairResult()):
        counts = manager.reconcile_registry_from_filesystem()

    assert counts["base_clone_markers_cleared"] == 1
    assert not (marked / im.CHECKED_OUT_MARKER).exists()
