"""Composite activated repositories on a pod without the repository volume.

A composite is ``activated-repos/<user>/<alias>/`` holding one working copy per
member. Under ``content_backend='delegated'`` the composite and its members live
only on workspace-mcp's storage (each member created by the single-activation
delegated clone), the composite's proxy config and every member's index are
THIS pod's index state, and listings/details read the members through the
content seam's manifest.

Each test runs the same operation locally (today's code) and delegated (fakes
from ``activated_seam_fakes``) and requires the same result, with every local
access to a pod repository path -- anything but index state -- raising.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union, cast
from unittest.mock import MagicMock

import pytest

from code_indexer.server import app_helpers
from code_indexer.server.managers import composite_file_listing
from code_indexer.server.managers.composite_file_listing import _list_composite_files
from code_indexer.server.models.activated_repository import ActivatedRepository
from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoError,
    ActivatedRepoManager,
)
from code_indexer.server.repositories.golden_repo_manager import GoldenRepo
from code_indexer.server.services.composite_content import (
    CompositeMemberUnreadableError,
)
from code_indexer.server.startup import content_backend_wiring
from code_indexer.server.storage.shared.content_backend import (
    ContentPage,
    LocalContentBackend,
)
from code_indexer.server.storage.shared.git_backend import LocalGitBackend
from code_indexer.server.utils.config_manager import ServerResourceConfig

from tests.unit.server.activated_seam_fakes import (
    _POD_STATE_NAMES,
    ALIAS,
    INDEX_DIR,
    USER,
    ActivatedWorld,
    _git,
    tree,
)

OTHER = "other-repo"
MEMBERS = [OTHER, ALIAS]  # deliberately unsorted: discovery lists them sorted
COMPOSITE = "combo"
_VOLATILE = ("activated_at", "last_accessed", "activation_id")
_OVERRIDE = ".code-indexer-override.yaml"
_WHEN = datetime(2024, 1, 1, tzinfo=timezone.utc)


class RemoteContentBackend:
    """The content seam over the remote tree, addressed by POD path, like
    workspace-mcp: a missing root is an error, never an empty manifest."""

    is_local = False

    def __init__(self, world: "CompositeWorld") -> None:
        self._world = world
        self._inner = LocalContentBackend()
        self.calls: List[str] = []
        self.fail = False
        self.drop_mtime = False
        self.ignore_prune = False

    def manifest(
        self,
        repo_path: Union[str, Path],
        *,
        cursor: Optional[str] = None,
        page_size: int = 1000,
        prune_dirs: Optional[Sequence[str]] = None,
    ) -> ContentPage:
        self.calls.append(str(repo_path))
        if self.fail:
            raise ConnectionError("workspace-mcp unreachable")
        root = self._world.remote_of(repo_path)
        if not root.is_dir():
            raise FileNotFoundError(f"source_path does not exist: {repo_path}")
        page = self._inner.manifest(
            root,
            cursor=cursor,
            page_size=2,
            prune_dirs=None if self.ignore_prune else prune_dirs,
        )
        if self.drop_mtime:
            from dataclasses import replace

            page = ContentPage(
                entries=tuple(replace(e, mtime_ns=None) for e in page.entries),
                next_cursor=page.next_cursor,
            )
        return page

    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Any:
        raise AssertionError("composite listing never fetches bytes")


class CompositeWorld(ActivatedWorld):
    """ActivatedWorld with a second golden repository and composite paths."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        super().__init__(tmp_path, monkeypatch)
        self.local_other = self.local_data / "golden-repos" / OTHER
        self._clone_golden(self.local_other)
        self._write_index(self.local_other / INDEX_DIR)
        self.pod_other = self.pod_data / "golden-repos" / OTHER
        self._clone_golden(self.remote_of(self.pod_other))
        self._write_index(self.pod_other / INDEX_DIR)
        self.content = RemoteContentBackend(self)

    def _golden_manager(self, golden_path: Path) -> MagicMock:
        golden_root = golden_path.parent

        def golden(alias: str) -> Optional[GoldenRepo]:
            if alias not in (ALIAS, OTHER):
                return None
            return GoldenRepo(
                alias=alias,
                repo_url=str(self.upstream),
                default_branch="main",
                clone_path=str(golden_root / alias),
                created_at="2024-01-01T00:00:00Z",
            )

        manager = MagicMock()
        manager.get_golden_repo.side_effect = golden
        manager.get_actual_repo_path.side_effect = lambda a: str(golden_root / a)
        manager.resource_config = ServerResourceConfig()
        manager._refresh_scheduler = None
        return manager

    def delegated(self) -> None:
        self._monkeypatch.setattr(
            content_backend_wiring, "server_content_backend", lambda: self.content
        )

    def _forbidden(self, value: Any) -> bool:
        if not super()._forbidden(value):
            return False
        full = os.path.abspath(os.fspath(value))
        other = str(self.pod_other)
        if full == other or full.startswith(other + os.sep):
            rel = Path(full).relative_to(other).parts
            return not (rel and rel[0] == INDEX_DIR)
        user_root = str(self.pod_data / "activated-repos" / USER)
        if full.startswith(user_root + os.sep):
            rel = Path(full).relative_to(user_root).parts
            # <composite>/<member>/.code-indexer is pod state too.
            if len(rel) >= 3 and rel[0] == COMPOSITE and rel[2] in _POD_STATE_NAMES:
                return False
            # ... and so are the IndexStore checkout's markers / lock beside it
            if (
                len(rel) == 2
                and rel[0] == COMPOSITE
                and (rel[1] in _POD_STATE_NAMES or rel[1].endswith(".checkout.lock"))
            ):
                return False
            # The composite directory is not a repository (no files of its
            # own on workspace-mcp but the members): on this pod it is the
            # directory holding the proxy config. Loading that config resolves
            # it and looks for an override file in it, as for any index config.
            # (The override lookup walks up through the user's directory.)
            if rel in ((COMPOSITE,), (COMPOSITE, _OVERRIDE), (_OVERRIDE,)):
                return False
        return True


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CompositeWorld:
    return CompositeWorld(tmp_path, monkeypatch)


