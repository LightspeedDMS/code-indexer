"""The git invoker as the core resolves it: through the shared seam resolver.

A delegate registered on ``code_indexer.git_invoker`` (here the testkit's fake
git-mcp, found exactly as an installed distribution is) becomes the invoker
the core's git chokepoint uses, and passes the git invoker conformance suite.
"""

from __future__ import annotations

from typing import Iterator

import pytest

from cidx_sdk.seams.git_invoker import GitInvoker
from cidx_testkit.conformance.git_invoker import GitInvokerContract
from cidx_testkit.fakes.git_mcp import (
    FakeGitMcp,
    FakeGitMcpInvoker,
    register_as_delegate,
    unregister_delegate,
)

from code_indexer.utils import git_invoker


@pytest.fixture
def resolved_invoker(monkeypatch: pytest.MonkeyPatch) -> Iterator[GitInvoker]:
    monkeypatch.setattr(git_invoker, "entry_points", lambda **_: [])
    monkeypatch.delenv(git_invoker.ENV_VAR, raising=False)
    registered = register_as_delegate(FakeGitMcp())
    git_invoker.reset_cache()
    try:
        invoker = git_invoker.get_git_invoker()
        assert isinstance(invoker, FakeGitMcpInvoker)
        yield invoker
    finally:
        unregister_delegate(registered)
        git_invoker.reset_cache()


class TestResolvedGitInvoker(GitInvokerContract):
    @pytest.fixture
    def git_invoker(self, resolved_invoker: GitInvoker) -> GitInvoker:
        return resolved_invoker


def test_no_delegate_means_native_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(git_invoker, "entry_points", lambda **_: [])
    monkeypatch.delenv(git_invoker.ENV_VAR, raising=False)
    git_invoker.reset_cache()
    try:
        assert git_invoker.get_git_invoker() is None
    finally:
        git_invoker.reset_cache()
