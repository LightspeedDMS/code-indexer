"""The MCP handlers read remote state through the configured GitBackend seam.

Before this, git_write and pull_requests shelled out to `git remote get-url`
directly, so a deployment with git_backend='delegated' had those handlers reading
the LOCAL clone while every other git-executing manager used the configured
backend -- two different answers to the same question.
"""

from __future__ import annotations

import types
from typing import Any, List

import pytest

from code_indexer.server.mcp.handlers import _git_seam, _utils
from code_indexer.server.storage.shared.git_backend import LocalGitBackend


class _Backend:
    def __init__(self) -> None:
        self.calls: List[tuple] = []

    def remote_get_url(self, repo_path: str, name: str, timeout: Any = None) -> str:
        self.calls.append((repo_path, name))
        return "https://git.example.com/org/repo.git"


def _module_with_app(backend: Any) -> types.ModuleType:
    """A real ModuleType whose `app` is ALREADY constructed (in __dict__)."""
    mod = types.ModuleType("fake_app_module")
    app = types.SimpleNamespace(state=types.SimpleNamespace(git_backend=backend))
    mod.app = app  # lands in __dict__, so no __getattr__ is involved
    return mod


def _module_that_explodes_on_lazy_access() -> types.ModuleType:
    """A real ModuleType whose `app` is LAZY -- touching it fails the test.

    This is the regression guard: `app` is a PEP-562 lazy attribute, so probing it
    with a bare getattr constructs the singleton as a side effect.
    """

    mod = types.ModuleType("lazy_app_module")

    def _raiser(name: str) -> Any:
        raise AssertionError(
            f"probed lazy attribute {name!r} -- this constructs the singleton"
        )

    mod.__getattr__ = _raiser  # type: ignore[attr-defined]
    return mod


def test_uses_the_backend_parked_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _Backend()
    monkeypatch.setattr(_utils, "app_module", _module_with_app(backend))
    assert _git_seam.configured_git_backend() is backend


def test_remote_get_url_delegates_to_that_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _Backend()
    monkeypatch.setattr(_utils, "app_module", _module_with_app(backend))
    url = _git_seam.remote_get_url("/repos/example")
    assert url == "https://git.example.com/org/repo.git"
    assert backend.calls == [("/repos/example", "origin")]


def test_remote_name_is_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _Backend()
    monkeypatch.setattr(_utils, "app_module", _module_with_app(backend))
    _git_seam.remote_get_url("/repos/example", "upstream")
    assert backend.calls == [("/repos/example", "upstream")]


def test_does_not_construct_the_lazy_app_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED against a bare getattr probe: `app` is lazily constructed (Bug #1638).

    Solo/CLI and unit tests are exactly the contexts this fallback serves, and
    exactly where the singleton has deliberately never been built.
    """
    monkeypatch.setattr(_utils, "app_module", _module_that_explodes_on_lazy_access())
    assert isinstance(_git_seam.configured_git_backend(), LocalGitBackend)
