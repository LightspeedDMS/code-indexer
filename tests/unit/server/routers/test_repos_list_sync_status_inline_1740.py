# ruff: noqa: F811
"""
Unit tests for GET /api/repos inlining a REAL per-repo sync_status field
(Bug #1740), and its opt-in cost gate (Bug #1743 code-review finding #4).

Background: list_activated_repositories() previously carried no per-repo
sync_status at all -- the client reported a constant "unknown" (#1740
Option B, an honest-but-fake value) because the only route that could
resolve one (GET /api/repos/{alias}/sync-status) was itself a facade that
never wrote a real value. Now that ActivatedRepoManager.compute_sync_status()
exists (real git-based computation), GET /api/repos populates a real
sync_status per repo directly on the list response -- the same way
current_branch is already populated per-repo -- so the client needs no
enrichment call at all.

compute_sync_status costs up to a few git subprocesses per repo (worse
under cluster NFS), so it is gated behind an opt-in `include_sync_status`
query param (default False -- unchanged, zero-cost listing) rather than
running unconditionally.

Tests:
  test_list_skips_sync_status_computation_by_default -- no query param ->
    compute_sync_status is never called, sync_status is None
  test_list_inlines_real_sync_status_per_repo -- include_sync_status=true:
    each repo's sync_status comes from compute_sync_status
  test_list_sync_status_none_when_compute_raises -- a repo whose
    compute_sync_status raises ActivatedRepoError degrades to
    sync_status=None rather than a 500 for the whole listing
  test_list_still_includes_current_branch_and_deactivation_job -- no
    regression to the pre-existing per-repo fields
  test_list_returns_200_when_golden_repo_registry_orphaned -- code-review
    finding #1 regression: a REAL ActivatedRepoManager wired to a
    MagicMock(spec=GoldenRepoManager) whose get_actual_repo_path raises
    GoldenRepoNotFoundError (Bug #1317 registry-orphan state) must not
    500 the whole listing
"""

from __future__ import annotations

import json
import os
import subprocess
from unittest.mock import MagicMock, Mock

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoError,
    ActivatedRepoManager,
)
from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepo,
    GoldenRepoManager,
    GoldenRepoNotFoundError,
)
from tests.unit.server.routers.inline_routes_test_helpers import (
    _find_route_handler,
    _patch_closure,
    user_client,  # noqa: F401
)


def _base_repo(alias: str) -> dict:
    return {
        "user_alias": alias,
        "golden_repo_alias": alias,
        "current_branch": "main",
        "activated_at": "2026-01-01T00:00:00+00:00",
        "last_accessed": "2026-01-01T00:00:00+00:00",
    }


def _patch_list_route(repos, compute_side_effect=None):
    handler = _find_route_handler("/api/repos", "GET")
    mock_arm = Mock()
    mock_arm.list_activated_repositories.return_value = repos
    if compute_side_effect is not None:
        mock_arm.compute_sync_status.side_effect = compute_side_effect

    mock_bgm = Mock()
    mock_bgm.list_jobs.return_value = {"jobs": []}

    return (
        mock_arm,
        _patch_closure(handler, "activated_repo_manager", mock_arm),
        _patch_closure(handler, "background_job_manager", mock_bgm),
    )


def test_list_skips_sync_status_computation_by_default(user_client):
    """Finding #4: without ?include_sync_status=true, the listing must
    NOT spend any git subprocesses computing sync status -- compute_sync_status
    is never called, and sync_status defaults to None (unchanged shape)."""
    repos = [_base_repo("web-app")]
    mock_arm, arm_patch, bgm_patch = _patch_list_route(repos)

    with arm_patch, bgm_patch:
        response = user_client.get("/api/repos")

    assert response.status_code == 200
    mock_arm.compute_sync_status.assert_not_called()
    assert response.json()["repositories"][0]["sync_status"] is None


def test_list_inlines_real_sync_status_per_repo(user_client):
    repos = [_base_repo("web-app"), _base_repo("api-service")]

    def compute_side_effect(username, alias):
        return {
            "web-app": {
                "current_branch": "main",
                "sync_status": "synced",
                "has_conflicts": False,
                "conflict_details": None,
                "last_sync_time": "2026-01-01T00:00:00+00:00",
            },
            "api-service": {
                "current_branch": "main",
                "sync_status": "needs_sync",
                "has_conflicts": False,
                "conflict_details": None,
                "last_sync_time": "2026-01-01T00:00:00+00:00",
            },
        }[alias]

    _mock_arm, arm_patch, bgm_patch = _patch_list_route(repos, compute_side_effect)
    with arm_patch, bgm_patch:
        response = user_client.get("/api/repos?include_sync_status=true")

    assert response.status_code == 200
    data = response.json()
    by_alias = {r["user_alias"]: r for r in data["repositories"]}
    assert by_alias["web-app"]["sync_status"] == "synced"
    assert by_alias["api-service"]["sync_status"] == "needs_sync"


