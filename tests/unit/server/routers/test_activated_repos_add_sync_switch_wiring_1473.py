"""Regression tests for Bug #1473: add_index_type/sync_repository/switch_branch
had the same no-op background job bug fixed in #1472 for trigger_reindex --
each submitted callable was a literal `pass` stub that returned a genuine
job_id and HTTP 202 while performing zero actual work.

The fix wires each submitted job to the REAL, already-tested entry point for
that operation instead of inventing a new mechanism:

- add_index_type -> ActivatedRepoIndexManager.trigger_reindex(index_types=[index_type])
  (the same manager/pipeline #1472 wired trigger_reindex to).
- sync_repository -> ActivatedRepoManager.sync_with_golden_repository(...)
  (the same method the sibling synchronous PUT /api/repos/{alias}/sync route
  in routers/inline_repos.py already calls directly), optionally chaining a
  real follow-up ActivatedRepoIndexManager.trigger_reindex job when the
  caller requests reindex=True.
- switch_branch -> ActivatedRepoManager.switch_branch(...) (the same method
  the sibling synchronous PUT /api/repos/{alias}/branch route already calls
  directly).

These tests use a REAL BackgroundJobManager (not a mock) so the submitted
callable is genuinely executed on a background worker thread. For sync and
switch_branch, real git repositories on disk are used (not mocked git
subprocess calls) so the assertions prove actual on-disk git state changed
-- the strongest possible evidence that real work happened, not just that
submit_job was called with the right arguments.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_user_hybrid
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.routers.activated_repos import router


@pytest.fixture
def app_with_router():
    app = FastAPI()
    app.include_router(router)

    test_user = User(
        username="alice",
        password_hash="hashed",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    app.dependency_overrides[get_current_user_hybrid] = lambda: test_user
    yield app, test_user
    app.dependency_overrides.clear()


@pytest.fixture
def client(app_with_router):
    app, _ = app_with_router
    return TestClient(app, raise_server_exceptions=False)


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"git {args} failed in {cwd}: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return result


def _init_repo_with_commit(path: Path) -> str:
    """Init a real git repo with one commit; returns the current branch name."""
    path.mkdir(parents=True, exist_ok=True)
    _run_git(["init"], path)
    _run_git(["config", "user.email", "test@example.com"], path)
    _run_git(["config", "user.name", "Test User"], path)
    (path / "README.md").write_text("initial content\n")
    _run_git(["add", "."], path)
    _run_git(["commit", "-m", "initial commit"], path)
    branch = _run_git(["symbolic-ref", "--short", "HEAD"], path).stdout.strip()
    return str(branch)


def _wait_for_job(bjm: BackgroundJobManager, job_id: str, username: str, timeout=10.0):
    deadline = time.time() + timeout
    last_status = None
    while time.time() < deadline:
        last_status = bjm.get_job_status(job_id, username)
        if last_status is not None and last_status["status"] in (
            "completed",
            "completed_partial",
            "failed",
        ):
            return last_status
        time.sleep(0.05)
    raise AssertionError(
        f"Job {job_id} did not reach a terminal state within {timeout}s: "
        f"last_status={last_status}"
    )


def _fake_completed_process(args):
    return SimpleNamespace(returncode=0, stdout="", stderr="", args=args)


# ---------------------------------------------------------------------------
# add_index_type
# ---------------------------------------------------------------------------


def test_add_index_type_job_actually_invokes_real_indexing_subprocess(
    client, app_with_router, tmp_path, monkeypatch
):
    """The background job submitted by add_index_type must, when executed,
    perform REAL indexing work for the single requested index type -- not a
    no-op. Mirrors test_reindex_job_actually_invokes_real_indexing_subprocess
    _per_index_type from Bug #1472, scoped to one index type."""
    _app, test_user = app_with_router

    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    repo_dir = tmp_path / "data" / "activated-repos" / "alice" / "my-repo"
    (repo_dir / ".code-indexer").mkdir(parents=True)
    (repo_dir / ".code-indexer" / "config.json").write_text("{}")

    activated_manager = MagicMock()
    activated_manager.get_activated_repo_path.return_value = str(repo_dir)

    real_job_manager = BackgroundJobManager()

    captured_commands: list[list[str]] = []

    def fake_run_cancellable_subprocess(
        args, cwd=None, env=None, cancel_check=None, poll_interval=None
    ):
        captured_commands.append(list(args))
        return _fake_completed_process(args)

    def fake_subprocess_run(args, cwd=None, capture_output=None, text=None, env=None):
        captured_commands.append(list(args))
        return _fake_completed_process(args)

    try:
        with (
            patch(
                "code_indexer.server.routers.activated_repos._get_activated_repo_manager",
                return_value=activated_manager,
            ),
            patch(
                "code_indexer.server.routers.activated_repos._get_background_job_manager",
                return_value=real_job_manager,
            ),
            patch(
                "code_indexer.server.services.activated_repo_index_manager"
                ".run_cancellable_subprocess",
                side_effect=fake_run_cancellable_subprocess,
            ),
            patch(
                "code_indexer.server.services.activated_repo_index_manager"
                ".subprocess.run",
                side_effect=fake_subprocess_run,
            ),
        ):
            response = client.post(
                "/api/activated-repos/my-repo/indexes/semantic",
            )

            assert response.status_code == 202, response.text
            job_id = response.json()["job_id"]
            assert response.json()["index_type"] == "semantic"

            final_status = _wait_for_job(real_job_manager, job_id, test_user.username)
    finally:
        real_job_manager.shutdown()

    assert final_status["status"] == "completed", (
        f"add_index_type job did not complete successfully: {final_status}"
    )
    assert final_status["result"]["success"] is True, final_status["result"]

    joined_commands = [" ".join(cmd) for cmd in captured_commands]
    assert "cidx index" in joined_commands, (
        "Bug #1473: add_index_type('semantic') must trigger a real 'cidx "
        f"index' subprocess call. Captured commands: {captured_commands}"
    )
    assert not any("--fts" in cmd for cmd in joined_commands), (
        "add_index_type('semantic') must NOT also index fts. "
        f"Captured commands: {captured_commands}"
    )
    assert not any("scip generate" in cmd for cmd in joined_commands), (
        "add_index_type('semantic') must NOT also generate scip. "
        f"Captured commands: {captured_commands}"
    )


