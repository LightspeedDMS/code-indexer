"""The ripgrep, content writer and index store seams as the core resolves them.

Each mode's implementation is reached ONLY through the seam resolver and
passes the seam's conformance suite:

* ``local`` -- the module registering the entry name ``local``
  (cidx-backend-workspace), built the way the core builds it (the ripgrep
  backend with the core's own executor as its runner);
* ``delegated`` -- the testkit's fake workspace-mcp, registered as the
  delegate exactly as an installed delegate distribution is found (the index
  store's delegate is an artifact client, wrapped by the core's
  ``DelegatedIndexStore``).

With no local module installed, each refuses, naming the seam.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterator

import pytest

from cidx_sdk.errors import SeamUnavailableError
from cidx_sdk.seams.index_store import IndexStore
from cidx_sdk.seams.resolver import extra_entry_points, remove_entry_points
from cidx_testkit.conformance.content_writer import (
    ContentWriterContract,
    WriterSubject,
)
from cidx_testkit.conformance.index_store import IndexStoreContract
from cidx_testkit.conformance.ripgrep import RipgrepBackendContract, RipgrepSubject
from cidx_testkit.fakes.workspace_mcp import (
    FakeIndexArtifacts,
    FakeWorkspaceMcp,
    FakeWorkspaceMcpContentWriter,
    FakeWorkspaceMcpRipgrepBackend,
    register_as_delegate,
    unregister_delegate,
)
from cidx_testkit.registry import register_in_tree_entry_points

from code_indexer.kernel import index_store_seam
from code_indexer.server.services import (
    backend_plugin_loader,
    content_writer_plugin,
    index_artifacts_plugin,
    ripgrep_backend_plugin,
)
from code_indexer.server.startup.index_store_wiring import build_configured_index_store
from code_indexer.server.startup.ripgrep_backend_wiring import (
    build_configured_ripgrep_backend,
)
from code_indexer.server.storage.shared.ripgrep_backend import (
    SubprocessExecutorRunner,
    local_ripgrep_backend,
)
from code_indexer.utils import index_store as index_store_module
from code_indexer.utils.index_store import DelegatedIndexStore

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CIDX_ROOT = "/srv/example/golden-repos/example-repo"
_RESET = (
    content_writer_plugin.reset_cache,
    index_artifacts_plugin.reset_cache,
    ripgrep_backend_plugin.reset_cache,
    index_store_seam.reset_cache,
)


@pytest.fixture(autouse=True)
def _fresh_resolvers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No ambient install: only what this tree declares, plus what a test adds."""
    monkeypatch.setattr(backend_plugin_loader, "entry_points", lambda **_: [])
    monkeypatch.setattr(index_store_seam, "entry_points", lambda **_: [])
    for name in ("CIDX_RIPGREP_BACKEND", "CIDX_CONTENT_WRITER", "CIDX_INDEX_ARTIFACTS"):
        monkeypatch.delenv(name, raising=False)
    for reset in _RESET:
        reset()
    yield
    for reset in _RESET:
        reset()


@pytest.fixture
def fake_service(tmp_path: Path) -> Iterator[FakeWorkspaceMcp]:
    service = FakeWorkspaceMcp(store=tmp_path / "store")
    registered = register_as_delegate(service)
    for reset in _RESET:
        reset()
    try:
        yield service
    finally:
        unregister_delegate(registered)


# ── ripgrep ──────────────────────────────────────────────────────────────────


class TestLocalRipgrepAsResolved(RipgrepBackendContract):
    @pytest.fixture
    def ripgrep_subject(self, tmp_path: Path) -> RipgrepSubject:
        backend = build_configured_ripgrep_backend(
            SimpleNamespace(ripgrep_backend="local"), max_workers=3
        )
        assert backend.is_local is True
        assert type(backend).__module__ == "cidx_backend_workspace.ripgrep"
        directory = tmp_path / "repo"
        directory.mkdir()
        return RipgrepSubject(backend, str(directory), directory)


class TestDelegatedRipgrepAsResolved(RipgrepBackendContract):
    @pytest.fixture
    def ripgrep_subject(self, fake_service: FakeWorkspaceMcp) -> RipgrepSubject:
        backend = build_configured_ripgrep_backend(
            SimpleNamespace(ripgrep_backend="delegated")
        )
        assert isinstance(backend, FakeWorkspaceMcpRipgrepBackend)
        return RipgrepSubject(backend, _CIDX_ROOT, fake_service.serve(_CIDX_ROOT))


