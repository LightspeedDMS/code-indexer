"""Activated-repository file tools read and write through the content seam.

Under a delegated content backend a user's activated working copy is not on
this pod: it lives on workspace-mcp's storage at the path cidx computes for it
(``<activated_repos_dir>/<user>/<alias>``), which does NOT exist on this
machine in these tests. Reads (list_files, get_file_content) go through the
content backend and writes (create/edit/delete) through the content writer,
both at that path.

Every tool call runs twice over identical trees -- locally on the tree itself
(today's code, no backend or writer anywhere) and delegated on the absent pod
path -- and the outcomes and resulting trees must be identical. The pod path
never appearing is the mount-independence proof: any local read of it fails
naturally and any local write would create it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict

import pytest

from code_indexer.server.models.api_models import FileListQueryParams
from code_indexer.server.services import file_crud_service as crud_mod
from code_indexer.server.services import file_service as fs_mod
from code_indexer.server.services.file_crud_service import FileCRUDService
from code_indexer.server.services.file_service import FileListingService
from code_indexer.server.startup import content_backend_wiring as backend_wiring
from code_indexer.server.startup import content_writer_wiring as writer_wiring
from tests.unit.server.services.test_golden_repo_writes_through_content_writer import (
    FakeBackend,
    FakeWriter,
    _outcome,
    _Remote,
    _seed,
    _snapshot,
)

ACT = "example-repo"
USER = "alice"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Registry:
    """The server's activated-repo registry: ``ACT`` is activated for USER."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.asked: list = []

    def user_has_activated_repo(self, username: str, alias: str) -> bool:
        self.asked.append((username, alias))
        return username == USER and alias == ACT

    def get_activated_repo_path(self, username: str, user_alias: str) -> str:
        return str(self.root / username / user_alias)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    local_root = tmp_path / "local" / "activated-repos"
    pod_root = tmp_path / "not-mounted" / "activated-repos"
    remote_tree = tmp_path / "workspace-mcp" / "activated-repos" / USER / ACT
    local_tree = local_root / USER / ACT
    _seed(local_tree)
    _seed(remote_tree)
    pod_copy = pod_root / USER / ACT
    remote = _Remote(pod_copy, remote_tree)
    w = SimpleNamespace(
        local_root=local_root,
        pod_root=pod_root,
        pod_copy=pod_copy,
        local_tree=local_tree,
        remote_tree=remote_tree,
        backend=FakeBackend(remote),
        writer=FakeWriter(remote),
        monkeypatch=monkeypatch,
    )
    _local(w)
    return w


def _use_registry(w: SimpleNamespace, root: Path) -> _Registry:
    registry = _Registry(root)
    w.monkeypatch.setattr(crud_mod, "_get_activated_repo_manager", lambda: registry)
    return registry


def _local(w: SimpleNamespace) -> None:
    w.monkeypatch.setattr(backend_wiring, "server_content_backend", lambda: None)
    w.monkeypatch.setattr(writer_wiring, "server_content_writer", lambda: None)
    _use_registry(w, w.local_root)
    fs_mod._collect_cache.clear()


def _delegate(w: SimpleNamespace) -> _Registry:
    w.monkeypatch.setattr(backend_wiring, "server_content_backend", lambda: w.backend)
    w.monkeypatch.setattr(writer_wiring, "server_content_writer", lambda: w.writer)
    fs_mod._collect_cache.clear()
    return _use_registry(w, w.pod_root)


def _listing_service(w: SimpleNamespace, root: Path) -> FileListingService:
    service = FileListingService()
    service.activated_repo_manager = _Registry(root)  # type: ignore[assignment]
    return service


def _paths(response: Any) -> Dict[str, int]:
    return {f.path: f.size_bytes for f in response.files}


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------


def test_list_files_is_identical_local_and_delegated(world: SimpleNamespace) -> None:
    params = FileListQueryParams(page=1, limit=500)
    local = _listing_service(world, world.local_root).list_files(ACT, USER, params)

    registry = _delegate(world)
    delegated = _listing_service(world, world.pod_root).list_files(ACT, USER, params)

    assert _paths(delegated) == _paths(local) != {}
    assert registry.asked == [(USER, ACT)]
    assert not world.pod_copy.exists()


@pytest.mark.parametrize("file_path", ["README.md", "docs/guide.md"])
def test_get_file_content_is_identical_local_and_delegated(
    world: SimpleNamespace, file_path: str
) -> None:
    def read(root: Path) -> Dict[str, Any]:
        result = _listing_service(world, root).get_file_content(
            ACT, file_path, USER, skip_truncation=True
        )
        meta = {k: v for k, v in result["metadata"].items() if k != "modified_at"}
        return {"content": result["content"], "metadata": meta}

    local = read(world.local_root)
    _delegate(world)
    delegated = read(world.pod_root)

    assert delegated == local
    assert delegated["metadata"]["content_hash"] == _sha(
        (world.remote_tree / file_path).read_bytes()
    )
    assert not world.pod_copy.exists()


