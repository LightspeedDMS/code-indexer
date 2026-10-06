"""The MCP handlers read remote state through the configured GitBackend seam.

Before this, git_write and pull_requests shelled out to `git remote get-url`
directly, so a deployment with git_backend='delegated' had those handlers reading
the LOCAL clone while every other git-executing manager used the configured
backend -- two different answers to the same question.
"""

from __future__ import annotations

from typing import Any, List

from code_indexer.server.mcp.handlers._git_seam import (
    configured_git_backend,
    remote_get_url,
)
from code_indexer.server.storage.shared.git_backend import LocalGitBackend


class _Backend:
    def __init__(self) -> None:
        self.calls: List[tuple] = []

    def remote_get_url(self, repo_path: str, name: str, timeout: Any = None) -> str:
        self.calls.append((repo_path, name))
        return "https://git.example.com/org/repo.git"


class _State:
    def __init__(self, backend: Any) -> None:
        self.git_backend = backend


class _App:
    def __init__(self, backend: Any) -> None:
        self.state = _State(backend)


class _AppModule:
    def __init__(self, backend: Any) -> None:
        self.app = _App(backend)


def test_uses_the_backend_parked_at_startup() -> None:
    backend = _Backend()
    assert configured_git_backend(_AppModule(backend)) is backend


def test_remote_get_url_delegates_to_that_backend() -> None:
    backend = _Backend()
    url = remote_get_url("/repos/example", app_module=_AppModule(backend))
    assert url == "https://git.example.com/org/repo.git"
    assert backend.calls == [("/repos/example", "origin")]


def test_remote_name_is_forwarded() -> None:
    backend = _Backend()
    remote_get_url("/repos/example", "upstream", app_module=_AppModule(backend))
    assert backend.calls == [("/repos/example", "upstream")]


def test_falls_back_to_local_when_startup_parked_nothing() -> None:
    # Solo/CLI and unit tests never run lifespan; they must keep today's behaviour.
    assert isinstance(configured_git_backend(_AppModule(None)), LocalGitBackend)
    assert isinstance(configured_git_backend(None), LocalGitBackend)


def test_falls_back_when_app_module_has_no_app() -> None:
    class _Bare:
        pass

    assert isinstance(configured_git_backend(_Bare()), LocalGitBackend)