def test_the_local_ripgrep_backend_runs_through_the_cores_executor() -> None:
    backend = local_ripgrep_backend(max_workers=5)
    runner = getattr(backend, "runner")
    assert isinstance(runner, SubprocessExecutorRunner)
    assert runner.max_workers == 5


# ── content writer ───────────────────────────────────────────────────────────


class TestLocalContentWriterAsResolved(ContentWriterContract):
    @pytest.fixture
    def writer_subject(self, tmp_path: Path) -> WriterSubject:
        writer = content_writer_plugin.resolve_content_writer_for("local")
        assert type(writer).__module__ == "cidx_backend_workspace.content_writer"
        directory = tmp_path / "repo"
        directory.mkdir()
        return WriterSubject(writer, str(directory), directory)


class TestDelegatedContentWriterAsResolved(ContentWriterContract):
    @pytest.fixture
    def writer_subject(self, fake_service: FakeWorkspaceMcp) -> WriterSubject:
        writer = content_writer_plugin.resolve_content_writer_for("delegated")
        assert isinstance(writer, FakeWorkspaceMcpContentWriter)
        assert content_writer_plugin.resolve_content_writer() is not None
        return WriterSubject(writer, _CIDX_ROOT, fake_service.serve(_CIDX_ROOT))


def test_the_local_writer_is_never_the_delegate(
    fake_service: FakeWorkspaceMcp,
) -> None:
    held = dict(extra_entry_points().get(content_writer_plugin.ENTRY_POINT_GROUP, {}))
    assert set(held) == {"local", "delegated"}
    assert isinstance(
        content_writer_plugin.resolve_content_writer(), FakeWorkspaceMcpContentWriter
    )


# ── index store ──────────────────────────────────────────────────────────────


class TestLocalIndexStoreAsResolved(IndexStoreContract):
    @pytest.fixture
    def index_store(self) -> IndexStore:
        store = build_configured_index_store(SimpleNamespace())
        assert store.is_local is True
        assert type(store).__module__ == "cidx_backend_workspace.index_store"
        return store


class TestDelegatedIndexStoreAsResolved(IndexStoreContract):
    @pytest.fixture
    def index_store(self, fake_service: FakeWorkspaceMcp) -> IndexStore:
        store = build_configured_index_store(
            SimpleNamespace(
                index_store_backend="delegated",
                content_backend="delegated",
                ripgrep_backend="delegated",
            )
        )
        assert isinstance(store, DelegatedIndexStore)
        assert isinstance(store._artifacts, FakeIndexArtifacts)
        return store


def test_path_questions_need_no_installed_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(index_store_module, "_DEFAULT", None)
    group = index_artifacts_plugin.ENTRY_POINT_GROUP
    held = dict(extra_entry_points().get(group, {}))
    remove_entry_points(group, list(held))
    try:
        index_store_seam.reset_cache()
        assert index_store_module.index_root(tmp_path) == tmp_path / ".code-indexer"
    finally:
        register_in_tree_entry_points(_REPO_ROOT)
    index_store_seam.reset_cache()
    store = index_store_module.get_index_store()
    assert type(store).__module__ == "cidx_backend_workspace.index_store"


# ── nothing installed for local ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "group, seam, resolve",
    [
        (
            ripgrep_backend_plugin.ENTRY_POINT_GROUP,
            "ripgrep",
            lambda: local_ripgrep_backend(),
        ),
        (
            content_writer_plugin.ENTRY_POINT_GROUP,
            "content_writer",
            lambda: content_writer_plugin.resolve_content_writer_for("local"),
        ),
        (
            index_artifacts_plugin.ENTRY_POINT_GROUP,
            "index_artifacts",
            lambda: index_store_seam.local_index_store(),
        ),
    ],
)
def test_no_local_module_installed_refuses_naming_the_seam(
    group: str, seam: str, resolve: Callable[[], object]
) -> None:
    held = dict(extra_entry_points().get(group, {}))
    remove_entry_points(group, list(held))
    try:
        for reset in _RESET:
            reset()
        with pytest.raises(SeamUnavailableError, match=f"seam '{seam}'"):
            resolve()
    finally:
        register_in_tree_entry_points(_REPO_ROOT)
