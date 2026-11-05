"""Conformance suite for the SCIP seam (``cidx_sdk.seams.scip``).

Every SCIP execution backend -- each delegate, the testkit's sandbox-mcp
fake -- must pass the same checks. Subclass :class:`ScipBackendContract` and
provide the ``scip_subject`` fixture: a :class:`ScipSubject` holding the
backend under test, the repository path it is addressed with, a way to lay
files out in that repository, and a language the backend cannot index.

The checks are the seam's contract as the core relies on it
(``cidx_scip``'s plugin indexer): a known failure is an OUTCOME, never
an exception; an ``ok`` outcome carries bytes whose sha256 is the one it
reports (the core re-hashes and fails the project otherwise); the status is
one of :data:`~cidx_sdk.seams.scip.SCIP_STATUSES`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import pytest

from cidx_sdk.seams.scip import (
    SCIP_STATUSES,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_UNSUPPORTED,
    ScipBackend,
    ScipBuildOutcome,
    ScipBuildRequest,
    sha256_hex,
)


@dataclass
class ScipSubject:
    """A backend under test and the repository it builds.

    Attributes:
        backend: the implementation under test.
        repo_path: the repository path as the BACKEND is addressed with it.
        golden_alias: the alias sent with the request (None: none).
        write: ``write(rel, data)`` creates a file (and its directories).
        language: a language the backend can index.
        build_system: a build system of that language.
        unsupported_language: a language the backend answers ``unsupported``.
        missing_repo_path: a repository the backend cannot address.
    """

    backend: ScipBackend
    repo_path: str
    golden_alias: Optional[str]
    write: Callable[[str, bytes], None]
    language: str = "python"
    build_system: str = "pip"
    unsupported_language: str = "example-unsupported"
    missing_repo_path: str = "/nonexistent/example-repo"


def directory_writer(directory: Path) -> Callable[[str, bytes], None]:
    """``write(rel, data)`` into *directory*."""
    directory.mkdir(parents=True, exist_ok=True)

    def write(rel: str, data: bytes) -> None:
        target = directory / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    return write


class ScipBackendContract:
    """The SCIP seam's contract. Subclass and provide ``scip_subject``."""

    @pytest.fixture
    def scip_subject(self, tmp_path: Path) -> ScipSubject:
        raise NotImplementedError("provide the backend under test")

    @pytest.fixture
    def populated(self, scip_subject: ScipSubject) -> ScipSubject:
        scip_subject.write("setup.py", b"from setuptools import setup\nsetup()\n")
        scip_subject.write("src/app.py", b"def main() -> int:\n    return 0\n")
        scip_subject.write("pkg/sub/mod.py", b"VALUE = 1\n")
        return scip_subject

    @staticmethod
    def request(
        subject: ScipSubject,
        *,
        project_path: str = ".",
        language: Optional[str] = None,
        repo_path: Optional[str] = None,
    ) -> ScipBuildRequest:
        return ScipBuildRequest(
            repo_path=repo_path or subject.repo_path,
            golden_alias=subject.golden_alias if repo_path is None else None,
            project_path=project_path,
            build_system=subject.build_system,
            language=language or subject.language,
            timeout_seconds=60,
        )

    @staticmethod
    def build(subject: ScipSubject, request: ScipBuildRequest) -> ScipBuildOutcome:
        outcome = subject.backend.build_index(request)
        assert isinstance(outcome, ScipBuildOutcome), type(outcome)
        assert outcome.status in SCIP_STATUSES, outcome.status
        return outcome

    def test_satisfies_the_protocol(self, scip_subject: ScipSubject) -> None:
        assert isinstance(scip_subject.backend, ScipBackend)

    def test_reports_itself_available(self, scip_subject: ScipSubject) -> None:
        assert scip_subject.backend.is_available() is True

    def test_an_ok_build_carries_bytes_matching_their_sha256(
        self, populated: ScipSubject
    ) -> None:
        outcome = self.build(populated, self.request(populated))
        assert outcome.status == STATUS_OK, outcome.error
        assert outcome.index_bytes
        assert outcome.sha256 == sha256_hex(outcome.index_bytes)

    def test_a_subproject_builds_on_its_own(self, populated: ScipSubject) -> None:
        whole = self.build(populated, self.request(populated))
        sub = self.build(populated, self.request(populated, project_path="pkg"))
        assert sub.status == STATUS_OK, sub.error
        assert sub.index_bytes and sub.index_bytes != whole.index_bytes

    def test_a_large_index_comes_back_whole(self, populated: ScipSubject) -> None:
        for number in range(200):
            populated.write(
                f"generated/module_{number:04d}.py",
                f"VALUE_{number} = {number}\n".encode(),
            )
        outcome = self.build(populated, self.request(populated))
        assert outcome.status == STATUS_OK, outcome.error
        assert outcome.index_bytes is not None
        assert outcome.sha256 == sha256_hex(outcome.index_bytes)

    def test_an_unsupported_language_is_an_outcome_not_an_error(
        self, populated: ScipSubject
    ) -> None:
        outcome = self.build(
            populated,
            self.request(populated, language=populated.unsupported_language),
        )
        assert outcome.status == STATUS_UNSUPPORTED
        assert outcome.index_bytes is None

    def test_a_repository_it_cannot_address_fails_loudly(
        self, populated: ScipSubject
    ) -> None:
        outcome = self.build(
            populated, self.request(populated, repo_path=populated.missing_repo_path)
        )
        assert outcome.status == STATUS_FAILED
        assert outcome.error
        assert outcome.index_bytes is None