def _activate(manager: ActivatedRepoManager) -> Dict[str, Any]:
    return manager._do_activate_composite_repository(
        username=USER, golden_repo_aliases=list(MEMBERS), user_alias=COMPOSITE
    )


def _neutral(value: Any, root: Path) -> Dict[str, Any]:
    neutral: Dict[str, Any] = json.loads(json.dumps(value).replace(str(root), "<ROOT>"))
    return neutral


def _stable(metadata: Any, root: Path) -> Dict[str, Any]:
    assert metadata is not None
    return _neutral({k: v for k, v in metadata.items() if k not in _VOLATILE}, root)


def _stable_result(result: Dict[str, Any], root: Path) -> Dict[str, Any]:
    return _neutral(
        {k: v for k, v in result.items() if k != "activation_timestamp"}, root
    )


def _config(index_dir: Path, root: Path) -> Any:
    return _neutral(json.loads((index_dir / "config.json").read_text()), root)


def _repo(world: CompositeWorld, path: Path) -> ActivatedRepository:
    return ActivatedRepository(
        user_alias=COMPOSITE,
        username=USER,
        path=path,
        activated_at=_WHEN,
        last_accessed=_WHEN,
        is_composite=True,
        golden_repo_aliases=list(MEMBERS),
        discovered_repos=sorted(MEMBERS),
    )


def _pod_composite(world: CompositeWorld) -> Path:
    return world.pod_copy(COMPOSITE)


def _remote_composite(world: CompositeWorld) -> Path:
    return world.remote_copy(COMPOSITE)


def _local_composite(world: CompositeWorld) -> Path:
    return world.local_copy(COMPOSITE)