# ---------------------------------------------------------------------------
# sync_repository
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_fixture(tmp_path):
    """Real golden + activated git repos wired for sync_with_golden_repository.

    Creates a 'golden-source' repo, clones it to the activated-repos layout
    as the 'golden' remote (mirroring real activation), then adds a new
    commit to golden-source AFTER the clone so there is real content to
    fetch+merge during sync.
    """
    data_dir = tmp_path / "data"
    golden_src = tmp_path / "golden-source"
    branch = _init_repo_with_commit(golden_src)

    repo_dir = data_dir / "activated-repos" / "alice" / "my-repo"
    repo_dir.parent.mkdir(parents=True)
    _run_git(["clone", str(golden_src), str(repo_dir)], tmp_path)
    _run_git(["config", "user.email", "test@example.com"], repo_dir)
    _run_git(["config", "user.name", "Test User"], repo_dir)
    _run_git(["remote", "rename", "origin", "golden"], repo_dir)

    metadata_path = repo_dir.parent / "my-repo_metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "user_alias": "my-repo",
                "golden_repo_alias": "test-golden",
                "current_branch": branch,
                "activated_at": "2025-01-01T00:00:00Z",
                "last_accessed": "2025-01-01T00:00:00Z",
            }
        )
    )

    # Add real new content to golden-source AFTER clone -- this is what
    # sync must pull down for real.
    (golden_src / "NEW_FILE.txt").write_text("new content from golden\n")
    _run_git(["add", "."], golden_src)
    _run_git(["commit", "-m", "add new file"], golden_src)

    golden_repo_manager_mock = MagicMock()
    golden_repo_manager_mock.get_golden_repo.return_value = None

    real_activated_manager = ActivatedRepoManager(
        data_dir=str(data_dir),
        golden_repo_manager=golden_repo_manager_mock,
    )

    return SimpleNamespace(
        data_dir=data_dir,
        golden_src=golden_src,
        repo_dir=repo_dir,
        branch=branch,
        activated_manager=real_activated_manager,
    )