def test_a_missing_file_is_not_found(world: SimpleNamespace) -> None:
    _delegate(world)
    with pytest.raises(FileNotFoundError):
        _listing_service(world, world.pod_root).get_file_content(ACT, "nope.md", USER)


def test_another_users_alias_is_refused_by_the_registry(
    world: SimpleNamespace,
) -> None:
    """Isolation: a user who has not activated the alias reads nothing, even
    though a copy exists at the path the alias would map to."""
    _delegate(world)
    service = _listing_service(world, world.pod_root)
    with pytest.raises(FileNotFoundError):
        service.list_files(ACT, "mallory", FileListQueryParams(page=1, limit=10))
    assert world.backend.fetched_roots == []


def test_an_unreachable_backend_fails_loud_not_empty(world: SimpleNamespace) -> None:
    _delegate(world)

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise ConnectionError("workspace-mcp unreachable")

    world.monkeypatch.setattr(world.backend, "manifest", boom)
    with pytest.raises(Exception, match="unreachable"):
        _listing_service(world, world.pod_root).list_files(
            ACT, USER, FileListQueryParams(page=1, limit=10)
        )


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------


Scenario = Callable[[FileCRUDService, Path], Any]

SCENARIOS: Dict[str, Scenario] = {
    "create": lambda s, t: s.create_file(ACT, "notes/new.md", "new\n", USER),
    "create_existing": lambda s, t: s.create_file(ACT, "README.md", "x", USER),
    "create_traversal": lambda s, t: s.create_file(ACT, "../escape.md", "x", USER),
    "create_git": lambda s, t: s.create_file(ACT, ".git/config", "x", USER),
    "edit_unique": lambda s, t: s.edit_file(
        ACT, "docs/guide.md", "guide", "GUIDE", _sha(b"guide\n"), False, USER
    ),
    "edit_replace_all": lambda s, t: s.edit_file(
        ACT,
        "README.md",
        "hello",
        "bye",
        _sha((t / "README.md").read_bytes()),
        True,
        USER,
    ),
    "edit_stale_hash": lambda s, t: s.edit_file(
        ACT, "README.md", "hello", "x", "0" * 64, True, USER
    ),
    "edit_missing_file": lambda s, t: s.edit_file(
        ACT, "nope.md", "a", "b", "0" * 64, False, USER
    ),
    "edit_bom": lambda s, t: s.edit_file(
        ACT, "bom.md", "one", "two", _sha((t / "bom.md").read_bytes()), False, USER
    ),
    "delete": lambda s, t: s.delete_file(ACT, "docs/guide.md", None, USER),
    "delete_with_hash": lambda s, t: s.delete_file(
        ACT, "docs/guide.md", _sha(b"guide\n"), USER
    ),
    "delete_stale_hash": lambda s, t: s.delete_file(
        ACT, "docs/guide.md", "0" * 64, USER
    ),
    "delete_missing": lambda s, t: s.delete_file(ACT, "nope.md", None, USER),
    "delete_git": lambda s, t: s.delete_file(ACT, ".git/HEAD", None, USER),
    "not_activated": lambda s, t: s.create_file("someone-else", "x.md", "x", USER),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_each_write_tool_is_identical_local_and_delegated(
    world: SimpleNamespace, name: str
) -> None:
    call = SCENARIOS[name]
    local = _outcome(lambda: call(FileCRUDService(), world.local_tree))

    _delegate(world)
    delegated = _outcome(lambda: call(FileCRUDService(), world.remote_tree))

    assert delegated == local
    assert _snapshot(world.remote_tree) == _snapshot(world.local_tree)
    assert not world.pod_copy.exists(), "a delegated write touched this pod's disk"


def test_writes_go_to_the_writer_at_the_copys_path(world: SimpleNamespace) -> None:
    _delegate(world)
    FileCRUDService().create_file(ACT, "fresh.md", "x", USER)
    assert world.writer.calls == [
        ("put", str(world.pod_copy / "fresh.md"), {"expected_blob_sha": None})
    ]


def test_a_read_hash_is_accepted_by_a_delegated_edit(world: SimpleNamespace) -> None:
    """get_file_content's content_hash is the precondition edit_file takes."""
    _delegate(world)
    read = _listing_service(world, world.pod_root).get_file_content(
        ACT, "docs/guide.md", USER, skip_truncation=True
    )
    FileCRUDService().edit_file(
        ACT,
        "docs/guide.md",
        "guide",
        "edited",
        read["metadata"]["content_hash"],
        False,
        USER,
    )
    assert (world.remote_tree / "docs" / "guide.md").read_bytes() == b"edited\n"


def test_no_writer_parked_refuses_instead_of_writing_the_pod(
    world: SimpleNamespace,
) -> None:
    _delegate(world)
    world.monkeypatch.setattr(writer_wiring, "server_content_writer", lambda: None)
    with pytest.raises(crud_mod.CRUDOperationError, match="refusing to write"):
        FileCRUDService().create_file(ACT, "x.md", "x", USER)
    assert not world.pod_copy.exists()


def test_local_never_consults_the_backend_or_writer(world: SimpleNamespace) -> None:
    FileCRUDService().create_file(ACT, "a.md", "a\n", USER)
    _listing_service(world, world.local_root).list_files(
        ACT, USER, FileListQueryParams(page=1, limit=10)
    )
    assert world.writer.calls == []
    assert world.backend.fetched_roots == []


# --------------------------------------------------------------------------
# regex_search on an activated alias
# --------------------------------------------------------------------------


def _run_regex(world: SimpleNamespace, copy_path: Path) -> Dict[str, Any]:
    """handle_regex_search on ACT; returns {"path": searched path} or the error."""
    import asyncio
    import json
    from datetime import datetime
    from unittest.mock import MagicMock, patch

    from code_indexer.server.auth.user_manager import User, UserRole
    from code_indexer.server.mcp.handlers.search import handle_regex_search

    arm = MagicMock()
    arm.user_has_activated_repo.return_value = True
    arm.get_activated_repo_path.return_value = str(copy_path)
    captured: Dict[str, Any] = {}

    async def fake_execute(args: Any, path: Any, alias: Any, user: Any) -> Any:
        captured["path"] = path
        meta = {"reranker_used": False, "reranker_provider": None, "rerank_time_ms": 0}
        result = MagicMock(
            truncated=False,
            read_capped=False,
            search_engine="ripgrep",
            search_time_ms=1,
            total_matches=0,
        )
        return [], meta, result

    mod = "code_indexer.server.mcp.handlers.search.regex_search"
    with (
        patch(
            "code_indexer.server.mcp.handlers._utils.app_module.activated_repo_manager",
            arm,
        ),
        patch(f"{mod}._execute_regex_search", side_effect=fake_execute),
    ):
        user = User(
            username=USER,
            role=UserRole.NORMAL_USER,
            password_hash="x",
            created_at=datetime.now(),
        )
        response = asyncio.new_event_loop().run_until_complete(
            handle_regex_search({"repository_alias": ACT, "pattern": "hello"}, user)
        )
    body = json.loads(response["content"][0]["text"])
    return {"path": captured.get("path"), "success": body.get("success")}


def test_regex_search_reaches_a_delegated_copy_absent_from_this_pod(
    world: SimpleNamespace,
) -> None:
    _delegate(world)
    outcome = _run_regex(world, world.pod_copy)
    assert outcome == {"path": world.pod_copy, "success": True}


def test_regex_search_locally_still_refuses_a_missing_copy(
    world: SimpleNamespace,
) -> None:
    outcome = _run_regex(world, world.pod_copy)
    assert outcome == {"path": None, "success": False}


# --------------------------------------------------------------------------
# auto-watch after a write
# --------------------------------------------------------------------------


def test_auto_watch_is_skipped_before_any_probe_under_delegated(
    world: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    """Delegated: nothing on this pod to watch, so the handler returns before
    asking the registry, probing the pod path or starting a watch -- and
    without the MCP-GENERAL-223 "skipping" warning a probe would log."""
    import logging
    from unittest.mock import MagicMock

    from code_indexer.server.mcp.handlers import _utils, files as files_handler
    from code_indexer.server.services import auto_watch_manager as awm

    _delegate(world)
    registry = MagicMock()
    start_watch = MagicMock()
    world.monkeypatch.setattr(_utils, "_get_activated_repo_manager", lambda: registry)
    world.monkeypatch.setattr(awm.auto_watch_manager, "start_watch", start_watch)
    user = MagicMock()
    user.username = USER

    with caplog.at_level(logging.DEBUG):
        files_handler._start_auto_watch_if_needed(ACT, user, "MCP-GENERAL-224")

    start_watch.assert_not_called()
    assert registry.mock_calls == []
    assert "MCP-GENERAL-223" not in caplog.text
    assert "MCP-GENERAL-224" not in caplog.text