class TestActivationParity:
    def test_delegated_composite_activation_matches_local(
        self, world: CompositeWorld
    ) -> None:
        local = world.local_manager()
        local_result = _activate(local)
        local_meta = local._load_metadata(USER, COMPOSITE)

        delegated = world.delegated_manager()
        with world.forbid_local_io():
            delegated_result = _activate(delegated)
        delegated_meta = delegated._load_metadata(USER, COMPOSITE)

        local_root, pod_root = world.local_data, world.pod_data
        assert _stable_result(delegated_result, pod_root) == _stable_result(
            local_result, local_root
        )
        assert _stable(delegated_meta, pod_root) == _stable(local_meta, local_root)
        assert delegated_meta is not None
        assert delegated_meta["discovered_repos"] == sorted(MEMBERS)
        # The composite's proxy config: in the composite locally, pod state here.
        assert _config(_pod_composite(world) / INDEX_DIR, pod_root) == _config(
            _local_composite(world) / INDEX_DIR, local_root
        )
        for member in MEMBERS:
            local_member = _local_composite(world) / member
            remote_member = _remote_composite(world) / member
            assert tree(remote_member) == tree(local_member) != {}
            assert _git(remote_member, "rev-parse", "HEAD") == _git(
                local_member, "rev-parse", "HEAD"
            )
            assert tree(_pod_composite(world) / member / INDEX_DIR, skip=()) == tree(
                local_member / INDEX_DIR, skip=()
            )

    def test_pod_holds_only_index_state(self, world: CompositeWorld) -> None:
        delegated = world.delegated_manager()
        with world.forbid_local_io():
            _activate(delegated)
        pod = _pod_composite(world)
        assert sorted(p.name for p in pod.iterdir()) == sorted([INDEX_DIR, *MEMBERS])
        for member in MEMBERS:
            assert [p.name for p in (pod / member).iterdir()] == [INDEX_DIR]
        assert not (_remote_composite(world) / INDEX_DIR).exists()
        clones = [b for r, b in world.workspace_mcp.calls if r == "/clone-path"]
        assert [(b["source_path"], b["dest_path"]) for b in clones] == [
            (str(world.pod_data / "golden-repos" / m), str(pod / m)) for m in MEMBERS
        ]


