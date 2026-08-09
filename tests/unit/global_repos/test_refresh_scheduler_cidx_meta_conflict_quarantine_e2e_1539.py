"""Real-git, un-mocked end-to-end proofs for Bug #1539's SHA-based
cidx-meta conflict quarantine (Codex round-3 items (a)/(c) verified via
the actual _execute_refresh code path, not by manually clearing DB
state or by mocking the git/sync layer).

Sibling module test_refresh_scheduler_cidx_meta_conflict_quarantine_1539.py
holds the mocked unit-level tests (backend record/reset/get, scheduler
skip/get-state decision logic, cluster-mode-no-backend fail-open).
"""

import subprocess

from code_indexer.config import ConfigManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import (
    _CIDX_META_CONFLICT_QUARANTINE_THRESHOLD,
    RefreshScheduler,
)
from code_indexer.server.services.cidx_meta_backup.bootstrap import (
    CidxMetaBackupBootstrap,
)
from code_indexer.server.services.cidx_meta_backup.sync import (
    resolve_upstream_target_sha,
)
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


_GIT_IDENTITY_ENV_OVERRIDES = {
    "GIT_AUTHOR_NAME": "cidx-meta-quarantine-test",
    "GIT_AUTHOR_EMAIL": "cidx-meta-quarantine-test@example.invalid",
    "GIT_COMMITTER_NAME": "cidx-meta-quarantine-test",
    "GIT_COMMITTER_EMAIL": "cidx-meta-quarantine-test@example.invalid",
}


def _git_env():
    import os

    env = dict(os.environ)
    env.update(_GIT_IDENTITY_ENV_OVERRIDES)
    return env


def _run_git(args, cwd):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    )


def _bootstrap_cidx_meta_repo(golden_repos_dir, tmp_path):
    """Real bootstrap of a cidx-meta clone against a real bare remote --
    identical to Story #926's own test fixtures. Returns (repo_dir, remote).
    """
    repo_dir = golden_repos_dir / "cidx-meta"
    repo_dir.mkdir()
    (repo_dir / ".code-indexer").mkdir()
    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    (repo_dir / "README.md").write_text("seed\n")
    CidxMetaBackupBootstrap().bootstrap(str(repo_dir), remote.as_uri())
    return repo_dir, remote


def _push_new_remote_commit(remote, tmp_path, filename, content):
    """Simulate the world changing: a new commit lands on the remote
    branch via a fresh clone (never touching the scheduler's own repo)."""
    divergent = tmp_path / "divergent"
    subprocess.run(
        ["git", "clone", remote.as_uri(), str(divergent)],
        check=True,
        capture_output=True,
        text=True,
    )
    (divergent / filename).write_text(content)
    _run_git(["add", "-A"], divergent)
    _run_git(["commit", "-m", "remote: new commit"], divergent)
    _run_git(["push", "origin", "master"], divergent)


class _RegistryStub:
    def get_global_repo(self, alias_name):
        return {"repo_url": "local://cidx-meta", "default_branch": "master"}

    def update_refresh_timestamp(self, alias_name):
        return None


def _build_scheduler(golden_repos_dir, tmp_path, backend):
    config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
    sched = RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=QueryTracker(),
        cleanup_manager=CleanupManager(QueryTracker()),
        registry=_RegistryStub(),
        golden_repo_metadata_backend=backend,
    )
    # These mocks isolate the conflict-quarantine mechanism from the
    # UNRELATED indexing/snapshot pipeline (same convention as the
    # pre-existing test_refresh_scheduler_cidx_meta_backup.py). The git
    # sync path itself (CidxMetaBackupSync, MetaDirectoryUpdater,
    # CidxMetaBackupBootstrap) is deliberately left REAL in both tests
    # below -- that is the actual mechanism under test.
    sched.alias_manager.read_alias = MagicMock(
        return_value=str(golden_repos_dir / ".versioned" / "cidx-meta" / "v_1")
    )
    sched._detect_existing_indexes = MagicMock(return_value={})
    sched._reconcile_registry_with_filesystem = MagicMock()
    sched._index_source = MagicMock()
    sched._create_snapshot = MagicMock(return_value=str(tmp_path / "snapshot"))
    sched.alias_manager.swap_alias = MagicMock()
    sched.is_write_locked = MagicMock(return_value=False)
    sched._reset_fetch_failures = MagicMock()
    sched._has_local_changes = MagicMock(return_value=False)
    return sched


