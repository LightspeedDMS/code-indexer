"""
Unit tests for ActivatedRepoManager.compute_sync_status (Bug #1740 / Bug #1743).

Background: the whole "sync status" feature was a facade end-to-end --
GET /api/repos/{alias}/sync-status read metadata.get("sync_status") which
nothing ever wrote, always falling through to a hardcoded "synced" default.
This module tests the REAL replacement: a git-based comparison between an
activated repository's current HEAD and its golden repository's HEAD on the
tracked branch, plus real merge-conflict detection via
`git diff --name-only --diff-filter=U`.

Classification:
- "synced":     activated HEAD == golden branch HEAD, no unmerged paths
- "needs_sync": activated HEAD != golden branch HEAD
- "conflict":   working tree has real git unmerged paths (mid-merge)
- None:         sync status could not be verified (unresolvable golden
                 repo/branch/HEAD) -- NEVER "synced" (post-347dbeb3 review
                 finding #3: fabricating "synced" when unverifiable
                 reproduces #1740's exact original symptom).

Tests use REAL git repositories (no mocks on the git layer) per project
anti-mock policy.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import MagicMock

import pytest

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoError,
    ActivatedRepoManager,
)
from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepo,
    GoldenRepoManager,
    GoldenRepoNotFoundError,
)


def _git(*args: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + list(args), cwd=cwd, check=True, capture_output=True, text=True
    )


def _write_text_file(path: str, content: str) -> None:
    """Write *content* to *path* using a guaranteed-close context manager."""
    with open(path, "w") as handle:
        handle.write(content)


def _init_repo(path: str, branch: str = "main") -> None:
    os.makedirs(path, exist_ok=True)
    _git("init", "-b", branch, cwd=path)
    _git("config", "user.email", "t@test.com", cwd=path)
    _git("config", "user.name", "T", cwd=path)


def _commit_file(repo_dir: str, filename: str, content: str, message: str) -> None:
    _write_text_file(os.path.join(repo_dir, filename), content)
    _git("add", ".", cwd=repo_dir)
    _git("commit", "-m", message, cwd=repo_dir)


def _make_golden_repo_manager_mock(golden_repo: GoldenRepo) -> MagicMock:
    mock = MagicMock(spec=GoldenRepoManager)
    mock.get_golden_repo.return_value = golden_repo
    mock.get_actual_repo_path.return_value = golden_repo.clone_path
    return mock


def _make_bgm_mock() -> MagicMock:
    mock = MagicMock()
    mock.submit_job.return_value = "job-001"
    return mock


@pytest.fixture()
def temp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


def _write_metadata(
    temp_dir: str,
    username: str,
    user_alias: str,
    golden_repo_alias: Optional[str],
    current_branch: str,
    extra: Optional[dict] = None,
) -> None:
    """Write activated-repo metadata.

    Note: the metadata key is "last_accessed" -- compute_sync_status maps
    it to the response key "last_sync_time", mirroring the pre-existing
    mapping already used by GET /api/repos/{alias}/sync-status
    (inline_repos.py: last_sync_time = metadata.get("last_accessed")).

    golden_repo_alias=None simulates a composite repo / any activation
    with no single golden repo to compare against.
    """
    user_dir = os.path.join(temp_dir, "activated-repos", username)
    os.makedirs(user_dir, exist_ok=True)
    metadata = {
        "username": username,
        "user_alias": user_alias,
        "golden_repo_alias": golden_repo_alias,
        "current_branch": current_branch,
        "activated_at": datetime.now(timezone.utc).isoformat(),
        "last_accessed": "2026-01-01T10:00:00+00:00",
    }
    if extra:
        metadata.update(extra)
    metadata_path = os.path.join(user_dir, f"{user_alias}_metadata.json")
    with open(metadata_path, "w") as handle:
        json.dump(metadata, handle)


def _build_arm(temp_dir: str, golden_repo: GoldenRepo) -> ActivatedRepoManager:
    grm = _make_golden_repo_manager_mock(golden_repo)
    return ActivatedRepoManager(
        data_dir=temp_dir,
        golden_repo_manager=grm,
        background_job_manager=_make_bgm_mock(),
    )


def _make_golden_repo(alias: str, clone_path: str, default_branch: str) -> GoldenRepo:
    return GoldenRepo(
        alias=alias,
        repo_url="file://" + clone_path,
        default_branch=default_branch,
        clone_path=clone_path,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _make_activated_repo(
    temp_dir: str,
    username: str,
    user_alias: str,
    *,
    golden_repo_alias: Optional[str],
    branch: str = "main",
    commit: bool = True,
    content: str = "x\n",
    extra_metadata: Optional[dict] = None,
) -> str:
    """Create a real, minimal git repo as an activated-repo directory plus
    its metadata.json. commit=False leaves it with zero commits (used to
    make `git rev-parse HEAD` genuinely unresolvable).
    """
    repo_dir = os.path.join(temp_dir, "activated-repos", username, user_alias)
    _init_repo(repo_dir, branch=branch)
    if commit:
        _commit_file(repo_dir, "f.txt", content, "init")
    _write_metadata(
        temp_dir, username, user_alias, golden_repo_alias, branch, extra_metadata
    )
    return repo_dir


def _assert_warning_logged(caplog) -> None:
    assert any(record.levelno == logging.WARNING for record in caplog.records), (
        f"Expected a WARNING log; got levels {[r.levelno for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# Fixture: golden repo + activated clone on same branch/commit (initially synced)
# ---------------------------------------------------------------------------


@pytest.fixture()
def golden_and_activated(temp_dir):
    """
    Real golden git repo cloned into a real activated-repo directory,
    both on branch "main" at the same commit. Returns (arm, golden_path,
    activated_path).
    """
    golden_path = os.path.join(temp_dir, "golden-repos", "myrepo")
    _init_repo(golden_path, branch="main")
    _commit_file(golden_path, "file.txt", "v1\n", "initial")

    activated_path = os.path.join(temp_dir, "activated-repos", "user1", "myrepo")
    os.makedirs(os.path.dirname(activated_path), exist_ok=True)
    _git("clone", golden_path, activated_path, cwd=temp_dir)
    _git("config", "user.email", "t@test.com", cwd=activated_path)
    _git("config", "user.name", "T", cwd=activated_path)

    golden_repo = _make_golden_repo("myrepo", golden_path, "main")
    arm = _build_arm(temp_dir, golden_repo)
    _write_metadata(temp_dir, "user1", "myrepo", "myrepo", "main")

    return arm, golden_path, activated_path


# ---------------------------------------------------------------------------
# Tests: baseline classification (unchanged from the original fix)
# ---------------------------------------------------------------------------


class TestComputeSyncStatusSynced:
    def test_synced_when_head_matches_golden_branch_head(self, golden_and_activated):
        """When activated HEAD == golden branch HEAD, status is 'synced'."""
        arm, _golden_path, _activated_path = golden_and_activated

        result = arm.compute_sync_status("user1", "myrepo")

        assert result["sync_status"] == "synced"
        assert result["has_conflicts"] is False
        assert result["current_branch"] == "main"


class TestComputeSyncStatusNeedsSync:
    def test_needs_sync_when_golden_has_new_commit(self, golden_and_activated):
        """Discriminating case: golden gets a NEW commit on the tracked branch
        after the activated clone was made. The activated repo's HEAD no
        longer matches golden's HEAD -> 'needs_sync'.
        """
        arm, golden_path, _activated_path = golden_and_activated

        _commit_file(golden_path, "file.txt", "v2\n", "golden moved forward")

        result = arm.compute_sync_status("user1", "myrepo")

        assert result["sync_status"] == "needs_sync"
        assert result["has_conflicts"] is False

    def test_needs_sync_when_activated_has_local_commit_golden_lacks(
        self, golden_and_activated
    ):
        """Activated repo diverges by committing locally without golden
        having that commit -- HEADs differ -> 'needs_sync'.
        """
        arm, _golden_path, activated_path = golden_and_activated

        _commit_file(activated_path, "local.txt", "local only\n", "local-only commit")

        result = arm.compute_sync_status("user1", "myrepo")

        assert result["sync_status"] == "needs_sync"


class TestComputeSyncStatusConflict:
    def test_conflict_when_working_tree_has_unmerged_paths(self, golden_and_activated):
        """Discriminating case: force a real git merge conflict in the
        activated repo's working tree (unmerged path detected via `git
        diff --name-only --diff-filter=U`) -> 'conflict', has_conflicts
        True, with non-empty conflict_details naming the conflicted path.
        """
        arm, golden_path, activated_path = golden_and_activated

        # Diverge golden and activated on the SAME file/line so a merge
        # produces a genuine conflict.
        _commit_file(golden_path, "file.txt", "golden change\n", "golden diverges")
        _commit_file(
            activated_path, "file.txt", "activated change\n", "activated diverges"
        )

        # Pull golden's commit in as a second parent so git must merge and
        # collide on file.txt. Assert the merge genuinely conflicted --
        # otherwise this test would silently validate nothing.
        _git("remote", "add", "golden", golden_path, cwd=activated_path)
        _git("fetch", "golden", cwd=activated_path)
        merge_result = subprocess.run(
            ["git", "merge", "golden/main"],
            cwd=activated_path,
            capture_output=True,
            text=True,
        )
        assert merge_result.returncode != 0, (
            "Setup failure: expected merge to conflict but it succeeded "
            f"cleanly (stdout={merge_result.stdout!r})"
        )
        assert "CONFLICT" in merge_result.stdout, (
            "Setup failure: merge did not report a real CONFLICT "
            f"(stdout={merge_result.stdout!r}, stderr={merge_result.stderr!r})"
        )

        result = arm.compute_sync_status("user1", "myrepo")

        assert result["sync_status"] == "conflict"
        assert result["has_conflicts"] is True
        assert result["conflict_details"]
        assert "file.txt" in result["conflict_details"]


# ---------------------------------------------------------------------------
# Tests: errors and "never fabricate synced" degradation
# (code-review findings #1/#2/#3 on commit 347dbeb3)
# ---------------------------------------------------------------------------


class TestComputeSyncStatusErrorsAndDegradation:
    def test_raises_activated_repo_error_when_not_found(self, temp_dir):
        golden_repo = _make_golden_repo(
            "myrepo", os.path.join(temp_dir, "golden-repos", "myrepo"), "main"
        )
        arm = _build_arm(temp_dir, golden_repo)

        with pytest.raises(ActivatedRepoError):
            arm.compute_sync_status("user1", "no-such-alias")

    def test_returns_unknown_when_golden_repo_missing(
        self, golden_and_activated, caplog
    ):
        """Finding #3: if the golden repo backing this activation has
        since been removed (get_golden_repo returns None),
        compute_sync_status must NOT fabricate a "synced" claim it cannot
        substantiate -- it degrades to sync_status=None ("unknown") and
        logs a WARNING. The original fix wrongly degraded to "synced",
        reproducing #1740's exact original symptom.
        """
        arm, _golden_path, _activated_path = golden_and_activated
        arm.golden_repo_manager.get_golden_repo.return_value = None  # type: ignore[union-attr]

        with caplog.at_level(logging.WARNING):
            result = arm.compute_sync_status("user1", "myrepo")

        assert result["sync_status"] is None
        assert result["has_conflicts"] is False
        _assert_warning_logged(caplog)

    def test_returns_last_sync_time_from_metadata_last_accessed(
        self, golden_and_activated
    ):
        """Response key 'last_sync_time' is sourced from the metadata's
        'last_accessed' field (see _write_metadata docstring)."""
        arm, _golden_path, _activated_path = golden_and_activated

        result = arm.compute_sync_status("user1", "myrepo")

        assert result["last_sync_time"] == "2026-01-01T10:00:00+00:00"


class TestComputeSyncStatusNoGoldenAlias:
    def test_returns_unknown_when_no_golden_repo_alias(self, temp_dir, caplog):
        """Composite repos (and any activation missing a single
        golden_repo_alias) have nothing to compare against -- must degrade
        to unknown with a WARNING, never fabricate "synced".
        """
        golden_repo = _make_golden_repo(
            "myrepo", os.path.join(temp_dir, "golden-repos", "myrepo"), "main"
        )
        arm = _build_arm(temp_dir, golden_repo)
        _make_activated_repo(
            temp_dir,
            "user1",
            "composite-repo",
            golden_repo_alias=None,
            extra_metadata={
                "golden_repo_aliases": ["myrepo", "other"],
                "is_composite": True,
            },
        )

        with caplog.at_level(logging.WARNING):
            result = arm.compute_sync_status("user1", "composite-repo")

        assert result["sync_status"] is None
        _assert_warning_logged(caplog)


class TestComputeSyncStatusUnresolvableHead:
    def test_returns_unknown_when_activated_head_unresolvable(self, temp_dir, caplog):
        """A real git repo with zero commits cannot resolve `git rev-parse
        HEAD` -- compute_sync_status must degrade to unknown with a
        WARNING, never fall through to a fabricated "synced".
        """
        golden_repo = _make_golden_repo(
            "myrepo", os.path.join(temp_dir, "golden-repos", "myrepo"), "main"
        )
        arm = _build_arm(temp_dir, golden_repo)
        _make_activated_repo(
            temp_dir, "user1", "empty-repo", golden_repo_alias="myrepo", commit=False
        )

        with caplog.at_level(logging.WARNING):
            result = arm.compute_sync_status("user1", "empty-repo")

        assert result["sync_status"] is None
        _assert_warning_logged(caplog)


class TestComputeSyncStatusGoldenRegistryOrphan:
    def test_returns_unknown_when_get_actual_repo_path_raises_registry_orphan(
        self, golden_and_activated, caplog
    ):
        """Finding #1 (root cause): GoldenRepoManager.get_actual_repo_path()
        raises GoldenRepoNotFoundError when the DB registry row exists but
        the on-disk clone is missing (Bug #1317 registry-orphan state).
        This is a GoldenRepoError, NOT an ActivatedRepoError --
        compute_sync_status must catch it internally (never let it
        escape) and degrade to unknown with a WARNING.
        """
        arm, _golden_path, _activated_path = golden_and_activated
        arm.golden_repo_manager.get_actual_repo_path.side_effect = (  # type: ignore[union-attr]
            GoldenRepoNotFoundError("golden repo 'myrepo' clone missing on disk")
        )

        with caplog.at_level(logging.WARNING):
            result = arm.compute_sync_status("user1", "myrepo")

        assert result["sync_status"] is None
        _assert_warning_logged(caplog)


# ---------------------------------------------------------------------------
# Tests: branch resolution must fall back to refs/remotes/origin/*
# (code-review finding #2 on commit 347dbeb3)
# ---------------------------------------------------------------------------


@pytest.fixture()
def golden_with_remote_tracking_branch(temp_dir):
    """Build a golden repo whose "feature/x" branch exists ONLY as
    refs/remotes/origin/feature/x (fetched from an upstream remote), never
    as a local refs/heads/feature/x branch -- reproducing how a golden
    repo that itself started life as a clone actually looks (verified
    live: 17/18 golden repos on the dev server have a single local head).

    Returns golden_path.
    """
    upstream_path = os.path.join(temp_dir, "upstream", "myrepo")
    _init_repo(upstream_path, branch="feature/x")
    _commit_file(upstream_path, "f.txt", "upstream v1\n", "upstream feature commit")

    golden_path = os.path.join(temp_dir, "golden-repos", "myrepo")
    _init_repo(golden_path, branch="main")
    _commit_file(golden_path, "base.txt", "main v1\n", "golden main commit")

    # Fetch (not clone/checkout) -- populates refs/remotes/origin/feature/x
    # WITHOUT creating a local refs/heads/feature/x branch, matching a real
    # golden repo that only ever checks out its default branch.
    _git("remote", "add", "origin", upstream_path, cwd=golden_path)
    _git("fetch", "origin", cwd=golden_path)

    heads_probe = subprocess.run(
        ["git", "rev-parse", "refs/heads/feature/x"],
        cwd=golden_path,
        capture_output=True,
        text=True,
    )
    assert heads_probe.returncode != 0, (
        "Setup failure: refs/heads/feature/x unexpectedly exists in golden -- "
        "this test needs it resolvable ONLY via refs/remotes/origin"
    )

    return golden_path


class TestComputeSyncStatusBranchRefFallback:
    def test_needs_sync_resolved_via_remote_tracking_branch_fallback(
        self, temp_dir, golden_with_remote_tracking_branch
    ):
        """Discriminating case: the activated repo is on a non-default
        branch that only exists in golden under refs/remotes/origin/, with
        genuinely divergent content vs golden's tracked commit. Must
        resolve to 'needs_sync' (real divergence detected) -- NOT 'synced'
        (what the pre-fallback code would have silently claimed) and NOT
        'unknown' (what an incomplete fallback might wrongly report).
        """
        golden_repo = _make_golden_repo(
            "myrepo", golden_with_remote_tracking_branch, "main"
        )
        arm = _build_arm(temp_dir, golden_repo)
        _make_activated_repo(
            temp_dir,
            "user1",
            "featurerepo",
            golden_repo_alias="myrepo",
            branch="feature/x",
            content="activated diverged content\n",
        )

        result = arm.compute_sync_status("user1", "featurerepo")

        assert result["sync_status"] == "needs_sync"

    def test_synced_resolved_via_remote_tracking_branch_fallback(
        self, temp_dir, golden_with_remote_tracking_branch
    ):
        """Same remote-tracking-only branch, but the activated repo carries
        the EXACT SAME commit object as golden's origin/feature/x --
        fetched directly via a refspec, not recreated from matching file
        content (commit hashes embed timestamp/author, so two
        independently-authored commits with identical content would NOT
        reliably produce identical hashes). The fallback must still
        correctly report 'synced' (not 'unknown').
        """
        golden_repo = _make_golden_repo(
            "myrepo", golden_with_remote_tracking_branch, "main"
        )
        arm = _build_arm(temp_dir, golden_repo)

        repo_dir = os.path.join(temp_dir, "activated-repos", "user1", "featurerepo2")
        os.makedirs(repo_dir, exist_ok=True)
        _git("init", cwd=repo_dir)
        _git("config", "user.email", "t@test.com", cwd=repo_dir)
        _git("config", "user.name", "T", cwd=repo_dir)
        _git(
            "fetch",
            golden_with_remote_tracking_branch,
            "refs/remotes/origin/feature/x:refs/heads/feature/x",
            cwd=repo_dir,
        )
        _git("checkout", "feature/x", cwd=repo_dir)

        _write_metadata(temp_dir, "user1", "featurerepo2", "myrepo", "feature/x")

        result = arm.compute_sync_status("user1", "featurerepo2")

        assert result["sync_status"] == "synced"

    def test_returns_unknown_when_branch_missing_from_both_heads_and_remotes(
        self, temp_dir, golden_and_activated, caplog
    ):
        """A branch name that resolves under NEITHER refs/heads NOR
        refs/remotes/origin in golden must degrade to unknown with a
        WARNING -- never 'synced'.
        """
        arm, _golden_path, activated_path = golden_and_activated
        _git("checkout", "-b", "totally-unknown-branch", cwd=activated_path)
        # compute_sync_status reads current_branch from metadata, not live
        # git state, so the metadata must be updated to match.
        _write_metadata(temp_dir, "user1", "myrepo", "myrepo", "totally-unknown-branch")

        with caplog.at_level(logging.WARNING):
            result = arm.compute_sync_status("user1", "myrepo")

        assert result["sync_status"] is None
        _assert_warning_logged(caplog)


# ---------------------------------------------------------------------------
# Tests: conflict probe must be tri-state (True/False/None), never fabricate
# "synced" when the probe itself could not determine anything (code review
# on commit 7bdd0dee -- the one remaining blocking finding).
# ---------------------------------------------------------------------------


def _inject_unmerged_index_entry(repo_dir: str, path: str) -> None:
    """Directly stage a real 3-way unmerged (conflict) index entry for
    *path* via `git update-index --index-info` plumbing.

    This avoids needing to construct a real merge/cherry-pick conflict
    through porcelain commands, and crucially leaves HEAD (and every ref)
    completely untouched -- reproducing the reviewer's exact repro shape:
    "activated repo mid-cherry-pick with a real unmerged path, HEAD equal
    to golden's HEAD".
    """
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repo_dir,
        input="conflicted content\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    index_info = (
        "\n".join(f"100644 {blob} {stage}\t{path}" for stage in (1, 2, 3)) + "\n"
    )
    subprocess.run(
        ["git", "update-index", "--index-info"],
        cwd=repo_dir,
        input=index_info,
        capture_output=True,
        text=True,
        check=True,
    )


def _is_conflict_probe_command(cmd) -> bool:
    """Recognize the conflict-probe subprocess.run call by its semantic
    shape rather than one exact argv, so this test keeps discriminating
    correctly across both the pre-fix (`git status --porcelain`) and
    post-fix (`git diff --diff-filter=U` / `git ls-files --unmerged`)
    implementations without needing to change alongside the fix.
    """
    if not cmd or cmd[0] != "git":
        return False
    return (
        ("status" in cmd and "--porcelain" in cmd)
        or ("diff" in cmd and "--diff-filter=U" in cmd)
        or ("ls-files" in cmd and "--unmerged" in cmd)
    )


def _fail_conflict_probe(monkeypatch, mode: str) -> None:
    """Patch subprocess.run so ONLY the conflict-probe git command fails;
    every other git invocation (activated HEAD rev-parse, golden branch
    resolution) proceeds via the REAL subprocess.run -- matching the
    reviewer's repro where "only the status probe faulted".
    """
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if _is_conflict_probe_command(cmd):
            if mode == "timeout":
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)
            return subprocess.CompletedProcess(
                cmd,
                returncode=128,
                stdout="",
                stderr="fatal: fault-injected git failure",
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)


class TestComputeSyncStatusConflictProbeFaultInjection:
    def test_returns_unknown_not_synced_when_probe_times_out(
        self, golden_and_activated, caplog, monkeypatch
    ):
        """Discriminating case: a real unmerged path exists (genuine
        conflict), HEAD equals golden's HEAD, but the conflict probe
        itself times out (simulating a wedged `hard` NFSv3 cow-storage
        mount -- CLAUDE.md documents this as able to block FOREVER).
        compute_sync_status must report sync_status=None ("unknown"), NOT
        "synced" -- it genuinely cannot tell whether the working tree is
        clean.
        """
        arm, _golden_path, activated_path = golden_and_activated
        _inject_unmerged_index_entry(activated_path, "file.txt")
        _fail_conflict_probe(monkeypatch, mode="timeout")

        with caplog.at_level(logging.WARNING):
            result = arm.compute_sync_status("user1", "myrepo")

        assert result["sync_status"] is None, (
            f"Expected 'unknown' (None) when the conflict probe times out, "
            f"got {result['sync_status']!r} -- this is the exact "
            f"fabricated-'synced' bug the fix must close"
        )
        assert result["has_conflicts"] is False
        _assert_warning_logged(caplog)

    def test_returns_unknown_not_synced_when_probe_exits_nonzero(
        self, golden_and_activated, caplog, monkeypatch
    ):
        """Same discriminating scenario, but the conflict probe fails via
        a non-zero git exit code instead of a timeout."""
        arm, _golden_path, activated_path = golden_and_activated
        _inject_unmerged_index_entry(activated_path, "file.txt")
        _fail_conflict_probe(monkeypatch, mode="returncode")

        with caplog.at_level(logging.WARNING):
            result = arm.compute_sync_status("user1", "myrepo")

        assert result["sync_status"] is None, (
            f"Expected 'unknown' (None) when the conflict probe exits "
            f"non-zero, got {result['sync_status']!r} -- this is the "
            f"exact fabricated-'synced' bug the fix must close"
        )
        assert result["has_conflicts"] is False
        _assert_warning_logged(caplog)
