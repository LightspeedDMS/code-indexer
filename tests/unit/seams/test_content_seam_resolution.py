"""The content seam as the core resolves it: by mode, through entry points.

Both implementations the core can be configured with are reached ONLY through
the seam resolver and must pass the same conformance suite:

* ``local`` -- whatever module registers the entry name ``local`` (in this
  tree, cidx-backend-workspace);
* ``delegated`` -- the module registered as the delegate, here the testkit's
  fake workspace-mcp, exactly as an installed delegate distribution is found.

With nothing installed for the configured mode startup refuses, naming the
seam; there is no built-in fallback.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from cidx_sdk.errors import SeamUnavailableError
from cidx_sdk.seams.resolver import extra_entry_points, remove_entry_points
from cidx_testkit.conformance.content import (
    ContentBackendContract,
    ContentSubject,
    directory_subject,
)
from cidx_testkit.fakes.workspace_mcp import (
    FakeWorkspaceMcp,
    FakeWorkspaceMcpContentBackend,
    register_as_delegate,
    unregister_delegate,
)
from cidx_testkit.registry import register_in_tree_entry_points

from code_indexer.kernel import content_seam
from code_indexer.server.services import backend_plugin_loader, content_backend_plugin
from code_indexer.server.startup.content_backend_wiring import (
    build_named_content_backend,
)
from code_indexer.server.storage.shared.content_backend import ContentBackendFactory

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CIDX_ROOT = "/srv/example/golden-repos/example-repo"
_GROUP = "code_indexer.content_backend"


@pytest.fixture(autouse=True)
def _fresh_resolvers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No ambient install: only what this tree declares, plus what a test adds."""
    monkeypatch.setattr(backend_plugin_loader, "entry_points", lambda **_: [])
    monkeypatch.setattr(content_seam, "entry_points", lambda **_: [])
    monkeypatch.delenv("CIDX_CONTENT_BACKEND", raising=False)
    content_backend_plugin.reset_cache()
    content_seam.reset_cache()
    yield
    content_backend_plugin.reset_cache()
    content_seam.reset_cache()


@pytest.fixture
def fake_service(tmp_path: Path) -> Iterator[FakeWorkspaceMcp]:
    service = FakeWorkspaceMcp(store=tmp_path / "store")
    registered = register_as_delegate(service)
    content_backend_plugin.reset_cache()
    try:
        yield service
    finally:
        unregister_delegate(registered)


class TestLocalModeAsResolved(ContentBackendContract):
    @pytest.fixture
    def content_subject(self, tmp_path: Path) -> ContentSubject:
        backend = build_named_content_backend("local")
        assert backend.is_local is True
        return directory_subject(backend, tmp_path / "repo")


class TestDelegatedModeAsResolved(ContentBackendContract):
    @pytest.fixture
    def content_subject(
        self, tmp_path: Path, fake_service: FakeWorkspaceMcp
    ) -> ContentSubject:
        backend = build_named_content_backend("delegated")
        assert isinstance(backend, FakeWorkspaceMcpContentBackend)
        directory = fake_service.serve(_CIDX_ROOT)
        return directory_subject(backend, directory, root=_CIDX_ROOT)


def test_every_local_construction_goes_through_the_resolver() -> None:
    assert type(ContentBackendFactory.create("local")).__module__ == (
        "cidx_backend_workspace.content"
    )
    assert content_seam.content_backend_for("local").is_local is True


def test_no_local_module_installed_refuses_naming_the_seam() -> None:
    held = dict(extra_entry_points().get(_GROUP, {}))
    remove_entry_points(_GROUP, list(held))
    try:
        content_backend_plugin.reset_cache()
        content_seam.reset_cache()
        with pytest.raises(SeamUnavailableError, match="seam 'content'"):
            build_named_content_backend("local")
        with pytest.raises(SeamUnavailableError, match="seam 'content'"):
            content_seam.content_backend_for("local")
    finally:
        register_in_tree_entry_points(_REPO_ROOT)


def test_a_delegate_is_never_picked_for_local(fake_service: FakeWorkspaceMcp) -> None:
    assert build_named_content_backend("local").is_local is True
    assert build_named_content_backend("delegated").is_local is False