class TestActivationFailsLoud:
    def test_existing_composite_on_workspace_mcp_is_never_replaced(
        self, world: CompositeWorld
    ) -> None:
        manager = world.delegated_manager()
        remote = _remote_composite(world)
        remote.mkdir(parents=True)
        (remote / "mine.txt").write_text("keep")
        with pytest.raises(ActivatedRepoError, match="already exists"):
            _activate(manager)
        assert (remote / "mine.txt").read_text() == "keep"
        assert "/clone-path" not in world.workspace_mcp.routes()
        assert "/clone-path/delete" not in world.workspace_mcp.routes()
        assert manager._load_metadata(USER, COMPOSITE) is None

    def test_unreachable_workspace_mcp_is_not_absence(
        self, world: CompositeWorld
    ) -> None:
        manager = world.delegated_manager()
        world.workspace_mcp.fail["/clone-path/stat"] = 503
        with pytest.raises(ActivatedRepoError, match="503"):
            _activate(manager)
        assert "/clone-path" not in world.workspace_mcp.routes()

    def test_failed_member_rolls_back_every_member_and_index_state(
        self, world: CompositeWorld
    ) -> None:
        shutil.rmtree(world.pod_golden / INDEX_DIR)  # second member cannot seed
        manager = world.delegated_manager()
        with pytest.raises(ActivatedRepoError, match="index is not on this server"):
            _activate(manager)
        assert not _remote_composite(world).exists()
        assert not _pod_composite(world).exists()
        assert manager._load_metadata(USER, COMPOSITE) is None

    def test_failed_registration_rolls_back(
        self, world: CompositeWorld, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = world.delegated_manager()

        def refuse(*_args: Any, **_kwargs: Any) -> None:
            raise IOError("registry down")

        monkeypatch.setattr(manager, "_save_metadata", refuse)
        with pytest.raises(ActivatedRepoError, match="registry down"):
            _activate(manager)
        assert not _remote_composite(world).exists()
        assert not _pod_composite(world).exists()

    def test_misconfigured_seam_refuses_before_queueing(
        self, world: CompositeWorld
    ) -> None:
        manager = world.delegated_manager(git_backend=LocalGitBackend())
        with pytest.raises(ActivatedRepoError, match="git_backend"):
            manager._activate_composite_repository(USER, list(MEMBERS), COMPOSITE)
        cast(MagicMock, manager.background_job_manager).submit_job.assert_not_called()

    def test_delegated_composite_is_queued(self, world: CompositeWorld) -> None:
        manager = world.delegated_manager()
        manager._activate_composite_repository(USER, list(MEMBERS), COMPOSITE)
        submit = cast(MagicMock, manager.background_job_manager).submit_job
        assert submit.call_args.args[0] == "activate_composite_repository"


class TestDeactivation:
    def test_removes_composite_every_member_index_and_registration(
        self, world: CompositeWorld
    ) -> None:
        manager = world.delegated_manager()
        _activate(manager)
        with world.forbid_local_io():
            result = manager._do_deactivate_repository(USER, COMPOSITE)
        assert result["success"] is True
        assert not _remote_composite(world).exists()
        assert not _pod_composite(world).exists()
        assert manager._load_metadata(USER, COMPOSITE) is None
        for golden in (world.pod_golden, world.pod_other):
            assert world.remote_of(golden).is_dir()  # goldens untouched

    def test_orphan_composite_without_registration_is_cleaned(
        self, world: CompositeWorld
    ) -> None:
        manager = world.delegated_manager()
        _activate(manager)
        manager._delete_metadata(USER, COMPOSITE)
        with world.forbid_local_io():
            result = manager._do_deactivate_repository(USER, COMPOSITE)
        assert "already deactivated" in result["message"]
        assert not _remote_composite(world).exists()
        assert not _pod_composite(world).exists()

    def test_unsafe_member_name_never_reaches_outside(
        self, world: CompositeWorld
    ) -> None:
        manager = world.delegated_manager()
        _activate(manager)
        victim = world.pod_data / "activated-repos" / USER / "victim" / INDEX_DIR
        victim.mkdir(parents=True)
        config = _pod_composite(world) / INDEX_DIR / "config.json"
        data = json.loads(config.read_text())
        data["discovered_repos"] = ["../victim", *data["discovered_repos"]]
        config.write_text(json.dumps(data))
        manager._do_deactivate_repository(USER, COMPOSITE)
        assert victim.is_dir()
        assert not _pod_composite(world).exists()


class TestListing:
    """Delegated listing (manifest) vs a local walk of the SAME files."""

    def _local_listing(
        self, world: CompositeWorld, path: str, recursive: bool
    ) -> List[Dict[str, Any]]:
        remote = _remote_composite(world)
        shutil.copytree(world.tmp / "proxy-config", remote / INDEX_DIR)
        try:
            world._monkeypatch.setattr(
                content_backend_wiring, "server_content_backend", lambda: None
            )
            files = _list_composite_files(_repo(world, remote), path, recursive)
            return [f.model_dump() for f in files]
        finally:
            shutil.rmtree(remote / INDEX_DIR)
            world.delegated()

    def _delegated_listing(
        self, world: CompositeWorld, path: str, recursive: bool
    ) -> List[Dict[str, Any]]:
        with world.forbid_local_io():
            files = _list_composite_files(
                _repo(world, _pod_composite(world)), path, recursive
            )
        return [f.model_dump() for f in files]

    @pytest.fixture
    def activated(self, world: CompositeWorld) -> CompositeWorld:
        _activate(world.delegated_manager())
        # A nested file, so recursion and directory roll-up are exercised.
        nested = _remote_composite(world) / ALIAS / "src" / "pkg"
        nested.mkdir()
        (nested / "deep.py").write_text("x = 1\n")
        # The listing never needs this pod's state for the composite (kept
        # aside only for the LOCAL walk the delegated one is compared with).
        shutil.copytree(_pod_composite(world) / INDEX_DIR, world.tmp / "proxy-config")
        shutil.rmtree(_pod_composite(world))
        return world

    @pytest.mark.parametrize("path", ["", "src", "src/pkg", "no-such-dir"])
    def test_recursive_matches_local(
        self, activated: CompositeWorld, path: str
    ) -> None:
        local = self._local_listing(activated, path, recursive=True)
        delegated = self._delegated_listing(activated, path, recursive=True)
        assert delegated == local
        assert path == "no-such-dir" or local

    @pytest.mark.parametrize("path", ["", "src"])
    def test_single_level_matches_local_except_directory_times(
        self, activated: CompositeWorld, path: str
    ) -> None:
        local = self._local_listing(activated, path, recursive=False)
        delegated = self._delegated_listing(activated, path, recursive=False)

        def strip(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            return [
                {
                    k: v
                    for k, v in r.items()
                    if not (r["is_directory"] and k == "modified")
                }
                for r in rows
            ]

        assert strip(delegated) == strip(local)
        assert any(r["is_directory"] for r in delegated)
        recursive = self._delegated_listing(activated, path, recursive=True)
        for row in delegated:
            if row["is_directory"]:
                beneath = [
                    r["modified"]
                    for r in recursive
                    if r["full_path"].startswith(row["full_path"] + "/")
                ]
                assert beneath and row["modified"] == max(beneath)

    def test_member_missing_on_workspace_mcp_raises(
        self, activated: CompositeWorld
    ) -> None:
        shutil.rmtree(_remote_composite(activated) / OTHER)
        with pytest.raises(CompositeMemberUnreadableError, match=OTHER):
            self._delegated_listing(activated, "", True)

    @pytest.mark.parametrize("recursive", [True, False])
    def test_index_dirs_are_hidden_even_if_the_backend_does_not_prune(
        self, activated: CompositeWorld, recursive: bool
    ) -> None:
        """A workspace-mcp predating prune_dirs still lists no index state."""
        stray = _remote_composite(activated) / ALIAS / "src" / INDEX_DIR
        stray.mkdir()
        (stray / "config.json").write_text("{}")
        activated.content.ignore_prune = True
        rows = self._delegated_listing(activated, "", recursive)
        assert rows and not any(INDEX_DIR in r["full_path"] for r in rows)

    def test_backend_failure_raises(self, activated: CompositeWorld) -> None:
        activated.content.fail = True
        with pytest.raises(CompositeMemberUnreadableError, match="unreachable"):
            self._delegated_listing(activated, "", False)

    def test_unknown_mtime_raises(self, activated: CompositeWorld) -> None:
        activated.content.drop_mtime = True
        with pytest.raises(CompositeMemberUnreadableError, match="modification time"):
            self._delegated_listing(activated, "", True)

    @pytest.mark.parametrize("path", ["../other-user", "/etc", "src/../../x"])
    def test_escaping_path_is_refused(
        self, activated: CompositeWorld, path: str
    ) -> None:
        with pytest.raises(ValueError):
            self._delegated_listing(activated, path, True)
        assert activated.content.calls == []


class TestDetails:
    def test_details_from_manifest_and_pod_index_state(
        self, world: CompositeWorld
    ) -> None:
        _activate(world.delegated_manager())
        pod, remote = _pod_composite(world), _remote_composite(world)
        meta = pod / ALIAS / INDEX_DIR / "metadata.json"
        meta.write_text(
            json.dumps({"indexed_files": 7, "last_indexed": "2024-02-03T04:05:06"})
        )
        with world.forbid_local_io():
            details = app_helpers._get_composite_details(_repo(world, pod))

        by_name = {c.name: c for c in details.component_repositories}
        assert sorted(by_name) == sorted(MEMBERS)
        assert by_name[ALIAS].indexed_files == 7
        assert by_name[ALIAS].last_indexed is not None
        assert details.total_files == 7
        for member in MEMBERS:
            files = sum(len(b) for b in tree(remote / member).values())
            index = sum(
                len(b) for b in tree(pod / member / INDEX_DIR, skip=()).values()
            )
            assert by_name[member].has_index is True
            assert by_name[member].size_mb == pytest.approx(
                (files + index) / (1024 * 1024)
            )
            assert by_name[member].path == str(pod / member)

    def test_index_facts_match_local(self, world: CompositeWorld) -> None:
        """Everything but size (.git is not in the manifest) matches the
        local analysis of the same member."""
        _activate(world.delegated_manager())
        pod, remote = _pod_composite(world), _remote_composite(world)
        shutil.copytree(pod / ALIAS / INDEX_DIR, remote / ALIAS / INDEX_DIR)
        local = app_helpers._analyze_component_repo(remote / ALIAS, ALIAS)
        shutil.rmtree(remote / ALIAS / INDEX_DIR)
        with world.forbid_local_io():
            details = app_helpers._get_composite_details(_repo(world, pod))
        delegated = {c.name: c for c in details.component_repositories}[ALIAS]
        assert delegated.name == local.name == ALIAS
        for field in (
            "has_index",
            "collection_exists",
            "indexed_files",
            "last_indexed",
        ):
            assert getattr(delegated, field) == getattr(local, field)

    def test_member_missing_on_workspace_mcp_raises(
        self, world: CompositeWorld
    ) -> None:
        _activate(world.delegated_manager())
        shutil.rmtree(_remote_composite(world) / ALIAS)
        with pytest.raises(CompositeMemberUnreadableError, match=ALIAS):
            app_helpers._get_composite_details(_repo(world, _pod_composite(world)))

    def test_backend_failure_raises(self, world: CompositeWorld) -> None:
        _activate(world.delegated_manager())
        world.content.fail = True
        with pytest.raises(CompositeMemberUnreadableError):
            app_helpers._get_composite_details(_repo(world, _pod_composite(world)))


class TestLocalDefaultUnchanged:
    def test_local_activation_and_reads_never_use_a_seam(
        self, world: CompositeWorld, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_seam(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("local composite consulted the content seam")

        monkeypatch.setattr(composite_file_listing, "member_entries", no_seam)
        monkeypatch.setattr(app_helpers, "member_entries", no_seam)
        local = world.local_manager()
        _activate(local)
        composite = _local_composite(world)
        repo = _repo(world, composite)
        files = _list_composite_files(repo, "", True)
        details = app_helpers._get_composite_details(repo)
        local._do_deactivate_repository(USER, COMPOSITE)

        assert world.workspace_mcp.calls == []
        assert world.git_mcp.calls == []
        assert world.content.calls == []
        assert {f.full_path for f in files} == {
            f"{m}/{p}" for m in MEMBERS for p in ("README.md", "src/app.py")
        }
        assert sorted(c.name for c in details.component_repositories) == sorted(MEMBERS)
        assert not composite.exists()

    def test_local_activation_discovers_by_walking(
        self, world: CompositeWorld, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from code_indexer.proxy.config_manager import ProxyConfigManager

        seen: List[str] = []
        original = ProxyConfigManager.refresh_repositories

        def spy(self: ProxyConfigManager) -> None:
            seen.append(str(self.proxy_root))
            original(self)

        monkeypatch.setattr(ProxyConfigManager, "refresh_repositories", spy)
        _activate(world.local_manager())
        assert seen == [str(_local_composite(world))]
        config = json.loads(
            (_local_composite(world) / INDEX_DIR / "config.json").read_text()
        )
        assert config["proxy_mode"] is True
        assert config["discovered_repos"] == sorted(MEMBERS)