def test_execute_refresh_skips_real_sync_when_quarantined(tmp_path):
    """The observable-behavior fix: once quarantined for the CURRENT
    upstream target SHA, _execute_refresh never even attempts a real
    sync -- it returns a skip result immediately, so no more FAILED jobs
    pile up. Uses a real cidx-meta repo (bootstrapped, no further
    commits) so resolve_upstream_target_sha inside _execute_refresh
    resolves the SAME SHA the test pre-seeds as quarantined.
    """
    golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
    golden_repos_dir.mkdir(parents=True)
    repo_dir, remote = _bootstrap_cidx_meta_repo(golden_repos_dir, tmp_path)

    target_sha = resolve_upstream_target_sha(str(repo_dir), "master")
    assert target_sha is not None

    backend = GoldenRepoMetadataSqliteBackend(str(tmp_path / "metadata.db"))
    backend.ensure_table_exists()
    for _ in range(_CIDX_META_CONFLICT_QUARANTINE_THRESHOLD):
        backend.record_cidx_meta_conflict_failure("cidx-meta-global", target_sha, "d")

    sched = _build_scheduler(golden_repos_dir, tmp_path, backend)

    config_service = SimpleNamespace(
        get_config=lambda: SimpleNamespace(
            cidx_meta_backup_config=SimpleNamespace(
                enabled=True, remote_url=remote.as_uri()
            )
        ),
        sync_repo_extensions_if_drifted=MagicMock(),
    )

    # If sync() were called for real here, it would be a no-op success
    # (no remote drift, no local changes) -- so genuinely observing "no
    # sync attempted" requires inspecting the git side-effect directly:
    # a real sync() would leave .git untouched too (nothing to commit or
    # push), so instead we assert on the QUARANTINE RESULT ITSELF, which
    # only fires when the skip-check runs and matches -- proven correct
    # independently by the unit-level skip_result tests.
    with patch(
        "code_indexer.global_repos.refresh_scheduler.get_config_service",
        return_value=config_service,
    ):
        result = sched._execute_refresh("cidx-meta-global")

    assert result["success"] is False
    assert result["skipped"] == "cidx_meta_conflict_quarantined"
    # The quarantine record is untouched (no reset happened -- proves
    # _perform_cidx_meta_backup_sync, and therefore sync(), never ran).
    state = backend.get_cidx_meta_conflict_failure_state("cidx-meta-global")
    assert state is not None
    assert (
        state["consecutive_failure_count"] == _CIDX_META_CONFLICT_QUARANTINE_THRESHOLD
    )


def test_quarantine_auto_clears_on_real_sync_after_sha_change(tmp_path):
    """(c) A quarantined state automatically un-quarantines once the
    upstream SHA changes -- proven via a REAL sync attempt (the actual
    _execute_refresh code path against real git repos, with NO mocking
    of MetaDirectoryUpdater/CidxMetaBackupBootstrap/CidxMetaBackupSync),
    not by manually clearing persisted state.
    """
    golden_repos_dir = tmp_path / ".code-indexer" / "golden_repos"
    golden_repos_dir.mkdir(parents=True)
    repo_dir, remote = _bootstrap_cidx_meta_repo(golden_repos_dir, tmp_path)

    old_sha = resolve_upstream_target_sha(str(repo_dir), "master")
    assert old_sha is not None

    backend = GoldenRepoMetadataSqliteBackend(str(tmp_path / "metadata.db"))
    backend.ensure_table_exists()
    for _ in range(_CIDX_META_CONFLICT_QUARANTINE_THRESHOLD):
        backend.record_cidx_meta_conflict_failure("cidx-meta-global", old_sha, "d")

    _push_new_remote_commit(remote, tmp_path, "new_file.txt", "new content\n")

    sched = _build_scheduler(golden_repos_dir, tmp_path, backend)

    config_service = SimpleNamespace(
        get_config=lambda: SimpleNamespace(
            cidx_meta_backup_config=SimpleNamespace(
                enabled=True, remote_url=remote.as_uri()
            )
        ),
        sync_repo_extensions_if_drifted=MagicMock(),
    )

    with patch(
        "code_indexer.global_repos.refresh_scheduler.get_config_service",
        return_value=config_service,
    ):
        result = sched._execute_refresh("cidx-meta-global")

    assert result.get("skipped") != "cidx_meta_conflict_quarantined"
    assert backend.get_cidx_meta_conflict_failure_state("cidx-meta-global") is None

    verify = tmp_path / "verify"
    subprocess.run(
        ["git", "clone", remote.as_uri(), str(verify)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert (verify / "new_file.txt").read_text() == "new content\n"
