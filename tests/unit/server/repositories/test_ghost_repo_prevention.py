"""Tests for Story #1032 Commit 8 — ghost-repo prevention (codex NEW HIGH fix).

Codex GPT-5 re-re-review of v10.80.0 found: when `_fd_anchored_phase1_rename`
fails (e.g. .trash swapped to symlink → raises ValueError), the deactivation
methods previously called `_delete_metadata` unconditionally afterwards. Result:
live repo dir still on disk in {username}/{user_alias}, but metadata gone →
the UI shows "deactivated" while the bytes remain hidden-but-alive (ghost).

Commit 8 added an `if repo_dir/repo_path exists` guard around the outer
metadata delete in both `_do_deactivate_single` and `_do_deactivate_composite`.
This file locks that invariant in.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)


@pytest.fixture
def manager():
    with tempfile.TemporaryDirectory() as tmp:
        m = ActivatedRepoManager(
            data_dir=tmp,
            golden_repo_manager=MagicMock(),
            background_job_manager=MagicMock(),
        )
        yield m


def _make_single_repo(manager: ActivatedRepoManager, username: str, alias: str):
    repo_dir = Path(manager.activated_repos_dir) / username / alias
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "marker.txt").write_text("alive")
    meta_dir = Path(manager.activated_repos_dir) / username
    meta_file = meta_dir / f"{alias}_metadata.json"
    meta_file.write_text(
        json.dumps(
            {
                "user_alias": alias,
                "username": username,
                "path": str(repo_dir),
                "is_composite": False,
            }
        )
    )
    return str(repo_dir), str(meta_file)


def _make_composite_repo(manager: ActivatedRepoManager, username: str, alias: str):
    repo_dir = Path(manager.activated_repos_dir) / username / alias
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "marker.txt").write_text("composite-alive")
    meta_dir = Path(manager.activated_repos_dir) / username
    meta_file = meta_dir / f"{alias}_metadata.json"
    meta_file.write_text(
        json.dumps(
            {
                "user_alias": alias,
                "username": username,
                "path": str(repo_dir),
                "is_composite": True,
            }
        )
    )
    return str(repo_dir), str(meta_file)


class TestGhostRepoPreventionSingle:
    """When Phase 1 rename fails, single-path metadata MUST be preserved."""

    def test_metadata_preserved_when_phase1_fails(self, manager):
        repo_dir, meta_file = _make_single_repo(manager, "alice", "ghost-test")
        metadata = {
            "user_alias": "ghost-test",
            "path": repo_dir,
            "is_composite": False,
            "username": "alice",
        }

        def fail_rename(*args, **kwargs):
            raise OSError("simulated rename failure (e.g. trash swap)")

        with patch(
            "src.code_indexer.server.repositories.activated_repo_manager._fd_anchored_phase1_rename",
            side_effect=fail_rename,
        ):
            result = manager._do_deactivate_single("alice", "ghost-test", metadata)

        # Both must still be on disk — no ghost state
        assert os.path.exists(repo_dir), (
            "GHOST REPO: live repo dir was removed despite Phase 1 failure"
        )
        assert os.path.exists(meta_file), (
            "GHOST REPO: metadata was deleted while repo dir still on disk — "
            "UI would show 'deactivated' while bytes remain"
        )
        # Result must surface the warning so admin knows
        warnings = result.get("warnings") or result.get("cleanup_warnings") or []
        assert any("ghost" in w.lower() or "phase 1" in w.lower() for w in warnings), (
            f"Expected ghost-prevention warning in cleanup_warnings; got: {warnings}"
        )


class TestGhostRepoPreventionComposite:
    """When Phase 1 rename fails on composite, composite metadata MUST be preserved."""

    def test_composite_metadata_preserved_when_phase1_fails(self, manager):
        repo_dir, meta_file = _make_composite_repo(manager, "alice", "ghost-composite")
        metadata = {
            "user_alias": "ghost-composite",
            "path": repo_dir,
            "is_composite": True,
            "username": "alice",
        }

        def fail_rename(*args, **kwargs):
            raise OSError("simulated composite rename failure")

        with patch(
            "src.code_indexer.server.repositories.activated_repo_manager._fd_anchored_phase1_rename",
            side_effect=fail_rename,
        ):
            with patch.object(manager, "_stop_composite_services"):
                result = manager._do_deactivate_composite(
                    "alice", "ghost-composite", metadata
                )

        assert os.path.exists(repo_dir), (
            "GHOST COMPOSITE: live composite dir removed despite Phase 1 failure"
        )
        assert os.path.exists(meta_file), (
            "GHOST COMPOSITE: metadata deleted while composite dir still on disk"
        )
        warnings = result.get("warnings") or result.get("cleanup_warnings") or []
        assert any("ghost" in w.lower() or "phase 1" in w.lower() for w in warnings), (
            f"Expected ghost-prevention warning in cleanup_warnings; got: {warnings}"
        )


class TestNoRegressionWhenPhase1Succeeds:
    """When Phase 1 succeeds normally, metadata IS deleted (no regression)."""

    def test_metadata_deleted_on_normal_success(self, manager):
        repo_dir, meta_file = _make_single_repo(manager, "alice", "normal-test")
        metadata = {
            "user_alias": "normal-test",
            "path": repo_dir,
            "is_composite": False,
            "username": "alice",
        }
        # No patches — real Phase 1 + real Phase 2 run.
        result = manager._do_deactivate_single("alice", "normal-test", metadata)
        assert result["success"] is True
        assert not os.path.exists(repo_dir), "repo dir should be gone after success"
        assert not os.path.exists(meta_file), "metadata should be deleted after success"
