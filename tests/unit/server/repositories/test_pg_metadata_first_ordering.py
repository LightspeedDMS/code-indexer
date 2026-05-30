"""Tests for Story #1032 Commit 10 — metadata-before-Phase-2 ordering (opus H2).

Opus review HIGH #2 follow-up: regression test asserting that
`_delete_metadata` runs AFTER `_fd_anchored_phase1_rename` returns and BEFORE
`_safe_purge_trash_entry` starts. This is the invariant that lets PG-mode UI
see the repo as gone the instant Phase 1 completes (because `_list_user_repos_pg`
reads from PG).

The invariant is backend-agnostic — same call order in file mode + PG mode.
We test by mocking the manager-level `_delete_metadata` (the dual-mode helper).
"""

import tempfile
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock, patch

from src.code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)


def _make_manager(tmp_dir: str) -> ActivatedRepoManager:
    return ActivatedRepoManager(
        data_dir=tmp_dir,
        golden_repo_manager=MagicMock(),
        background_job_manager=MagicMock(),
    )


def _seed_single_repo(manager: ActivatedRepoManager, username: str, alias: str) -> dict:
    repo_dir = Path(manager.activated_repos_dir) / username / alias
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "marker.txt").write_text("alive")
    return {
        "user_alias": alias,
        "username": username,
        "path": str(repo_dir),
        "is_composite": False,
    }


def _seed_composite_repo(
    manager: ActivatedRepoManager, username: str, alias: str
) -> dict:
    repo_dir = Path(manager.activated_repos_dir) / username / alias
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "marker.txt").write_text("composite-alive")
    return {
        "user_alias": alias,
        "username": username,
        "path": str(repo_dir),
        "is_composite": True,
        "golden_repo_aliases": [],
        "discovered_repos": [],
    }


class _CallOrderRecorder:
    def __init__(self) -> None:
        self.order: List[str] = []

    def wrap(self, name: str, real_fn: Any) -> Any:
        recorder = self

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            recorder.order.append(name)
            return real_fn(*args, **kwargs)

        return wrapper


def _assert_order(order: List[str]) -> None:
    assert "phase1" in order, f"Phase 1 must be called; order={order}"
    assert "metadata" in order, f"_delete_metadata must be called; order={order}"
    assert "phase2" in order, f"Phase 2 must be called; order={order}"
    p1 = order.index("phase1")
    md = order.index("metadata")
    p2 = order.index("phase2")
    assert p1 < md, (
        f"_delete_metadata (pos {md}) must be AFTER phase1 (pos {p1}); order={order}"
    )
    assert md < p2, (
        f"_delete_metadata (pos {md}) must be BEFORE phase2 (pos {p2}); order={order}"
    )


class TestMetadataDeletedBeforePhase2Single:
    def test_metadata_deleted_before_phase2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = _make_manager(tmp)
            metadata = _seed_single_repo(manager, "alice", "single-repo")

            import src.code_indexer.server.repositories.activated_repo_manager as arm

            real_phase1 = arm._fd_anchored_phase1_rename
            real_phase2 = arm._safe_purge_trash_entry
            real_meta = manager._delete_metadata

            recorder = _CallOrderRecorder()
            with (
                patch.object(
                    arm,
                    "_fd_anchored_phase1_rename",
                    side_effect=recorder.wrap("phase1", real_phase1),
                ),
                patch.object(
                    arm,
                    "_safe_purge_trash_entry",
                    side_effect=recorder.wrap("phase2", real_phase2),
                ),
                patch.object(
                    manager,
                    "_delete_metadata",
                    side_effect=recorder.wrap("metadata", real_meta),
                ),
            ):
                result = manager._do_deactivate_single("alice", "single-repo", metadata)

            assert result["success"] is True
            _assert_order(recorder.order)


class TestMetadataDeletedBeforePhase2Composite:
    def test_metadata_delete_ordering_holds_for_composite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = _make_manager(tmp)
            metadata = _seed_composite_repo(manager, "bob", "composite-repo")

            import src.code_indexer.server.repositories.activated_repo_manager as arm

            real_phase1 = arm._fd_anchored_phase1_rename
            real_phase2 = arm._safe_purge_trash_entry
            real_meta = manager._delete_metadata

            recorder = _CallOrderRecorder()
            with (
                patch.object(
                    arm,
                    "_fd_anchored_phase1_rename",
                    side_effect=recorder.wrap("phase1", real_phase1),
                ),
                patch.object(
                    arm,
                    "_safe_purge_trash_entry",
                    side_effect=recorder.wrap("phase2", real_phase2),
                ),
                patch.object(
                    manager,
                    "_delete_metadata",
                    side_effect=recorder.wrap("metadata", real_meta),
                ),
                patch.object(manager, "_stop_composite_services"),
            ):
                result = manager._do_deactivate_composite(
                    "bob", "composite-repo", metadata
                )

            assert result["success"] is True
            _assert_order(recorder.order)


class TestPhase2NeverRunsBeforeMetadataDelete:
    def test_phase2_not_invoked_until_metadata_call_returns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = _make_manager(tmp)
            metadata = _seed_single_repo(manager, "alice", "single-repo")

            import src.code_indexer.server.repositories.activated_repo_manager as arm

            real_meta = manager._delete_metadata
            real_phase2 = arm._safe_purge_trash_entry

            state = {"meta_returned": False, "phase2_started_too_early": False}

            def meta_wrap(*args, **kwargs):
                result = real_meta(*args, **kwargs)
                state["meta_returned"] = True
                return result

            def phase2_wrap(*args, **kwargs):
                if not state["meta_returned"]:
                    state["phase2_started_too_early"] = True
                return real_phase2(*args, **kwargs)

            with (
                patch.object(arm, "_safe_purge_trash_entry", side_effect=phase2_wrap),
                patch.object(manager, "_delete_metadata", side_effect=meta_wrap),
            ):
                manager._do_deactivate_single("alice", "single-repo", metadata)

            assert not state["phase2_started_too_early"], (
                "REGRESSION: Phase 2 started before _delete_metadata returned"
            )
