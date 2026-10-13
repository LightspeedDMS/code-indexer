"""cidx-meta's refresh on a mount-less pod indexes the SHARED content.

Under a delegated content backend the pod's ``golden-repos/cidx-meta`` holds
index state only; the content -- what every server-side writer and the write
tools put -- is workspace-mcp's. So the refresh must:

* decide "changed" from the shared content's manifest mtimes, never a walk of
  the pod's directory;
* spawn ``cidx index`` with ``--content-backend=delegated`` (git stays local:
  cidx-meta is no repository git-mcp holds);
* accept "No files found to index" only when the shared content is genuinely
  empty -- an unreachable backend fails the refresh;
* refuse the git backup, loudly, every cycle, while still indexing.

Under the local backend every one of these is today's code path.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.services import cidx_meta_content as cmc
from code_indexer.services.progress_subprocess_runner import IndexingSubprocessError
from tests.unit.server.cidx_meta_seam_fakes import (
    MetaWorld,
)


@pytest.fixture
def meta_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MetaWorld:
    return MetaWorld(tmp_path, monkeypatch)


ALIAS = "cidx-meta-global"
SNAPSHOT_TS = 1_700_000_000


def _scheduler(golden: Path, repo_url: str = "local://cidx-meta") -> RefreshScheduler:
    registry = MagicMock()
    registry.list_global_repos.return_value = []
    registry.get_global_repo.return_value = {
        "alias_name": ALIAS,
        "repo_url": repo_url,
        "enable_temporal": False,
        "enable_scip": False,
    }
    config_source = MagicMock()
    config_source.get_global_refresh_interval.return_value = 3600
    scheduler = RefreshScheduler(
        golden_repos_dir=str(golden),
        config_source=config_source,
        query_tracker=MagicMock(spec=QueryTracker),
        cleanup_manager=MagicMock(spec=CleanupManager),
        registry=registry,
    )
    scheduler._latest_versioned_timestamp = lambda alias: SNAPSHOT_TS  # type: ignore[method-assign]
    return scheduler


def _seed_at(world: MetaWorld, rel: str, mtime: int) -> None:
    world.seed(rel, "content\n")
    for meta in (world.local_meta, world.remote_meta):
        os.utime(meta / rel, (mtime, mtime))


# ── change detection ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "files, expected",
    [
        ({}, False),  # nothing visible: no changes
        ({"old.md": SNAPSHOT_TS - 10}, False),
        ({"old.md": SNAPSHOT_TS - 10, "memories/new.md": SNAPSHOT_TS + 10}, True),
        ({".hidden/new.md": SNAPSHOT_TS + 10}, False),
    ],
)
def test_change_detection_agrees_local_and_shared(
    meta_world: MetaWorld, files: Dict[str, int], expected: bool
) -> None:
    for rel, mtime in files.items():
        _seed_at(meta_world, rel, mtime)

    local = _scheduler(meta_world.local_golden)._has_local_changes(
        str(meta_world.local_meta), ALIAS
    )
    meta_world.delegated()
    with meta_world.forbid_local_io():
        shared = _scheduler(meta_world.pod_golden)._has_local_changes(
            str(meta_world.pod_meta), ALIAS
        )
    assert local is shared is expected


def test_change_detection_ignores_the_pods_own_directory(
    meta_world: MetaWorld,
) -> None:
    """A fresh file on the POD (where the old code looked) is not a change;
    a fresh shared file is."""
    (meta_world.pod_meta / "stray.md").write_text("pod-local\n")
    os.utime(meta_world.pod_meta / "stray.md", (SNAPSHOT_TS + 99,) * 2)
    _seed_at(meta_world, "a.md", SNAPSHOT_TS - 1)
    meta_world.delegated()
    scheduler = _scheduler(meta_world.pod_golden)
    assert scheduler._has_local_changes(str(meta_world.pod_meta), ALIAS) is False
    (meta_world.remote_meta / "a.md").write_text("new shared content\n")
    assert scheduler._has_local_changes(str(meta_world.pod_meta), ALIAS) is True


def test_change_detection_fails_loud_when_the_backend_is_down(
    meta_world: MetaWorld,
) -> None:
    meta_world.delegated()
    meta_world.backend.fail = ConnectionError("workspace-mcp down")
    with pytest.raises(cmc.CidxMetaUnavailableError, match="workspace-mcp down"):
        _scheduler(meta_world.pod_golden)._has_local_changes(
            str(meta_world.pod_meta), ALIAS
        )


def test_other_repos_never_use_the_seam(meta_world: MetaWorld) -> None:
    """Only cidx-meta's content moved; another local:// repo is still walked
    on this pod even under a delegated backend."""
    notes = meta_world.pod_golden / "notes"
    notes.mkdir()
    (notes / "n.md").write_text("x")
    os.utime(notes / "n.md", (SNAPSHOT_TS + 5,) * 2)
    meta_world.delegated()
    meta_world.backend.fail = AssertionError("seam used for another repo")
    assert (
        _scheduler(meta_world.pod_golden)._has_local_changes(str(notes), "notes-global")
        is True
    )


def test_extension_drift_scans_the_shared_content(
    meta_world: MetaWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    drift = SimpleNamespace(added={"yaml"}, removed=set())
    service = SimpleNamespace(sync_repo_extensions_if_drifted=lambda path: drift)
    monkeypatch.setattr(
        "code_indexer.global_repos.refresh_scheduler.get_config_service",
        lambda: service,
    )
    meta_world.delegated()
    scheduler = _scheduler(meta_world.pod_golden)
    with meta_world.forbid_local_io():
        assert (
            scheduler._check_extension_drift(str(meta_world.pod_meta), ALIAS) is False
        )
    meta_world.seed("dependency-map/a.yaml", "x")
    with meta_world.forbid_local_io():
        assert scheduler._check_extension_drift(str(meta_world.pod_meta), ALIAS) is True


# ── indexing ─────────────────────────────────────────────────────────────


NO_FILES = "❌ Indexing failed: No files found to index\n"


def _index(
    scheduler: RefreshScheduler,
    source: Path,
    *,
    error: str = "",
    backends: str = "delegated",
) -> List[List[str]]:
    commands: List[List[str]] = []

    def fake_run(**kwargs: Any) -> None:
        commands.append(kwargs["command"])
        if error:
            raise IndexingSubprocessError(error)

    with (
        patch(
            "code_indexer.server.utils.index_command_layout.configured_content_backend_name",
            return_value=backends,
        ),
        patch(
            "code_indexer.server.utils.index_command_layout.configured_git_backend_name",
            return_value=backends,
        ),
        patch(
            "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
            return_value=(0, 0),
        ),
        patch(
            "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
            side_effect=fake_run,
        ),
        patch(
            "code_indexer.global_repos.refresh_scheduler._build_source_trigram_index"
        ),
        patch("code_indexer.global_repos.refresh_scheduler._record_fts_build_commit"),
    ):
        scheduler._index_source(ALIAS, str(source))
    return commands


def test_the_index_child_reads_cidx_meta_through_the_content_backend(
    meta_world: MetaWorld,
) -> None:
    meta_world.seed("a.md", "x")
    meta_world.delegated()
    commands = _index(_scheduler(meta_world.pod_golden), meta_world.pod_meta)
    assert "--content-backend=delegated" in commands[0]
    assert "--git-backend=delegated" not in commands[0]


def test_other_local_repos_still_index_this_pods_disk(meta_world: MetaWorld) -> None:
    notes = meta_world.pod_golden / "notes"
    notes.mkdir()
    meta_world.delegated()
    scheduler = _scheduler(meta_world.pod_golden, repo_url="local://notes")
    commands: List[List[str]] = []
    with (
        patch(
            "code_indexer.server.utils.index_command_layout.configured_content_backend_name",
            return_value="delegated",
        ),
        patch(
            "code_indexer.server.utils.index_command_layout.configured_git_backend_name",
            return_value="delegated",
        ),
        patch(
            "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
            return_value=(0, 0),
        ),
        patch(
            "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
            side_effect=lambda **kw: commands.append(kw["command"]),
        ),
        patch(
            "code_indexer.global_repos.refresh_scheduler._build_source_trigram_index"
        ),
        patch("code_indexer.global_repos.refresh_scheduler._record_fts_build_commit"),
    ):
        scheduler._index_source("notes-global", str(notes))
    assert not [
        a for a in commands[0] if a.startswith(("--content-backend", "--git-backend"))
    ]


def test_local_backend_command_is_unchanged(meta_world: MetaWorld) -> None:
    commands = _index(
        _scheduler(meta_world.local_golden), meta_world.local_meta, backends="local"
    )
    assert commands[0] == [
        "cidx",
        "index",
        "--fts",
        "--progress-json",
        "--new-collection-layout=chunks_db",
        "--ignore-resume-state",
        "--server-managed-provider-settings",
    ]


def test_genuinely_empty_shared_content_is_tolerated(meta_world: MetaWorld) -> None:
    meta_world.seed(".code-indexer/legacy.json", "{}")  # hidden: not content
    meta_world.delegated()
    _index(_scheduler(meta_world.pod_golden), meta_world.pod_meta, error=NO_FILES)


def test_no_files_while_the_shared_content_has_files_fails(
    meta_world: MetaWorld,
) -> None:
    meta_world.seed("a.md", "x")
    meta_world.delegated()
    with pytest.raises(RuntimeError, match="indexing on source failed"):
        _index(_scheduler(meta_world.pod_golden), meta_world.pod_meta, error=NO_FILES)


def test_no_files_with_an_unreachable_backend_fails(meta_world: MetaWorld) -> None:
    """The child's "No files" plus a backend that cannot be listed is never
    "cidx-meta is empty"."""
    meta_world.delegated()
    scheduler = _scheduler(meta_world.pod_golden)
    meta_world.backend.fail = ConnectionError("workspace-mcp down")
    with pytest.raises(cmc.CidxMetaUnavailableError):
        _index(scheduler, meta_world.pod_meta, error=NO_FILES)


def test_local_empty_meta_tolerance_is_unchanged(meta_world: MetaWorld) -> None:
    meta_world.seed("a.md", "x")  # local: the child's word is the truth
    _index(
        _scheduler(meta_world.local_golden),
        meta_world.local_meta,
        error=NO_FILES,
        backends="local",
    )


# ── reconciliation ───────────────────────────────────────────────────────


def test_missing_description_is_checked_in_the_shared_content(
    meta_world: MetaWorld,
) -> None:
    meta_world.seed("present.md", "x")
    meta_world.delegated()
    scheduler = _scheduler(meta_world.pod_golden)
    cli = MagicMock()
    with meta_world.forbid_local_io():
        assert (
            scheduler._queue_missing_description(
                "present-global", Path("/clone/present"), cli
            )
            is False
        )
        assert (
            scheduler._queue_missing_description(
                "absent-global", Path("/clone/absent"), cli
            )
            is True
        )
    cli.submit_work.assert_called_once()


# ── backup ───────────────────────────────────────────────────────────────


def _refresh_with_backup(
    world: MetaWorld, monkeypatch: pytest.MonkeyPatch, *, changed: bool
) -> MagicMock:
    """Run _execute_refresh for cidx-meta-global with the git backup enabled
    on a delegated pod. Returns the indexing mock."""
    import code_indexer.global_repos.refresh_scheduler as rs

    backup = SimpleNamespace(enabled=True, remote_url="file:///backup.git")
    monkeypatch.setattr(
        rs,
        "get_config_service",
        lambda: SimpleNamespace(
            get_config=lambda: SimpleNamespace(cidx_meta_backup_config=backup),
            sync_repo_extensions_if_drifted=lambda path: None,
        ),
    )
    forbidden = MagicMock(side_effect=AssertionError("backup ran on shared content"))
    monkeypatch.setattr(rs, "CidxMetaBackupSync", forbidden)
    monkeypatch.setattr(rs, "CidxMetaBackupBootstrap", forbidden)
    monkeypatch.setattr(rs, "MetaDirectoryUpdater", forbidden)

    world.delegated()
    scheduler = _scheduler(world.pod_golden)
    scheduler.alias_manager.read_alias = MagicMock(return_value=str(world.pod_meta))
    scheduler.golden_repo_metadata = MagicMock()
    scheduler._detect_existing_indexes = MagicMock(return_value={})  # type: ignore[method-assign]
    scheduler._reconcile_registry_with_filesystem = MagicMock()  # type: ignore[method-assign]
    scheduler._refresh_integrity_quarantine_skip_result = (  # type: ignore[method-assign]
        lambda alias: None
    )
    scheduler._has_local_changes = MagicMock(return_value=changed)  # type: ignore[method-assign]
    index = MagicMock()
    scheduler._index_source = index  # type: ignore[method-assign]
    scheduler._run_and_publish_integrity_gate = MagicMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(passed=True, failures=[])
    )
    scheduler._create_snapshot = MagicMock(return_value=str(world.pod_meta))  # type: ignore[method-assign]
    scheduler._published_point_counts = MagicMock(return_value={})  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="git backup is not supported") as exc:
        scheduler._execute_refresh(ALIAS)
    assert "content_backend='delegated'" in str(exc.value)
    forbidden.assert_not_called()
    return index


def test_backup_is_refused_but_changed_content_is_still_indexed(
    meta_world: MetaWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = _refresh_with_backup(meta_world, monkeypatch, changed=True)
    index.assert_called_once()


def test_backup_refusal_is_loud_even_without_changes(
    meta_world: MetaWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = _refresh_with_backup(meta_world, monkeypatch, changed=False)
    index.assert_not_called()


# ── fingerprint change detection (no clock skew) ─────────────────────────


def _refresh_once(
    world: MetaWorld, monkeypatch: pytest.MonkeyPatch, scheduler: RefreshScheduler
) -> MagicMock:
    """One full _execute_refresh of cidx-meta-global on a delegated pod (no
    backup), with real change detection. Returns the indexing mock."""
    index = MagicMock()
    _refresh_once_with(world, monkeypatch, scheduler, index=index)
    return index


def _refresh_once_with(
    world: MetaWorld,
    monkeypatch: pytest.MonkeyPatch,
    scheduler: RefreshScheduler,
    *,
    index: Any,
) -> None:
    """_refresh_once with *index* as the indexing step (None: the class's)."""
    import code_indexer.global_repos.refresh_scheduler as rs

    monkeypatch.setattr(
        rs,
        "get_config_service",
        lambda: SimpleNamespace(
            get_config=lambda: SimpleNamespace(cidx_meta_backup_config=None),
            sync_repo_extensions_if_drifted=lambda path: None,
        ),
    )
    scheduler.alias_manager.read_alias = MagicMock(return_value=str(world.pod_meta))
    scheduler.golden_repo_metadata = MagicMock()
    scheduler._detect_existing_indexes = MagicMock(return_value={})  # type: ignore[method-assign]
    scheduler._reconcile_registry_with_filesystem = MagicMock()  # type: ignore[method-assign]
    scheduler._refresh_integrity_quarantine_skip_result = (  # type: ignore[method-assign]
        lambda alias: None
    )
    if index is not None:
        scheduler._index_source = index  # type: ignore[method-assign]
    scheduler._run_and_publish_integrity_gate = MagicMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(passed=True, failures=[])
    )
    scheduler._create_snapshot = MagicMock(return_value=str(world.pod_meta))  # type: ignore[method-assign]
    scheduler._published_point_counts = MagicMock(return_value={})  # type: ignore[method-assign]
    scheduler._execute_refresh(ALIAS)


