"""Golden-repo write tools write through the content writer on a mount-less pod.

Under a delegated content backend the repository is not on this pod: the
write-exception (golden) repo's registered path does not exist here. The files
live "elsewhere" -- a real tree in this test -- served by a fake NON-local
content backend (``LocalContentBackend`` over that tree) and written by a fake
content writer that applies ``put``/``delete`` to the same tree with git-blob
compare-and-swap, refusing ``.git``, symlinks and a secret like workspace-mcp.

Each tool call runs twice, on identical starting trees:

* locally, on the tree itself (today's code path, no writer anywhere), and
* delegated, on a path that does not exist on this machine.

Results (minus timestamps) and the resulting trees must be identical.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import get_current_user
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.services import file_service as fs_mod
from code_indexer.server.services.file_crud_service import (
    CRUDOperationError,
    FileCRUDService,
    HashMismatchError,
)
from code_indexer.server.startup import content_backend_wiring as backend_wiring
from code_indexer.server.startup import content_writer_wiring as writer_wiring
from code_indexer.server.storage.shared.content_backend import (
    ContentPage,
    FetchedFile,
    LocalContentBackend,
)
from code_indexer.utils.blob_hash import git_blob_hash_bytes

ALIAS = "cidx-meta-global"
USER = "alice"
SECRET = "AKIAABCDEFGHIJKLMNOP"
SECRET_RULE = "aws-access-key-id"
BOM = b"\xef\xbb\xbf"


# --------------------------------------------------------------------------
# fakes: a remote repository at an absolute path this pod does not have
# --------------------------------------------------------------------------


class _Remote:
    """Maps the pod's (absent) repository path onto the tree that holds it."""

    def __init__(self, pod_root: Path, tree: Path) -> None:
        self.pod_root = PurePosixPath(pod_root)
        self.tree = tree

    def local(self, path: Union[str, Path]) -> Path:
        rel = PurePosixPath(path).relative_to(self.pod_root)
        return self.tree / str(rel) if str(rel) != "." else self.tree


class FakeBackend:
    """A NON-local content backend serving the remote tree."""

    def __init__(self, remote: _Remote) -> None:
        self._remote = remote
        self._inner = LocalContentBackend()
        self.after_fetch: Optional[Any] = None
        self.fetched_roots: List[str] = []

    @property
    def is_local(self) -> bool:
        return False

    def manifest(self, repo_path: Any, **kwargs: Any) -> ContentPage:
        return self._inner.manifest(self._remote.local(repo_path), **kwargs)

    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[FetchedFile]:
        self.fetched_roots.append(str(repo_path))
        records = list(self._inner.fetch(self._remote.local(repo_path), paths))
        if self.after_fetch is not None:
            self.after_fetch()
        return iter(records)


class FakeWriteError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class FakeWriter:
    """workspace-mcp's POST /content/write semantics over the remote tree."""

    def __init__(self, remote: _Remote) -> None:
        self._remote = remote
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []
        self.fail_with: Optional[Exception] = None

    def _target(self, path: str) -> Path:
        rel = PurePosixPath(path).relative_to(self._remote.pod_root)
        if ".git" in rel.parts:
            raise FakeWriteError("EFORBIDDEN", f"{path}: .git is not writable")
        target = self._remote.local(path)
        probe = target
        while probe != self._remote.tree:
            if probe.is_symlink():
                raise FakeWriteError("EFORBIDDEN", f"{path}: symlink refused")
            probe = probe.parent
        return target

    def _check(self, target: Path, kwargs: Dict[str, Any]) -> None:
        if "expected_blob_sha" not in kwargs:
            return
        expected = kwargs["expected_blob_sha"]
        current = git_blob_hash_bytes(target.read_bytes()) if target.is_file() else None
        if current != expected:
            raise FakeWriteError("ECONFLICT", "expected_blob_sha does not match")

    def put(self, path: str, data: bytes, **kwargs: Any) -> str:
        self.calls.append(("put", path, kwargs))
        if self.fail_with is not None:
            raise self.fail_with
        target = self._target(path)
        if SECRET.encode() in data:
            raise FakeWriteError("ESECRET", f"{path}: matched rule {SECRET_RULE}")
        self._check(target, kwargs)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return git_blob_hash_bytes(data)

    def delete(self, path: str, **kwargs: Any) -> bool:
        self.calls.append(("delete", path, kwargs))
        if self.fail_with is not None:
            raise self.fail_with
        target = self._target(path)
        self._check(target, kwargs)
        if not target.exists():
            return False
        target.unlink()
        return True


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


