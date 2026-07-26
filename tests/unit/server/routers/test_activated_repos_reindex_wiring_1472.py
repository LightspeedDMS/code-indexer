"""Regression tests for Bug #1472: trigger_reindex's background job was a
literal no-op stub (`pass`) -- the endpoint returned a genuine job_id and
HTTP 202 while performing zero actual indexing work, for ANY index_types
value.

The fix wires the submitted job to the REAL, already-tested indexing
pipeline (ActivatedRepoIndexManager.trigger_reindex ->
_execute_indexing_job -> per-index-type `cidx` subprocess calls) instead of
inventing a new mechanism.

These tests use a REAL BackgroundJobManager (not a mock) so the submitted
callable is genuinely executed on a background worker thread, and assert
on the REAL subprocess commands that ActivatedRepoIndexManager issues for
each requested index type (semantic -> `cidx index`, fts -> `cidx index
--fts`, scip -> `cidx scip generate`). This proves the job actually
performs real indexing work end-to-end -- not just that submit_job was
called with the right arguments.

Bare FastAPI app exposing only the router under test, auth dependency
overridden. `_get_activated_repo_manager()`/`_get_background_job_manager()`
read from the GLOBAL `code_indexer.server.app.app.state` (not the local
test app's state), so they are patched directly at the module level rather
than via app.state assignment -- matching the existing pattern in
test_activated_repos_reindex_temporal_default_1457.py.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_user_hybrid
from code_indexer.server.auth.user_manager import User, UserRole
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


def _fake_completed_process(args):
    """Real subprocess.run/run_cancellable_subprocess return a
    CompletedProcess-like object with returncode/stdout/stderr; build a
    minimal stand-in via SimpleNamespace."""
    return SimpleNamespace(returncode=0, stdout="", stderr="", args=args)


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


def test_reindex_job_actually_invokes_real_indexing_subprocess_per_index_type(
    client, app_with_router, tmp_path, monkeypatch
):
    """The background job submitted by trigger_reindex must, when executed,
    perform REAL per-index-type indexing work -- not a no-op.

    Drives the full router -> ActivatedRepoIndexManager ->
    _execute_indexing_job -> subprocess wiring with a REAL
    BackgroundJobManager (real worker thread), stubbing only the outermost
    subprocess boundary (run_cancellable_subprocess / subprocess.run) so no
    actual `cidx` binary needs to run. Asserts that each of the three
    default index types (semantic, fts, scip) results in its own distinct,
    real subprocess invocation with the expected command.
    """
    _app, test_user = app_with_router

    # ActivatedRepoIndexManager.trigger_reindex enforces a real path-
    # confinement check (repo path must resolve under its data_dir).
    # CIDX_SERVER_DATA_DIR drives data_dir = <env>/data (see __init__), so
    # placing repo_dir under tmp_path/data/... satisfies that check exactly
    # like a real deployment's activated-repos layout.
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
                "/api/activated-repos/my-repo/reindex",
                json={},
            )

            assert response.status_code == 202, response.text
            job_id = response.json()["job_id"]
            assert response.json()["index_types"] == ["semantic", "fts", "scip"]

            final_status = _wait_for_job(real_job_manager, job_id, test_user.username)
    finally:
        real_job_manager.shutdown()

    assert final_status["status"] == "completed", (
        f"Reindex job did not complete successfully: {final_status}"
    )
    assert final_status["result"]["success"] is True, final_status["result"]

    joined_commands = [" ".join(cmd) for cmd in captured_commands]

    assert "cidx index" in joined_commands, (
        "Bug #1472: semantic index type must trigger a real 'cidx index' "
        f"subprocess call. Captured commands: {captured_commands}"
    )
    assert "cidx index --fts" in joined_commands, (
        "Bug #1472: fts index type must trigger a real 'cidx index --fts' "
        f"subprocess call. Captured commands: {captured_commands}"
    )
    assert any(cmd.startswith("cidx scip generate") for cmd in joined_commands), (
        "Bug #1472: scip index type must trigger a real 'cidx scip generate' "
        f"subprocess call. Captured commands: {captured_commands}"
    )


def test_reindex_job_wires_normalized_index_types_to_index_manager(client, tmp_path):
    """When a specific, mixed-case index_types subset is requested, the
    submitted job must forward the SAME normalized list to
    ActivatedRepoIndexManager.trigger_reindex -- proving real per-request
    wiring, not a hardcoded default."""
    repo_dir = tmp_path / "my-repo"
    repo_dir.mkdir()

    activated_manager = MagicMock()
    activated_manager.get_activated_repo_path.return_value = str(repo_dir)
    job_manager = MagicMock()
    job_manager.submit_job.return_value = "job-456"

    with (
        patch(
            "code_indexer.server.routers.activated_repos._get_activated_repo_manager",
            return_value=activated_manager,
        ),
        patch(
            "code_indexer.server.routers.activated_repos._get_background_job_manager",
            return_value=job_manager,
        ),
        patch(
            "code_indexer.server.services.activated_repo_index_manager"
            ".ActivatedRepoIndexManager.trigger_reindex",
        ) as mock_trigger_reindex,
    ):
        mock_trigger_reindex.return_value = "job-456"

        response = client.post(
            "/api/activated-repos/my-repo/reindex",
            json={"index_types": ["FTS"]},
        )

    assert response.status_code == 202, response.text
    assert response.json()["job_id"] == "job-456"
    assert response.json()["index_types"] == ["fts"]

    mock_trigger_reindex.assert_called_once_with(
        repo_alias="my-repo",
        index_types=["fts"],
        clear=False,
        username="alice",
    )