def test_sync_job_actually_invokes_real_git_sync(client, app_with_router, sync_fixture):
    """The background job submitted by sync_repository must, when executed,
    perform a REAL git fetch+merge against the golden repository -- not a
    no-op. Proven by asserting the new file from golden-source actually
    lands in the activated repo's real working tree on disk."""
    _app, test_user = app_with_router
    real_job_manager = BackgroundJobManager()

    try:
        with (
            patch(
                "code_indexer.server.routers.activated_repos._get_activated_repo_manager",
                return_value=sync_fixture.activated_manager,
            ),
            patch(
                "code_indexer.server.routers.activated_repos._get_background_job_manager",
                return_value=real_job_manager,
            ),
        ):
            response = client.post(
                "/api/activated-repos/my-repo/sync",
                json={"reindex": False},
            )

            assert response.status_code == 202, response.text
            job_id = response.json()["job_id"]

            final_status = _wait_for_job(real_job_manager, job_id, test_user.username)
    finally:
        real_job_manager.shutdown()

    assert final_status["status"] == "completed", (
        f"sync job did not complete successfully: {final_status}"
    )
    assert final_status["result"]["success"] is True, final_status["result"]
    assert final_status["result"]["changes_applied"] is True, final_status["result"]

    new_file = sync_fixture.repo_dir / "NEW_FILE.txt"
    assert new_file.exists(), (
        "Bug #1473: sync_repository's job must perform a REAL git "
        "fetch+merge -- the file added to golden-source after clone must "
        f"appear in the activated repo's real working tree. repo_dir "
        f"contents: {list(sync_fixture.repo_dir.iterdir())}"
    )
    assert new_file.read_text() == "new content from golden\n"


def test_sync_job_with_reindex_flag_triggers_real_followup_reindex_job(
    client, app_with_router, sync_fixture, monkeypatch
):
    """When reindex=True is requested, the sync job must chain a REAL
    follow-up job through ActivatedRepoIndexManager.trigger_reindex (the
    same public, job-tracked entry point used by trigger_reindex/
    add_index_type) -- not silently drop the flag."""
    _app, test_user = app_with_router

    # ActivatedRepoIndexManager (constructed inside sync_job) resolves its
    # own data_dir from CIDX_SERVER_DATA_DIR when not passed explicitly --
    # point it at the SAME data_dir the sync fixture's ActivatedRepoManager
    # uses so the path-confinement check in trigger_reindex passes.
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(sync_fixture.data_dir.parent))
    (sync_fixture.repo_dir / ".code-indexer").mkdir(exist_ok=True)
    (sync_fixture.repo_dir / ".code-indexer" / "config.json").write_text("{}")

    real_job_manager = BackgroundJobManager()
    real_subprocess_run = subprocess.run

    captured_commands: list[list[str]] = []

    def fake_run_cancellable_subprocess(
        args, cwd=None, env=None, cancel_check=None, poll_interval=None
    ):
        captured_commands.append(list(args))
        return _fake_completed_process(args)

    def fake_subprocess_run(
        args, cwd=None, capture_output=None, text=None, env=None, **kwargs
    ):
        # `subprocess` is a singleton module, so patching
        # activated_repo_index_manager.subprocess.run also intercepts the
        # REAL git subprocess.run calls made by ActivatedRepoManager's sync
        # logic in this same test. Only stub `cidx` invocations (no real
        # binary needed); delegate everything else (git) to the real
        # subprocess.run so the sync's actual fetch+merge still executes.
        if args and args[0] == "cidx":
            captured_commands.append(list(args))
            return _fake_completed_process(args)
        return real_subprocess_run(
            args, cwd=cwd, capture_output=capture_output, text=text, env=env, **kwargs
        )

    try:
        with (
            patch(
                "code_indexer.server.routers.activated_repos._get_activated_repo_manager",
                return_value=sync_fixture.activated_manager,
            ),
            patch(
                "code_indexer.server.routers.activated_repos._get_background_job_manager",
                return_value=real_job_manager,
            ),
            # Only the indexer module's own subprocess boundary is stubbed
            # (no real `cidx` binary needed). Global subprocess.run stays
            # REAL so the sync's real git fetch+merge still executes for
            # real in the same test.
            patch(
                "code_indexer.server.services.activated_repo_index_manager"
                ".run_cancellable_subprocess",
                side_effect=fake_run_cancellable_subprocess,
            ),
            patch(
                "code_indexer.server.services.activated_repo_index_manager"
                ".subprocess.run",
                side_effect=fake_subprocess_run,
            ),
        ):
            response = client.post(
                "/api/activated-repos/my-repo/sync",
                json={"reindex": True},
            )

            assert response.status_code == 202, response.text
            sync_job_id = response.json()["job_id"]

            sync_final_status = _wait_for_job(
                real_job_manager, sync_job_id, test_user.username
            )
            assert sync_final_status["status"] == "completed", sync_final_status
            reindex_job_id = sync_final_status["result"]["reindex_job_id"]
            assert reindex_job_id, (
                "Bug #1473: reindex=True must produce a real chained job_id, "
                f"got result={sync_final_status['result']}"
            )

            reindex_final_status = _wait_for_job(
                real_job_manager, reindex_job_id, test_user.username
            )
    finally:
        real_job_manager.shutdown()

    assert reindex_final_status["status"] == "completed", (
        f"chained reindex job did not complete successfully: {reindex_final_status}"
    )
    assert reindex_final_status["result"]["success"] is True, reindex_final_status[
        "result"
    ]

    joined_commands = [" ".join(cmd) for cmd in captured_commands]
    assert "cidx index" in joined_commands, (
        "Bug #1473: sync's reindex=True must trigger real per-index-type "
        f"indexing subprocess calls. Captured commands: {captured_commands}"
    )
    assert "cidx index --fts" in joined_commands
    assert any(cmd.startswith("cidx scip generate") for cmd in joined_commands)


