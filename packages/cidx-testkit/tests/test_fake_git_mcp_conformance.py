"""The fake git-mcp's invoker passes the git invoker suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from cidx_sdk.seams.git_invoker import GitInvoker
from cidx_testkit.conformance.git_invoker import GitInvokerContract
from cidx_testkit.fakes.git_mcp import FakeGitMcp, FakeGitMcpInvoker


class TestFakeGitMcpInvoker(GitInvokerContract):
    @pytest.fixture
    def git_invoker(self) -> GitInvoker:
        return FakeGitMcpInvoker(FakeGitMcp())


def test_requests_are_recorded(tmp_path: Path) -> None:
    service = FakeGitMcp()
    FakeGitMcpInvoker(service).run(["git", "--version"], tmp_path)
    assert service.requests == [(("git", "--version"), str(tmp_path))]
