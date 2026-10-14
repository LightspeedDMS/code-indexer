"""Activated repositories on a pod without the repository volume.

Under ``content_backend='delegated'`` an activated working copy lives only on
workspace-mcp's storage at the pod's absolute path; git reaches it through
git-mcp; the pod keeps the copy's index state. These tests run the lifecycle
(activate, inspect, branch, sync status, deactivate) twice over ONE upstream
repository -- locally, today's code, and delegated, through the fakes in
``activated_seam_fakes`` -- and require:

* the same result (working tree, HEAD, branch, remotes, index state,
  metadata), and
* under delegated, no local access to any repository path on the pod (the
  guard raises on any open/stat/listing/rmtree, and on any subprocess run in
  or naming one), only to the pod's own ``.code-indexer`` state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoCloneNotStartedError,
    ActivatedRepoError,
    ActivatedRepoManager,
)
from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend
from code_indexer.server.storage.shared.git_backend import LocalGitBackend

from tests.unit.server.activated_seam_fakes import (
    ALIAS,
    INDEX_DIR,
    USER,
    ActivatedWorld,
    LocalIOForbidden,
    _git,
    tree,
)

_VOLATILE_METADATA = ("activated_at", "last_accessed", "activation_id")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ActivatedWorld:
    return ActivatedWorld(tmp_path, monkeypatch)


def _activate(manager: ActivatedRepoManager, branch: str = "main") -> Dict[str, Any]:
    return manager._do_activate_repository(
        username=USER, golden_repo_alias=ALIAS, branch_name=branch, user_alias=ALIAS
    )


def _stable(metadata: Any) -> Dict[str, Any]:
    assert metadata is not None
    return {k: v for k, v in metadata.items() if k not in _VOLATILE_METADATA}


def _stable_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """The activation result minus its timestamp and the deployment's root."""
    details = {k: v for k, v in result["details"].items() if k != "cloned_from"}
    stable = {k: v for k, v in result.items() if k != "activation_timestamp"}
    return {**stable, "details": details}


def _remotes(repo: Path, world_root: Path) -> Dict[str, str]:
    """Remote name -> URL, with the deployment's own root made neutral."""
    names = _git(repo, "remote").split()
    return {
        name: _git(repo, "remote", "get-url", name).replace(str(world_root), "<ROOT>")
        for name in names
    }


class TestActivationParity:
    @pytest.mark.parametrize("branch", ["main", "feature"])
    def test_delegated_activation_matches_local(
        self, world: ActivatedWorld, branch: str
    ) -> None:
        local = world.local_manager()
        local_result = _activate(local, branch)
        local_meta = local._load_metadata(USER, ALIAS)

        delegated = world.delegated_manager()
        with world.forbid_local_io():
            delegated_result = _activate(delegated, branch)
        delegated_meta = delegated._load_metadata(USER, ALIAS)

        local_copy, remote_copy = world.local_copy(), world.remote_copy()
        assert tree(remote_copy) == tree(local_copy) != {}
        assert _git(remote_copy, "rev-parse", "HEAD") == _git(
            local_copy, "rev-parse", "HEAD"
        )
        assert _git(remote_copy, "branch", "--show-current") == branch
        assert _git(local_copy, "branch", "--show-current") == branch
        assert _remotes(remote_copy, world.remote_root) == _remotes(
            local_copy, world.local_data.parent
        )
        assert _git(remote_copy, "config", "user.email") == "dev@example.com"
        # The index state: locally inside the copy, delegated on THIS pod.
        assert tree(world.pod_copy() / INDEX_DIR, skip=()) == tree(
            local_copy / INDEX_DIR, skip=()
        )
        assert not (remote_copy / INDEX_DIR).exists() or tree(
            remote_copy / INDEX_DIR, skip=()
        ) == tree(world.remote_of(world.pod_golden) / INDEX_DIR, skip=())
        assert _stable(delegated_meta) == _stable(local_meta)
        assert _stable_result(delegated_result) == _stable_result(local_result)

    def test_delegated_activation_never_touches_pod_repository_paths(
        self, world: ActivatedWorld
    ) -> None:
        manager = world.delegated_manager()
        with world.forbid_local_io():
            _activate(manager)
        # Nothing but index state was created on the pod for the copy.
        pod_entries = sorted(p.name for p in world.pod_copy().iterdir())
        assert pod_entries == [INDEX_DIR]
        assert world.fix_config_calls == [str(world.pod_copy())]
        clone = [b for r, b in world.workspace_mcp.calls if r == "/clone-path"]
        assert clone == [
            {
                "source_path": str(world.pod_golden),
                "dest_path": str(world.pod_copy()),
                "preserve_attrs": True,
                "if_absent": True,
            }
        ]

    def test_guard_catches_a_local_read(self, world: ActivatedWorld) -> None:
        """The proof has teeth: a local read of the copy's path raises."""
        world.pod_copy().mkdir(parents=True)
        with world.forbid_local_io():
            with pytest.raises(LocalIOForbidden):
                (world.pod_copy() / "README.md").exists()