# ---------------------------------------------------------------------------
# switch_branch
# ---------------------------------------------------------------------------


@pytest.fixture
def switch_branch_fixture(tmp_path):
    """Real activated git repo with two local branches, no remote --
    exercises ActivatedRepoManager.switch_branch's local-checkout path."""
    data_dir = tmp_path / "data"
    repo_dir = data_dir / "activated-repos" / "alice" / "my-repo"
    default_branch = _init_repo_with_commit(repo_dir)

    _run_git(["checkout", "-b", "feature-x"], repo_dir)
    (repo_dir / "README.md").write_text("feature branch content\n")
    _run_git(["commit", "-am", "feature commit"], repo_dir)

    _run_git(["checkout", default_branch], repo_dir)

    metadata_path = repo_dir.parent / "my-repo_metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "user_alias": "my-repo",
                "current_branch": default_branch,
                "activated_at": "2025-01-01T00:00:00Z",
                "last_accessed": "2025-01-01T00:00:00Z",
            }
        )
    )

    golden_repo_manager_mock = MagicMock()
    real_activated_manager = ActivatedRepoManager(
        data_dir=str(data_dir),
        golden_repo_manager=golden_repo_manager_mock,
    )

    return SimpleNamespace(
        data_dir=data_dir,
        repo_dir=repo_dir,
        default_branch=default_branch,
        activated_manager=real_activated_manager,
    )


def test_switch_branch_job_actually_invokes_real_git_checkout(
    client, app_with_router, switch_branch_fixture
):
    """The background job submitted by switch_branch must, when executed,
    perform a REAL git checkout -- not a no-op. Proven by asserting the
    repo's actual current branch and working tree content on disk changed
    to match the target branch."""
    _app, test_user = app_with_router
    real_job_manager = BackgroundJobManager()

    try:
        with (
            patch(
                "code_indexer.server.routers.activated_repos._get_activated_repo_manager",
                return_value=switch_branch_fixture.activated_manager,
            ),
            patch(
                "code_indexer.server.routers.activated_repos._get_background_job_manager",
                return_value=real_job_manager,
            ),
        ):
            response = client.post(
                "/api/activated-repos/my-repo/branch",
                json={"branch_name": "feature-x"},
            )

            assert response.status_code == 202, response.text
            job_id = response.json()["job_id"]

            final_status = _wait_for_job(real_job_manager, job_id, test_user.username)
    finally:
        real_job_manager.shutdown()

    assert final_status["status"] == "completed", (
        f"switch_branch job did not complete successfully: {final_status}"
    )
    assert final_status["result"]["success"] is True, final_status["result"]

    current_branch = _run_git(
        ["symbolic-ref", "--short", "HEAD"], switch_branch_fixture.repo_dir
    ).stdout.strip()
    assert current_branch == "feature-x", (
        "Bug #1473: switch_branch's job must perform a REAL git checkout -- "
        f"expected repo HEAD to be 'feature-x', got '{current_branch}'"
    )
    assert (
        switch_branch_fixture.repo_dir / "README.md"
    ).read_text() == "feature branch content\n"