def _seed(tree: Path) -> None:
    (tree / "docs").mkdir(parents=True)
    (tree / "README.md").write_bytes(b"# Meta\n\nhello world\nhello again\n")
    (tree / "docs" / "guide.md").write_bytes(b"guide\n")
    (tree / "bom.md").write_bytes(BOM + "café one\n".encode("utf-8"))
    (tree / "dir-as-file").mkdir()


def _snapshot(tree: Path) -> Dict[str, bytes]:
    return {
        str(p.relative_to(tree)): p.read_bytes()
        for p in sorted(tree.rglob("*"))
        if p.is_file()
    }


def _service(golden_repos_dir: Path, repo_path: Path) -> FileCRUDService:
    service = FileCRUDService()
    service.register_write_exception(ALIAS, repo_path)
    service.set_golden_repos_dir(golden_repos_dir)
    return service


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A local repo, an identical remote one, and the pod path that maps to it."""
    golden = tmp_path / "golden-repos"
    (golden / ".write_mode").mkdir(parents=True)
    (golden / ".write_mode" / "cidx-meta.json").write_text("{}")

    local_tree = tmp_path / "local" / "cidx-meta"
    remote_tree = tmp_path / "elsewhere" / "cidx-meta"
    _seed(local_tree)
    _seed(remote_tree)
    pod_root = tmp_path / "not-mounted" / "golden-repos" / "cidx-meta"

    remote = _Remote(pod_root, remote_tree)
    backend = FakeBackend(remote)
    writer = FakeWriter(remote)
    monkeypatch.setattr(backend_wiring, "server_content_backend", lambda: None)
    monkeypatch.setattr(writer_wiring, "server_content_writer", lambda: None)
    fs_mod._collect_cache.clear()
    return SimpleNamespace(
        golden=golden,
        local_tree=local_tree,
        remote_tree=remote_tree,
        pod_root=pod_root,
        backend=backend,
        writer=writer,
        local=_service(golden, local_tree),
        delegated=_service(golden, pod_root),
        monkeypatch=monkeypatch,
    )


def _delegate(world: SimpleNamespace, writer: Optional[Any] = None) -> None:
    world.monkeypatch.setattr(
        backend_wiring, "server_content_backend", lambda: world.backend
    )
    chosen = world.writer if writer is None else writer
    world.monkeypatch.setattr(writer_wiring, "server_content_writer", lambda: chosen)


def _sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


_STAMPS = ("created_at", "modified_at", "deleted_at")


def _outcome(call: Any) -> Tuple[str, Any]:
    try:
        result = call()
    except Exception as exc:  # the outcome under test, whatever it is
        return type(exc).__name__, str(exc)
    return "ok", {k: v for k, v in result.items() if k not in _STAMPS}


# --------------------------------------------------------------------------
# equivalence: every tool, local vs delegated
# --------------------------------------------------------------------------


def _readme_hash(tree: Path) -> str:
    return _sha((tree / "README.md").read_bytes())


SCENARIOS = {
    "create": lambda s, t: s.create_file(ALIAS, "notes/new.md", "new\n", USER),
    "create_existing": lambda s, t: s.create_file(ALIAS, "README.md", "x", USER),
    "create_traversal": lambda s, t: s.create_file(ALIAS, "../escape.md", "x", USER),
    "create_git": lambda s, t: s.create_file(ALIAS, ".git/config", "x", USER),
    "create_absolute": lambda s, t: s.create_file(ALIAS, "/etc/x", "x", USER),
    "edit_unique": lambda s, t: s.edit_file(
        ALIAS, "docs/guide.md", "guide", "GUIDE", _sha(b"guide\n"), False, USER
    ),
    "edit_replace_all": lambda s, t: s.edit_file(
        ALIAS, "README.md", "hello", "bye", _readme_hash(t), True, USER
    ),
    "edit_not_unique": lambda s, t: s.edit_file(
        ALIAS, "README.md", "hello", "bye", _readme_hash(t), False, USER
    ),
    "edit_not_found_string": lambda s, t: s.edit_file(
        ALIAS, "README.md", "absent", "x", _readme_hash(t), False, USER
    ),
    "edit_stale_hash": lambda s, t: s.edit_file(
        ALIAS, "README.md", "hello", "x", "0" * 64, True, USER
    ),
    "edit_missing_file": lambda s, t: s.edit_file(
        ALIAS, "nope.md", "a", "b", "0" * 64, False, USER
    ),
    "edit_missing_dir": lambda s, t: s.edit_file(
        ALIAS, "no/such/dir.md", "a", "b", "0" * 64, False, USER
    ),
    "edit_bom": lambda s, t: s.edit_file(
        ALIAS,
        "bom.md",
        "one",
        "two",
        _sha((t / "bom.md").read_bytes()),
        False,
        USER,
    ),
    "edit_directory": lambda s, t: s.edit_file(
        ALIAS, "dir-as-file", "a", "b", "0" * 64, False, USER
    ),
    "delete": lambda s, t: s.delete_file(ALIAS, "docs/guide.md", None, USER),
    "delete_with_hash": lambda s, t: s.delete_file(
        ALIAS, "docs/guide.md", _sha(b"guide\n"), USER
    ),
    "delete_stale_hash": lambda s, t: s.delete_file(
        ALIAS, "docs/guide.md", "0" * 64, USER
    ),
    "delete_missing": lambda s, t: s.delete_file(ALIAS, "nope.md", None, USER),
    "delete_missing_with_hash": lambda s, t: s.delete_file(
        ALIAS, "nope.md", "0" * 64, USER
    ),
    "delete_git": lambda s, t: s.delete_file(ALIAS, ".git/HEAD", None, USER),
    "delete_root": lambda s, t: s.delete_file(ALIAS, ".", None, USER),
}

#: Where the two paths say the same thing in different words: today's local
#: edit of a directory surfaces the OS error (with the absolute path) and
#: deleting "." trips the resolved-path .git guard; the seam reports the
#: backend's code and a lexical refusal. Same exception type either way.
_TYPE_ONLY = frozenset({"edit_directory", "delete_root"})


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_each_tool_is_identical_local_and_delegated(
    world: SimpleNamespace, name: str
) -> None:
    call = SCENARIOS[name]
    local = _outcome(lambda: call(world.local, world.local_tree))

    _delegate(world)
    delegated = _outcome(lambda: call(world.delegated, world.remote_tree))

    if name in _TYPE_ONLY:
        assert delegated[0] == local[0]
    else:
        assert delegated == local
    assert _snapshot(world.remote_tree) == _snapshot(world.local_tree)
    assert not world.pod_root.exists(), "a delegated write touched this pod's disk"


def test_local_never_consults_the_writer(world: SimpleNamespace) -> None:
    """The hard rule: with the local default, the writer is never reached."""

    class Exploding:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"writer.{name} used under the local backend")

    world.monkeypatch.setattr(
        backend_wiring, "server_content_backend", lambda: LocalContentBackend()
    )
    world.monkeypatch.setattr(writer_wiring, "server_content_writer", Exploding)
    world.local.create_file(ALIAS, "a.md", "a\n", USER)
    world.local.edit_file(ALIAS, "a.md", "a", "b", _sha(b"a\n"), False, USER)
    world.local.delete_file(ALIAS, "a.md", None, USER)
    assert not (world.local_tree / "a.md").exists()


# --------------------------------------------------------------------------
# compare-and-swap
# --------------------------------------------------------------------------


def test_create_sends_must_not_exist(world: SimpleNamespace) -> None:
    _delegate(world)
    world.delegated.create_file(ALIAS, "fresh.md", "x", USER)
    assert world.writer.calls == [
        ("put", str(world.pod_root / "fresh.md"), {"expected_blob_sha": None})
    ]


def test_create_on_an_existing_file_is_already_exists(world: SimpleNamespace) -> None:
    _delegate(world)
    with pytest.raises(FileExistsError, match="File already exists: README.md"):
        world.delegated.create_file(ALIAS, "README.md", "clobber", USER)
    assert (world.remote_tree / "README.md").read_bytes().startswith(b"# Meta")


def test_edit_pins_the_blob_it_read(world: SimpleNamespace) -> None:
    _delegate(world)
    before = (world.remote_tree / "docs" / "guide.md").read_bytes()
    world.delegated.edit_file(
        ALIAS, "docs/guide.md", "guide", "G", _sha(before), False, USER
    )
    assert world.writer.calls[-1][2] == {
        "expected_blob_sha": git_blob_hash_bytes(before)
    }


def test_a_concurrent_edit_is_a_conflict_not_a_lost_update(
    world: SimpleNamespace,
) -> None:
    """Another writer lands between our read and our put: ours must lose."""
    _delegate(world)
    guide = world.remote_tree / "docs" / "guide.md"
    world.backend.after_fetch = lambda: guide.write_bytes(b"theirs\n")

    with pytest.raises(HashMismatchError, match="modified by another writer"):
        world.delegated.edit_file(
            ALIAS, "docs/guide.md", "guide", "ours", _sha(b"guide\n"), False, USER
        )
    assert guide.read_bytes() == b"theirs\n"


def test_a_conditional_delete_pins_the_blob_it_read(world: SimpleNamespace) -> None:
    _delegate(world)
    guide = world.remote_tree / "docs" / "guide.md"
    world.backend.after_fetch = lambda: guide.write_bytes(b"theirs\n")

    with pytest.raises(HashMismatchError):
        world.delegated.delete_file(ALIAS, "docs/guide.md", _sha(b"guide\n"), USER)
    assert guide.read_bytes() == b"theirs\n"


def test_an_unconditional_delete_sends_no_precondition(world: SimpleNamespace) -> None:
    _delegate(world)
    world.delegated.delete_file(ALIAS, "docs/guide.md", None, USER)
    assert world.writer.calls == [
        ("delete", str(world.pod_root / "docs" / "guide.md"), {})
    ]


# --------------------------------------------------------------------------
# error mapping
# --------------------------------------------------------------------------


def test_a_secret_is_refused_without_echoing_it(world: SimpleNamespace) -> None:
    _delegate(world)
    with pytest.raises(ValueError) as caught:
        world.delegated.create_file(ALIAS, "leak.md", f"key={SECRET}\n", USER)
    message = str(caught.value)
    assert SECRET_RULE in message and "secret" in message
    assert SECRET not in message
    assert not (world.remote_tree / "leak.md").exists()


def test_a_forbidden_path_is_a_permission_error(world: SimpleNamespace) -> None:
    os.symlink(world.remote_tree / "docs", world.remote_tree / "linked")
    _delegate(world)
    with pytest.raises(PermissionError, match="create_file blocked: .*symlink"):
        world.delegated.create_file(ALIAS, "linked/x.md", "x", USER)


@pytest.mark.parametrize(
    "code, expected",
    [
        ("EINVAL", ValueError),
        ("ETOOLARGE", ValueError),
        ("EUNAVAILABLE", CRUDOperationError),
        ("ETRANSPORT", CRUDOperationError),
        ("EIO", CRUDOperationError),
    ],
)
def test_other_writer_codes_map_to_todays_types(
    world: SimpleNamespace, code: str, expected: type
) -> None:
    _delegate(world)
    world.writer.fail_with = FakeWriteError(code, "nope")
    with pytest.raises(expected, match="nope"):
        world.delegated.create_file(ALIAS, "x.md", "x", USER)


def test_an_uncoded_writer_failure_is_a_crud_error(world: SimpleNamespace) -> None:
    _delegate(world)
    world.writer.fail_with = RuntimeError("boom")
    with pytest.raises(CRUDOperationError, match="boom"):
        world.delegated.create_file(ALIAS, "x.md", "x", USER)


# --------------------------------------------------------------------------
# mount-less pod, snapshot mapping, fail loud
# --------------------------------------------------------------------------


def test_no_mount_writes_land_only_behind_the_seam(world: SimpleNamespace) -> None:
    _delegate(world)
    assert not world.pod_root.exists()
    world.delegated.create_file(ALIAS, "a/b/c.md", "deep\n", USER)
    assert (world.remote_tree / "a" / "b" / "c.md").read_bytes() == b"deep\n"
    assert not world.pod_root.exists()


def test_a_snapshot_alias_writes_to_the_base_clone(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As the read tools do: a pod-local snapshot maps to the base clone."""
    from code_indexer.server.repositories import golden_repo_manager as grm

    snapshot = world.golden / ".versioned" / "cidx-meta" / "v_1760000000"
    manager = SimpleNamespace(
        get_golden_repo=lambda name: SimpleNamespace(clone_path=str(world.pod_root))
    )
    monkeypatch.setattr(grm, "get_golden_repo_manager", lambda: manager)
    service = _service(world.golden, snapshot)
    _delegate(world)

    service.create_file(ALIAS, "snap.md", "x", USER)

    assert world.writer.calls[0][1] == str(world.pod_root / "snap.md")
    assert (world.remote_tree / "snap.md").exists()