class TestActivationFailsLoud:
    def test_existing_copy_is_never_replaced(self, world: ActivatedWorld) -> None:
        manager = world.delegated_manager()
        remote = world.remote_copy()
        remote.mkdir(parents=True)
        (remote / "mine.txt").write_text("keep")
        with pytest.raises(ActivatedRepoError):
            _activate(manager)
        assert (remote / "mine.txt").read_text() == "keep"
        assert "/clone-path/delete" not in world.workspace_mcp.routes()
        assert manager._load_metadata(USER, ALIAS) is None

    def test_failed_clone_raises_and_registers_nothing(
        self, world: ActivatedWorld
    ) -> None:
        manager = world.delegated_manager()
        world.workspace_mcp.fail["/clone-path"] = 500
        with pytest.raises(ActivatedRepoError, match="HTTP 500"):
            _activate(manager)
        assert manager._load_metadata(USER, ALIAS) is None
        assert not world.remote_copy().exists()

    def test_failure_after_clone_removes_the_copy_and_index_state(
        self, world: ActivatedWorld
    ) -> None:
        manager = world.delegated_manager()
        with pytest.raises(ActivatedRepoError):
            _activate(manager, branch="no-such-branch")
        assert not world.remote_copy().exists()
        assert not world.pod_copy().exists()
        assert manager._load_metadata(USER, ALIAS) is None

    def test_missing_golden_index_on_this_pod_is_an_error(
        self, world: ActivatedWorld
    ) -> None:
        import shutil

        shutil.rmtree(world.pod_golden / INDEX_DIR)
        manager = world.delegated_manager()
        with pytest.raises(ActivatedRepoError, match="index is not on this server"):
            _activate(manager)
        assert not world.remote_copy().exists()

    def test_local_git_backend_is_refused(self, world: ActivatedWorld) -> None:
        manager = world.delegated_manager(git_backend=LocalGitBackend())
        with pytest.raises(ActivatedRepoCloneNotStartedError, match="git_backend"):
            manager._clone_with_copy_on_write(
                str(world.pod_golden), str(world.pod_copy()), golden_repo_alias=ALIAS
            )
        assert world.workspace_mcp.calls == []

    def test_non_delegated_clone_backend_is_refused(
        self, world: ActivatedWorld
    ) -> None:
        manager = world.delegated_manager(clone_backend=LocalCloneBackend())
        with pytest.raises(ActivatedRepoError, match="clone_backend"):
            manager._working_copy_exists(str(world.pod_copy()))

    def test_unreachable_workspace_mcp_is_not_absence(
        self, world: ActivatedWorld
    ) -> None:
        manager = world.delegated_manager()
        _activate(manager)
        world.workspace_mcp.fail["/clone-path/stat"] = 503
        with pytest.raises(Exception, match="HTTP 503"):
            manager.get_current_branch(USER, ALIAS)

    def test_composite_activation_is_refused(self, world: ActivatedWorld) -> None:
        manager = world.delegated_manager()
        with pytest.raises(ActivatedRepoError, match="Composite"):
            manager._activate_composite_repository(USER, [ALIAS, "other"], "combo")


class TestInspection:
    def test_branch_listing_and_current_branch_match_local(
        self, world: ActivatedWorld
    ) -> None:
        local = world.local_manager()
        _activate(local)
        local_branches = local.list_repository_branches(USER, ALIAS)
        local_current = local.get_current_branch(USER, ALIAS)

        delegated = world.delegated_manager()
        _activate(delegated)
        with world.forbid_local_io():
            branches = delegated.list_repository_branches(USER, ALIAS)
            current = delegated.get_current_branch(USER, ALIAS)

        assert current == local_current == "main"
        assert branches == local_branches
        assert any(b.get("last_commit_hash") for b in branches["branches"])

    def test_sync_status_matches_local(self, world: ActivatedWorld) -> None:
        local = world.local_manager()
        _activate(local)
        expected = local.compute_sync_status(USER, ALIAS)
        delegated = world.delegated_manager()
        _activate(delegated)
        with world.forbid_local_io():
            status = delegated.compute_sync_status(USER, ALIAS)
        assert {k: v for k, v in status.items() if k != "last_sync_time"} == {
            k: v for k, v in expected.items() if k != "last_sync_time"
        }

    def test_conflicts_are_detected_through_git(self, world: ActivatedWorld) -> None:
        delegated = world.delegated_manager()
        _activate(delegated)
        remote = world.remote_copy()
        _git(remote, "checkout", "-q", "-b", "left")
        (remote / "README.md").write_text("left\n")
        _git(remote, "commit", "-qam", "left")
        _git(remote, "checkout", "-q", "main")
        (remote / "README.md").write_text("right\n")
        _git(remote, "commit", "-qam", "right")
        import subprocess

        subprocess.run(["git", "merge", "left"], cwd=remote, capture_output=True)
        with world.forbid_local_io():
            has_conflicts, details = delegated._detect_conflict_paths(
                str(world.pod_copy()), USER, ALIAS
            )
        assert has_conflicts is True
        assert details == "Unmerged paths: README.md"

    def test_rev_parse_miss_is_none(self, world: ActivatedWorld) -> None:
        delegated = world.delegated_manager()
        _activate(delegated)
        with world.forbid_local_io():
            head = delegated._rev_parse_ref(str(world.pod_copy()), "HEAD", USER, ALIAS)
            miss = delegated._rev_parse_ref(
                str(world.pod_copy()), "refs/heads/nope", USER, ALIAS
            )
        assert head == _git(world.remote_copy(), "rev-parse", "HEAD")
        assert miss is None

    def test_listing_reports_existence_from_workspace_mcp(
        self, world: ActivatedWorld
    ) -> None:
        delegated = world.delegated_manager()
        _activate(delegated)
        with world.forbid_local_io():
            mine = delegated.list_activated_repositories(USER)
            everyone = delegated.list_all_activated_repositories()
            by_golden = delegated.find_repos_by_golden_alias(ALIAS)
        assert [r["user_alias"] for r in mine] == [ALIAS]
        assert [r["path_exists"] for r in everyone] == [True]
        assert by_golden == [
            {"username": USER, "user_alias": ALIAS, "is_composite": False}
        ]

    def test_copy_removed_out_from_under_registration_is_not_found(
        self, world: ActivatedWorld
    ) -> None:
        delegated = world.delegated_manager()
        _activate(delegated)
        import shutil

        shutil.rmtree(world.remote_copy())
        with pytest.raises(ActivatedRepoError, match="not found"):
            delegated.get_current_branch(USER, ALIAS)
        assert delegated.list_activated_repositories(USER) == []


