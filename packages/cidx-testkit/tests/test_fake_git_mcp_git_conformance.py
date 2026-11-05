"""The fake git-mcp's git backend passes the git seam's conformance suite,
addressed by paths that exist only at the service."""

from __future__ import annotations

from pathlib import Path

import pytest

from cidx_sdk.seams.git import GitBackend, GitCommandRunner
from cidx_testkit.conformance.git import GitBackendContract, GitSubject, seed_upstream
from cidx_testkit.fakes.git_mcp import FakeGitMcp, FakeGitMcpGitBackend

#: Where cidx thinks the repositories are; nothing exists there on this machine.
CIDX_ROOT = "/srv/example-cidx/repos"


def fake_subject(tmp_path: Path) -> GitSubject:
    held = tmp_path / "service"
    held.mkdir()
    service = FakeGitMcp(roots={CIDX_ROOT: held})
    upstream = seed_upstream(tmp_path / "upstream")
    return GitSubject(FakeGitMcpGitBackend(service), CIDX_ROOT, str(upstream), upstream)


class TestFakeGitMcpGitBackend(GitBackendContract):
    @pytest.fixture
    def git_subject(self, tmp_path: Path) -> GitSubject:
        return fake_subject(tmp_path)


def test_the_fake_has_the_delegate_shape(tmp_path: Path) -> None:
    subject = fake_subject(tmp_path)
    backend = subject.backend
    assert isinstance(backend, GitBackend) and isinstance(backend, GitCommandRunner)
    assert backend.owns_credentials is True


def test_paths_are_mapped_onto_the_service(tmp_path: Path) -> None:
    subject = fake_subject(tmp_path)
    assert isinstance(subject.backend, FakeGitMcpGitBackend)
    subject.backend.clone(subject.upstream, f"{CIDX_ROOT}/one")
    assert (tmp_path / "service" / "one" / "a.txt").is_file()
    assert not Path(CIDX_ROOT).exists()
    assert subject.backend.service.requests[-1][1] == str(tmp_path / "service")
