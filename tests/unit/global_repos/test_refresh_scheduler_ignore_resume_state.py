"""Indexer resume-state trust and containment.

Server-spawned indexing must ignore resume state on
``RefreshScheduler._index_source()`` -- the REAL scheduled golden-repo
refresh path (distinct from ``GoldenRepoManager._execute_post_clone_workflow``,
which only covers the initial add / git-pull-triggered incremental-refresh
flow, NOT the periodic scheduler). This is "the dominant, steady-state
production `cidx index` spawn" per this module's own comment -- runs on
every golden repo, every refresh cycle.

Fixed via the single shared seam (``append_server_layout_args`` in
``server/utils/index_command_layout.py``), which this file's semantic+FTS
command already routes through (proven by the pre-existing Story #1488 AST
guard). This test proves the fix reaches this REAL call site end-to-end,
mirroring the mocking pattern of
tests/unit/global_repos/test_refresh_scheduler_subprocess_env_sanitization_1325.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.config import ConfigManager


@pytest.fixture
def golden_repos_dir(tmp_path):
    grd = tmp_path / "golden_repos"
    grd.mkdir(parents=True)
    return grd


@pytest.fixture
def config_mgr(tmp_path):
    return ConfigManager(tmp_path / ".code-indexer" / "config.json")


@pytest.fixture
def query_tracker():
    return QueryTracker()


@pytest.fixture
def cleanup_manager(query_tracker):
    return CleanupManager(query_tracker)


@pytest.fixture
def source_repo(tmp_path):
    src = tmp_path / "source_repo"
    src.mkdir()
    (src / "README.md").write_text("# Test Repo")
    (src / ".git").mkdir()
    return src


@pytest.fixture
def mock_registry():
    registry = MagicMock()
    registry.get_global_repo.return_value = {
        "alias": "test-repo-global",
        "repo_url": "git@github.com:org/repo.git",
        "enable_temporal": False,
        "temporal_options": None,
        "enable_scip": False,
    }
    return registry


@pytest.fixture
def scheduler(
    golden_repos_dir, config_mgr, query_tracker, cleanup_manager, mock_registry
):
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=query_tracker,
        cleanup_manager=cleanup_manager,
        registry=mock_registry,
    )


def _capture_popen(calls):
    def _fake(*, command, phase_name, env=None, **kwargs):
        calls.append({"command": command, "phase_name": phase_name, "env": env})
        return 100

    return _fake


def test_index_source_semantic_command_ignores_resume_state(scheduler, source_repo):
    """The scheduled golden-repo refresh's `cidx index --fts ...` command
    must never trust committer-authored resume state."""
    popen_calls: list = []
    with patch(
        "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
        side_effect=_capture_popen(popen_calls),
    ):
        scheduler._index_source(
            alias_name="test-repo-global", source_path=str(source_repo)
        )

    by_phase = {c["phase_name"]: c["command"] for c in popen_calls}
    assert "semantic" in by_phase, f"expected a semantic-phase call, got: {popen_calls}"
    semantic_command = by_phase["semantic"]
    assert "--ignore-resume-state" in semantic_command, (
        "SECURITY: the scheduled golden-repo refresh's semantic+FTS `cidx "
        "index` command must never trust committer-authored resume state. "
        f"Got command: {semantic_command}"
    )


def test_index_source_reconcile_command_also_ignores_resume_state(
    scheduler, source_repo
):
    """The needs_reconcile=True branch (crash-recovery / extension-drift
    reindex) builds a DIFFERENT command literal (`--reconcile` appended) --
    it must also carry --ignore-resume-state."""
    popen_calls: list = []
    with patch(
        "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
        side_effect=_capture_popen(popen_calls),
    ):
        scheduler._index_source(
            alias_name="test-repo-global",
            source_path=str(source_repo),
            force_reconcile=True,
        )

    by_phase = {c["phase_name"]: c["command"] for c in popen_calls}
    semantic_command = by_phase["semantic"]
    assert "--reconcile" in semantic_command, (
        f"expected force_reconcile=True to produce a --reconcile command, "
        f"got: {semantic_command}"
    )
    assert "--ignore-resume-state" in semantic_command, (
        "SECURITY: the reconcile-mode refresh command must also never trust "
        f"committer-authored resume state. Got command: {semantic_command}"
    )