class TestBranchSwitch:
    def test_switch_branch_matches_local(self, world: ActivatedWorld) -> None:
        local = world.local_manager()
        _activate(local)
        local_result = local.switch_branch(USER, ALIAS, "feature")

        delegated = world.delegated_manager()
        _activate(delegated)
        with world.forbid_local_io():
            result = delegated.switch_branch(USER, ALIAS, "feature")

        assert result == local_result
        assert _git(world.remote_copy(), "branch", "--show-current") == "feature"
        assert tree(world.remote_copy()) == tree(world.local_copy())


class TestDeactivation:
    def test_deactivation_removes_copy_index_state_and_registration(
        self, world: ActivatedWorld
    ) -> None:
        delegated = world.delegated_manager()
        _activate(delegated)
        with world.forbid_local_io():
            result = delegated._do_deactivate_repository(USER, ALIAS)
        assert result["success"] is True
        assert result["administrative_notes"]["cleanup_verified"] is True
        assert result["resource_summary"]["directories_removed"] == 1
        assert not world.remote_copy().exists()
        assert not world.pod_copy().exists()
        assert delegated._load_metadata(USER, ALIAS) is None
        assert world.remote_of(world.pod_golden).is_dir()  # golden untouched

    def test_failed_removal_keeps_the_registration(self, world: ActivatedWorld) -> None:
        delegated = world.delegated_manager()
        _activate(delegated)
        world.workspace_mcp.fail["/clone-path/delete"] = 500
        with pytest.raises(ActivatedRepoError, match="HTTP 500"):
            delegated._do_deactivate_repository(USER, ALIAS)
        assert world.remote_copy().is_dir()
        assert delegated._load_metadata(USER, ALIAS) is not None
        assert (world.pod_copy() / INDEX_DIR).is_dir()

    def test_orphan_copy_without_registration_is_cleaned(
        self, world: ActivatedWorld
    ) -> None:
        delegated = world.delegated_manager()
        world.remote_copy().mkdir(parents=True)
        (world.remote_copy() / "leftover").write_text("x")
        with world.forbid_local_io():
            delegated.deactivate_repository(USER, ALIAS)  # front door accepts it
            result = delegated._do_deactivate_repository(USER, ALIAS)
        assert "already deactivated" in result["message"]
        assert not world.remote_copy().exists()

    def test_nothing_anywhere_is_not_found(self, world: ActivatedWorld) -> None:
        delegated = world.delegated_manager()
        with pytest.raises(ActivatedRepoError, match="not found"):
            delegated.deactivate_repository(USER, ALIAS)

    def test_reactivation_after_deactivation(self, world: ActivatedWorld) -> None:
        delegated = world.delegated_manager()
        _activate(delegated)
        delegated._do_deactivate_repository(USER, ALIAS)
        with world.forbid_local_io():
            _activate(delegated, "feature")
        assert _git(world.remote_copy(), "branch", "--show-current") == "feature"


class TestLocalDefaultUnchanged:
    def test_local_activation_never_calls_workspace_mcp(
        self, world: ActivatedWorld
    ) -> None:
        local = world.local_manager()
        _activate(local)
        local._do_deactivate_repository(USER, ALIAS)
        assert world.workspace_mcp.calls == []
        assert world.git_mcp.calls == []
        assert not world.local_copy().exists()
