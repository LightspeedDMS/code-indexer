"""The git seam as the core resolves it: by mode, through entry points.

Both implementations the core can be configured with are reached ONLY through
the seam resolver and must pass the same conformance suite:

* ``local`` -- whatever module registers the entry name ``local`` (in this
  tree, cidx-backend-git), given the core's git process runner;
* ``delegated`` -- the module registered as the delegate, here the testkit's
  fake git-mcp, exactly as an installed delegate distribution is found.

With nothing installed for the configured mode the core refuses, naming the
seam; there is no built-in fallback.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Iterator, List

import pytest

from cidx_sdk.errors import SeamUnavailableError
from cidx_sdk.seams.git import GitProcessRunner
from cidx_sdk.seams.resolver import extra_entry_points, remove_entry_points
from cidx_testkit.conformance.git import (
    GitBackendContract,
    GitSubject,
    local_subject,
    seed_upstream,
)
from cidx_testkit.fakes.git_mcp import (
    FakeGitMcp,
    FakeGitMcpGitBackend,
    register_as_delegate,
    unregister_delegate,
)
from cidx_testkit.registry import register_in_tree_entry_points

from code_indexer.server.services import backend_plugin_loader, git_backend_plugin
from code_indexer.server.startup.git_backend_wiring import build_named_git_backend
from code_indexer.server.storage.shared import git_backend
from code_indexer.server.storage.shared.git_backend import (
    CoreGitProcessRunner,
    GitBackendFactory,
    LocalGitBackend,
    local_git_backend,
)
from code_indexer.utils import git_invoker

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CIDX_ROOT = "/srv/example-cidx/golden-repos"
_GROUP = "code_indexer.git_backend"


@pytest.fixture(autouse=True)
def _fresh_resolvers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No ambient install: only what this tree declares, plus what a test adds."""
    monkeypatch.setattr(backend_plugin_loader, "entry_points", lambda **_: [])
    monkeypatch.setattr(git_invoker, "entry_points", lambda **_: [])
    monkeypatch.delenv("CIDX_GIT_BACKEND", raising=False)
    monkeypatch.delenv(git_invoker.ENV_VAR, raising=False)
    git_backend_plugin.reset_cache()
    git_invoker.reset_cache()
    yield
    git_backend_plugin.reset_cache()
    git_invoker.reset_cache()


@pytest.fixture
def fake_service(tmp_path: Path) -> Iterator[FakeGitMcp]:
    held = tmp_path / "service"
    held.mkdir()
    service = FakeGitMcp(roots={_CIDX_ROOT: held})
    registered = register_as_delegate(service)
    git_backend_plugin.reset_cache()
    git_invoker.reset_cache()
    try:
        yield service
    finally:
        unregister_delegate(registered)
        git_invoker.reset_cache()


class TestLocalModeAsResolved(GitBackendContract):
    @pytest.fixture
    def git_subject(self, tmp_path: Path) -> GitSubject:
        backend = build_named_git_backend("local")
        assert isinstance(backend, LocalGitBackend)
        return local_subject(backend, tmp_path)


class TestDelegatedModeAsResolved(GitBackendContract):
    @pytest.fixture
    def git_subject(self, tmp_path: Path, fake_service: FakeGitMcp) -> GitSubject:
        backend = build_named_git_backend("delegated")
        assert isinstance(backend, FakeGitMcpGitBackend)
        upstream = seed_upstream(tmp_path / "upstream")
        return GitSubject(backend, _CIDX_ROOT, str(upstream), upstream)


def test_every_local_construction_goes_through_the_resolver() -> None:
    for backend in (local_git_backend(), GitBackendFactory.create("local")):
        assert type(backend).__module__ == "cidx_backend_git.local"
        assert isinstance(backend, LocalGitBackend)
        assert isinstance(backend.runner, CoreGitProcessRunner)


def test_the_core_runner_is_the_core_git_funnel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``run_git_command`` is looked up when a command runs, so a patch of
    ``git_backend.run_git_command`` reaches the resolved local backend."""
    calls: List[Any] = []

    def fake(cmd: List[str], **kwargs: Any) -> "subprocess.CompletedProcess[str]":
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, "main\n", "")

    monkeypatch.setattr(git_backend, "run_git_command", fake)
    monkeypatch.setattr(git_backend, "run_git_command_with_retry", fake)
    backend = local_git_backend()
    assert backend.current_branch(str(tmp_path)) == "main"
    backend.fetch(str(tmp_path), retry=True, credentials_url="https://example.com/r")
    assert calls[0] == (
        ["git", "symbolic-ref", "--short", "HEAD"],
        {"cwd": tmp_path, "check": True, "timeout": None},
    )
    assert calls[1][1]["credentials_url"] == "https://example.com/r"
    assert isinstance(CoreGitProcessRunner(), GitProcessRunner)


def test_no_local_module_installed_refuses_naming_the_seam() -> None:
    held = dict(extra_entry_points().get(_GROUP, {}))
    remove_entry_points(_GROUP, list(held))
    try:
        git_backend_plugin.reset_cache()
        with pytest.raises(SeamUnavailableError, match="seam 'git'"):
            build_named_git_backend("local")
        with pytest.raises(SeamUnavailableError, match="seam 'git'"):
            local_git_backend()
    finally:
        register_in_tree_entry_points(_REPO_ROOT)


def test_a_delegate_is_never_picked_for_local(fake_service: FakeGitMcp) -> None:
    assert isinstance(build_named_git_backend("local"), LocalGitBackend)
    assert isinstance(build_named_git_backend("delegated"), FakeGitMcpGitBackend)
