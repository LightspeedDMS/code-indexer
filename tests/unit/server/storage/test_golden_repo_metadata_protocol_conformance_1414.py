"""
Bug #1414 DoD item 4: protocol-conformance test for GoldenRepoMetadataBackend.

Root cause context: GoldenRepoMetadataPostgresBackend was missing
update_temporal_options entirely (only update_enable_temporal and
update_repo_url existed), and the GoldenRepoMetadataBackend Protocol did not
declare it either -- so mypy could not catch the mismatch at the `Any`-typed
injection site in service_init.py (GoldenRepoManager._sqlite_backend).
GoldenRepoManager.save_temporal_options() (the Web UI's only write path)
calling .update_temporal_options(...) on the injected PG backend in cluster
mode raised an unhandled AttributeError -> HTTP 500, persisting nothing.

The PRE-EXISTING "protocol has required methods" test in
test_protocols_tracking.py (TestGoldenRepoMetadataBackend.test_protocol_has_
required_methods) checked the Protocol against a hand-maintained hardcoded
set -- and critically, checked the Protocol against ITSELF (dir(Protocol)),
never against either concrete backend implementation. A hand-maintained list
can drift silently (nobody remembers to add a new method to it either), and
checking the Protocol against itself can never catch a backend that fails to
implement a Protocol member.

This module fixes both gaps: reflection is driven by dir(Protocol) directly
(never a list a human must remember to update), and conformance is verified
against BOTH concrete implementations (SQLite solo, PostgreSQL cluster).
"""

import pytest

from code_indexer.server.storage.protocols import GoldenRepoMetadataBackend
from code_indexer.server.storage.sqlite_backends import (
    GoldenRepoMetadataSqliteBackend,
)
from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
    GoldenRepoMetadataPostgresBackend,
)


def _protocol_public_members() -> set:
    """Public members the Protocol declares, via reflection (never a
    hand-maintained list)."""
    return {m for m in dir(GoldenRepoMetadataBackend) if not m.startswith("_")}


class TestGoldenRepoMetadataBackendProtocolDeclaresTemporalOptions:
    def test_protocol_declares_update_temporal_options(self) -> None:
        """
        Bug #1414: update_temporal_options must be part of the Protocol
        contract -- not merely an incidental method some backends happen to
        have. Without this, a backend implementation is free to omit it and
        no static or runtime check would flag the gap.
        """
        assert "update_temporal_options" in _protocol_public_members()


class TestGoldenRepoMetadataBackendImplementationsConformToProtocol:
    """
    Bug #1414: every concrete GoldenRepoMetadataBackend implementation must
    implement EVERY member the Protocol declares. Reflection-driven
    (dir(GoldenRepoMetadataBackend)), never a second hand-maintained list --
    that duplication is exactly what let update_temporal_options silently
    exist on SQLite but not PostgreSQL for as long as it did.
    """

    @pytest.mark.parametrize(
        "impl_cls",
        [GoldenRepoMetadataSqliteBackend, GoldenRepoMetadataPostgresBackend],
        ids=["sqlite", "postgres"],
    )
    def test_backend_implements_every_protocol_member(self, impl_cls) -> None:
        protocol_members = _protocol_public_members()
        missing = [
            member
            for member in sorted(protocol_members)
            if not callable(getattr(impl_cls, member, None))
        ]
        assert not missing, (
            f"{impl_cls.__name__} is missing Protocol member(s): {missing}. "
            "This is exactly the class of bug #1414 fixed: a backend that "
            "silently fails to implement a Protocol member causes an "
            "AttributeError at call time (in cluster mode, often manifesting "
            "as an unhandled HTTP 500), never caught by mypy because the "
            "injection site is Any-typed."
        )