def test_delegated_without_a_writer_refuses_and_writes_nothing(
    world: SimpleNamespace,
) -> None:
    world.monkeypatch.setattr(
        backend_wiring, "server_content_backend", lambda: world.backend
    )
    world.monkeypatch.setattr(writer_wiring, "server_content_writer", lambda: None)
    with pytest.raises(CRUDOperationError, match="no content writer"):
        world.delegated.create_file(ALIAS, "x.md", "x", USER)
    assert not world.pod_root.exists()
    assert not (world.remote_tree / "x.md").exists()


def test_the_write_mode_gate_still_applies(world: SimpleNamespace) -> None:
    (world.golden / ".write_mode" / "cidx-meta.json").unlink()
    _delegate(world)
    with pytest.raises(PermissionError, match="requires write mode"):
        world.delegated.create_file(ALIAS, "x.md", "x", USER)
    assert world.writer.calls == []


# --------------------------------------------------------------------------
# front doors: the same statuses and shapes
# --------------------------------------------------------------------------


@pytest.fixture
def rest(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from code_indexer.server.routers import files as files_router

    monkeypatch.setattr(files_router, "file_crud_service", world.delegated)
    app = FastAPI()
    app.include_router(files_router.router)
    app.dependency_overrides[get_current_user] = lambda: User(
        username=USER,
        password_hash="x",
        role=UserRole.ADMIN,
        created_at=datetime.now(),
    )
    _delegate(world)
    return TestClient(app)


def test_rest_statuses_match_todays(rest: TestClient, world: SimpleNamespace) -> None:
    base = f"/api/v1/repos/{ALIAS}/files"
    assert (
        rest.post(base, json={"file_path": "n.md", "content": "n"}).status_code == 201
    )
    assert (
        rest.post(base, json={"file_path": "n.md", "content": "n"}).status_code == 409
    )
    secret = rest.post(base, json={"file_path": "s.md", "content": SECRET})
    assert secret.status_code == 400 and SECRET not in secret.text
    assert (
        rest.post(base, json={"file_path": ".git/x", "content": "n"}).status_code == 403
    )
    stale = rest.patch(
        f"{base}/n.md",
        json={"old_string": "n", "new_string": "m", "content_hash": "0" * 64},
    )
    assert stale.status_code == 409
    assert rest.delete(f"{base}/missing.md").status_code == 404
    assert rest.delete(f"{base}/n.md").status_code == 200
    assert _snapshot(world.remote_tree).get("n.md") is None


def test_mcp_handler_shapes_match_todays(
    world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.mcp.handlers import files as handlers
    from code_indexer.server.services import file_crud_service as crud_mod

    monkeypatch.setattr(crud_mod, "file_crud_service", world.delegated)
    monkeypatch.setattr(handlers, "_start_auto_watch_if_needed", lambda *a: None)
    invalidated: List[Tuple[str, str]] = []
    monkeypatch.setattr(
        handlers, "_invalidate_wiki_cache", lambda a, p: invalidated.append((a, p))
    )
    _delegate(world)
    user = SimpleNamespace(username=USER)

    ok = handlers.handle_create_file(
        {"repository_alias": ALIAS, "file_path": "m.md", "content": "m\n"}, user
    )
    refused = handlers.handle_create_file(
        {"repository_alias": ALIAS, "file_path": "s.md", "content": SECRET}, user
    )

    ok_body = json.loads(ok["content"][0]["text"])
    refused_body = json.loads(refused["content"][0]["text"])
    assert ok_body["success"] is True and ok_body["content_hash"] == _sha(b"m\n")
    assert refused_body["success"] is False and SECRET not in refused_body["error"]
    assert invalidated == [(ALIAS, "m.md")]


# --------------------------------------------------------------------------
# keep the fakes honest
# --------------------------------------------------------------------------


def test_the_remote_tree_is_really_elsewhere(world: SimpleNamespace) -> None:
    assert world.remote_tree.exists()
    assert not str(world.remote_tree).startswith(str(world.pod_root))
    shutil.rmtree(world.local_tree)
    _delegate(world)
    world.delegated.create_file(ALIAS, "z.md", "z", USER)
    assert (world.remote_tree / "z.md").exists()


def test_an_activated_repo_still_writes_its_local_workspace(
    world: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Out of scope here (per-user workspaces): never routed to the writer."""
    from code_indexer.server.services import file_crud_service as crud_mod

    workspace = tmp_path / "activated-repos" / USER / "mine"
    workspace.mkdir(parents=True)
    manager = SimpleNamespace(
        user_has_activated_repo=lambda user, alias: True,
        get_activated_repo_path=lambda username, user_alias: str(workspace),
    )
    monkeypatch.setattr(crud_mod, "_get_activated_repo_manager", lambda: manager)
    _delegate(world)

    world.delegated.create_file("mine", "w.md", "w\n", USER)

    assert (workspace / "w.md").read_bytes() == b"w\n"
    assert world.writer.calls == []