def test_after_an_index_change_detection_ignores_clocks(
    meta_world: MetaWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """workspace-mcp's clock is far AHEAD of this pod: every mtime is newer
    than every snapshot timestamp. After one index, unchanged content is not
    a change any more; new, edited and deleted files are -- whatever their
    mtimes."""
    future = SNAPSHOT_TS + 10**6
    _seed_at(meta_world, "a.md", future)
    _seed_at(meta_world, "b.md", future)
    meta_world.delegated()
    scheduler = _scheduler(meta_world.pod_golden)

    assert _refresh_once(meta_world, monkeypatch, scheduler).call_count == 1
    assert _refresh_once(meta_world, monkeypatch, scheduler).call_count == 0

    (meta_world.remote_meta / "a.md").write_text("edited\n")
    os.utime(meta_world.remote_meta / "a.md", (SNAPSHOT_TS - 99,) * 2)  # clock behind
    assert _refresh_once(meta_world, monkeypatch, scheduler).call_count == 1
    assert _refresh_once(meta_world, monkeypatch, scheduler).call_count == 0

    (meta_world.remote_meta / "b.md").unlink()
    assert _refresh_once(meta_world, monkeypatch, scheduler).call_count == 1


def test_a_failed_refresh_does_not_record_the_fingerprint(
    meta_world: MetaWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a published index records the fingerprint: after a failed
    refresh the same content is still a change."""
    _seed_at(meta_world, "a.md", SNAPSHOT_TS + 5)
    meta_world.delegated()
    scheduler = _scheduler(meta_world.pod_golden)
    original_index = RefreshScheduler._index_source

    def failing_index(self, *args, **kwargs):
        raise RuntimeError("index failed")

    monkeypatch.setattr(RefreshScheduler, "_index_source", failing_index)
    with pytest.raises(RuntimeError):
        _refresh_once_with(meta_world, monkeypatch, scheduler, index=None)
    assert ALIAS not in scheduler._indexed_meta_fingerprints
    monkeypatch.setattr(RefreshScheduler, "_index_source", original_index)
    assert _refresh_once(meta_world, monkeypatch, scheduler).call_count == 1