def test_list_sync_status_none_when_compute_raises(user_client):
    repos = [_base_repo("flaky-repo")]

    def compute_side_effect(username, alias):
        raise ActivatedRepoError("race with deactivation")

    _mock_arm, arm_patch, bgm_patch = _patch_list_route(repos, compute_side_effect)
    with arm_patch, bgm_patch:
        response = user_client.get("/api/repos?include_sync_status=true")

    assert response.status_code == 200
    data = response.json()
    assert data["repositories"][0]["sync_status"] is None


def test_list_still_includes_current_branch_and_deactivation_job(user_client):
    """No regression: the pre-existing current_branch and deactivation_job
    fields remain populated alongside the new sync_status field."""
    repos = [_base_repo("web-app")]

    def compute_side_effect(username, alias):
        return {
            "current_branch": "main",
            "sync_status": "synced",
            "has_conflicts": False,
            "conflict_details": None,
            "last_sync_time": "2026-01-01T00:00:00+00:00",
        }

    _mock_arm, arm_patch, bgm_patch = _patch_list_route(repos, compute_side_effect)
    with arm_patch, bgm_patch:
        response = user_client.get("/api/repos?include_sync_status=true")

    assert response.status_code == 200
    repo = response.json()["repositories"][0]
    assert repo["current_branch"] == "main"
    assert repo["deactivation_job"] is None
    assert repo["sync_status"] == "synced"


def _make_real_activated_repo(data_dir: str, username: str, user_alias: str) -> None:
    repo_dir = os.path.join(data_dir, "activated-repos", username, user_alias)
    os.makedirs(repo_dir)
    for args in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "t@test.com"],
        ["git", "config", "user.name", "T"],
    ):
        subprocess.run(args, cwd=repo_dir, check=True, capture_output=True)
    with open(os.path.join(repo_dir, "f.txt"), "w") as handle:
        handle.write("x\n")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True
    )

    metadata = {
        "username": username,
        "user_alias": user_alias,
        "golden_repo_alias": "goldenrepo",
        "current_branch": "main",
        "activated_at": "2026-01-01T00:00:00+00:00",
        "last_accessed": "2026-01-01T00:00:00+00:00",
    }
    metadata_path = os.path.join(
        data_dir, "activated-repos", username, f"{user_alias}_metadata.json"
    )
    with open(metadata_path, "w") as handle:
        json.dump(metadata, handle)


def test_list_returns_200_when_golden_repo_registry_orphaned(user_client, tmp_path):
    """Code-review finding #1 regression (commit 347dbeb3): get_actual_repo_path
    raising GoldenRepoNotFoundError (Bug #1317 registry-orphan state) must
    not 500 the whole listing. Uses a REAL ActivatedRepoManager (not a bare
    mock) wired to a MagicMock(spec=GoldenRepoManager) that never raises by
    default -- explicitly configuring get_actual_repo_path to raise is what
    proves the gap; a mock that "just works" would hide it exactly the way
    this bug slipped through round 1 review.
    """
    handler = _find_route_handler("/api/repos", "GET")
    data_dir = str(tmp_path)
    _make_real_activated_repo(data_dir, "testuser", "myrepo")

    golden_repo = GoldenRepo(
        alias="goldenrepo",
        repo_url="file:///nonexistent",
        default_branch="main",
        clone_path="/nonexistent/golden-path",
        created_at="2026-01-01T00:00:00+00:00",
    )
    golden_mock = MagicMock(spec=GoldenRepoManager)
    golden_mock.get_golden_repo.return_value = golden_repo
    golden_mock.get_actual_repo_path.side_effect = GoldenRepoNotFoundError(
        "golden repo 'goldenrepo' clone missing on disk"
    )
    real_arm = ActivatedRepoManager(
        data_dir=data_dir,
        golden_repo_manager=golden_mock,
        background_job_manager=Mock(),
    )

    mock_bgm = Mock()
    mock_bgm.list_jobs.return_value = {"jobs": []}

    with _patch_closure(handler, "activated_repo_manager", real_arm):
        with _patch_closure(handler, "background_job_manager", mock_bgm):
            response = user_client.get("/api/repos?include_sync_status=true")

    assert response.status_code == 200, (
        "GET /api/repos 500'd on a registry-orphan golden repo "
        f"(got {response.status_code}: {response.text})"
    )
    repo = response.json()["repositories"][0]
    assert repo["user_alias"] == "myrepo"
    # Sync status genuinely could not be verified -- unknown, never "synced".
    assert repo["sync_status"] is None
