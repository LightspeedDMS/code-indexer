"""RefreshScheduler's repository probes on a mount-less pod.

Under ``content_backend='delegated'`` a git golden repo's files are
workspace-mcp's and this pod's ``golden-repos/<alias>`` holds index state only:

* extension drift asks the content backend's manifest whether the clone holds
  files of the drifted extensions -- a walk of the pod's directory finds none
  and silently skipped the reconcile. A listing failure fails the refresh.
  ``local://`` repos (pod-local by design) still walk this disk.
* startup reconciliation never "restores a missing master" from a local
  absence: the clone is not here to be missing.

Under the local backend both are today's code paths.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest

from code_indexer.global_repos import refresh_scheduler as rs
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import (
    RefreshScheduler,
    has_files_with_extensions,
)
from tests.unit.server.cidx_meta_seam_fakes import MetaWorld

ALIAS = "my-repo-global"
EXCLUDE = {".git", "node_modules", "__pycache__", ".code-indexer", ".versioned"}


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MetaWorld:
    return MetaWorld(tmp_path, monkeypatch)


def _scheduler(golden: Path, snapshot_manager: Any = None) -> RefreshScheduler:
    registry = MagicMock()
    registry.list_global_repos.return_value = []
    config_source = MagicMock()
    config_source.get_global_refresh_interval.return_value = 3600
    return RefreshScheduler(
        golden_repos_dir=str(golden),
        config_source=config_source,
        query_tracker=MagicMock(spec=QueryTracker),
        cleanup_manager=MagicMock(spec=CleanupManager),
        registry=registry,
        snapshot_manager=snapshot_manager,
    )


def _drift(monkeypatch: pytest.MonkeyPatch, added: set) -> None:
    service = MagicMock()
    service.sync_repo_extensions_if_drifted.return_value = SimpleNamespace(
        added=added, removed=set()
    )
    monkeypatch.setattr(rs, "get_config_service", lambda: service)


def _seed_remote(world: MetaWorld, rel: str) -> None:
    target = world.remote_meta / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x")


# ── has_files_with_extensions: walk vs manifest parity ───────────────────


@pytest.mark.parametrize(
    "files, expected",
    [
        ([], False),
        (["src/a.py"], True),
        (["src/a.txt"], False),
        ([".hidden/a.py"], False),
        (["node_modules/pkg/a.py"], False),
        (["deep/__pycache__/a.py"], False),
        ([".a.py"], True),  # a hidden FILE still counts; only directories prune
    ],
)
def test_manifest_answers_exactly_as_the_walk(
    world: MetaWorld, files: list, expected: bool
) -> None:
    for rel in files:
        _seed_remote(world, rel)
        target = world.local_meta / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
    local = has_files_with_extensions(str(world.local_meta), {"py"}, EXCLUDE)
    with world.forbid_local_io():
        remote = has_files_with_extensions(
            str(world.pod_meta), {"py"}, EXCLUDE, backend=world.backend
        )
    assert local is expected
    assert remote is expected


def test_manifest_failure_raises(world: MetaWorld) -> None:
    world.backend.fail = RuntimeError("workspace-mcp down")
    with pytest.raises(RuntimeError, match="workspace-mcp down"):
        has_files_with_extensions(
            str(world.pod_meta), {"py"}, EXCLUDE, backend=world.backend
        )


# ── _check_extension_drift ───────────────────────────────────────────────


def test_delegated_git_repo_drift_lists_the_clone_through_the_backend(
    world: MetaWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_remote(world, "src/a.py")
    _drift(monkeypatch, {"py"})
    world.delegated()
    scheduler = _scheduler(world.pod_golden)
    with world.forbid_local_io():
        assert scheduler._check_extension_drift(
            str(world.pod_meta), ALIAS, pod_local=False
        )
    assert world.backend.manifest_calls >= 1


def test_delegated_listing_failure_fails_the_refresh(
    world: MetaWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    _drift(monkeypatch, {"py"})
    world.delegated()
    world.backend.fail = RuntimeError("workspace-mcp down")
    scheduler = _scheduler(world.pod_golden)
    with world.forbid_local_io(), pytest.raises(RuntimeError, match="workspace-mcp"):
        scheduler._check_extension_drift(str(world.pod_meta), ALIAS, pod_local=False)


def test_delegated_pod_local_repo_still_walks_this_disk(
    world: MetaWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    (world.pod_meta / "trace.py").write_text("x")
    _drift(monkeypatch, {"py"})
    world.delegated()
    assert _scheduler(world.pod_golden)._check_extension_drift(
        str(world.pod_meta), ALIAS, pod_local=True
    )
    assert world.backend.manifest_calls == 0


def test_local_backend_walks_this_disk_for_a_git_repo(
    world: MetaWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    (world.local_meta / "a.py").write_text("x")
    _drift(monkeypatch, {"py"})
    assert _scheduler(world.local_golden)._check_extension_drift(
        str(world.local_meta), ALIAS, pod_local=False
    )
    assert world.backend.manifest_calls == 0


def test_config_sync_failure_is_still_only_logged(
    world: MetaWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = MagicMock()
    service.sync_repo_extensions_if_drifted.side_effect = RuntimeError("db down")
    monkeypatch.setattr(rs, "get_config_service", lambda: service)
    assert (
        _scheduler(world.local_golden)._check_extension_drift(
            str(world.local_meta), ALIAS, pod_local=False
        )
        is False
    )


# ── reconcile_golden_repos ───────────────────────────────────────────────


def _reconcile_setup(golden: Path) -> tuple:
    clone_backend = Mock()
    snapshot_manager = Mock()
    snapshot_manager._clone_backend = clone_backend
    snapshot_manager.latest_snapshot.return_value = str(
        golden / ".versioned" / "my-repo" / "v_1"
    )
    scheduler = _scheduler(golden, snapshot_manager=snapshot_manager)
    scheduler.registry.list_global_repos.return_value = [
        {"alias_name": ALIAS, "repo_url": "git@example.com:org/my-repo.git"}
    ]
    return scheduler, clone_backend


def test_local_reconcile_restores_a_missing_master(world: MetaWorld) -> None:
    scheduler, clone_backend = _reconcile_setup(world.local_golden)
    with patch("subprocess.run"):
        scheduler.reconcile_golden_repos()
    clone_backend.create_clone_at_path.assert_called_once()


def test_delegated_reconcile_never_probes_or_restores_the_master(
    world: MetaWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.delegated()
    scheduler, clone_backend = _reconcile_setup(world.pod_golden)
    master = world.pod_golden / "my-repo"
    real_exists = Path.exists

    def guarded_exists(self: Path, *args: Any, **kwargs: Any) -> bool:
        if self == master:
            raise AssertionError("probed the master clone on a delegated pod")
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", guarded_exists)
    with patch("subprocess.run") as run:
        scheduler.reconcile_golden_repos()
    clone_backend.create_clone_at_path.assert_not_called()
    run.assert_not_called()
    monkeypatch.setattr(Path, "exists", real_exists)
    assert (world.pod_golden / ".reconciliation_complete_v1").exists()
    assert not master.exists()
