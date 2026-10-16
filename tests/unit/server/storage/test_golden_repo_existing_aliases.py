"""GoldenRepoMetadataSqliteBackend.existing_aliases: one bounded membership
lookup for a given set of names (public #1984).

The post-search access filter asks which of the caller's activation aliases
are also golden repository names. The answer must come from a query bounded
to those names, never a listing of every golden repository.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Iterator

import pytest

from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend


@pytest.fixture
def backend() -> Iterator[GoldenRepoMetadataSqliteBackend]:
    with tempfile.TemporaryDirectory() as tmp:
        be = GoldenRepoMetadataSqliteBackend(str(Path(tmp) / "golden.db"))
        be.ensure_table_exists()
        yield be


def _add(backend: GoldenRepoMetadataSqliteBackend, alias: str) -> None:
    backend.add_repo(
        alias=alias,
        repo_url=f"https://git.example.com/example/{alias}.git",
        default_branch="main",
        clone_path=f"/data/golden-repos/{alias}",
        created_at="2024-01-01T00:00:00+00:00",
    )


class TestProtocolDeclaresExistingAliases:
    def test_protocol_declares_existing_aliases(self) -> None:
        # Both concrete backends are checked against the Protocol by
        # test_golden_repo_metadata_protocol_conformance_1414.
        from code_indexer.server.storage.protocols import GoldenRepoMetadataBackend

        assert "existing_aliases" in dir(GoldenRepoMetadataBackend)


class TestExistingAliases:
    def test_returns_only_the_requested_names_that_exist(self, backend):
        for alias in ("example-repo", "second-repo", "other-repo"):
            _add(backend, alias)

        found = backend.existing_aliases(["example-repo", "my-alias", "other-repo"])

        assert found == {"example-repo", "other-repo"}

    def test_empty_request_returns_empty_set(self, backend):
        _add(backend, "example-repo")

        assert backend.existing_aliases([]) == set()

    def test_large_request_is_answered_in_full(self, backend):
        for i in range(5):
            _add(backend, f"repo-{i}")
        names = [f"missing-{i}" for i in range(2500)] + ["repo-1", "repo-4"]

        assert backend.existing_aliases(names) == {"repo-1", "repo-4"}
