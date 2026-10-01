"""
Story #1589: `clear_all_dedup_states` must be part of the
`GoldenRepoMetadataBackend` Protocol contract -- not merely an incidental
method some backend happens to have. Mirrors Bug #1414's
`TestGoldenRepoMetadataBackendProtocolDeclaresTemporalOptions` pattern.

The EXISTING reflection-driven conformance test in
test_golden_repo_metadata_protocol_conformance_1414.py
(`test_backend_implements_every_protocol_member`) automatically extends to
cover this new member for BOTH concrete backends once it is declared here --
no duplicate list to maintain.
"""

from code_indexer.server.storage.protocols import GoldenRepoMetadataBackend


def _protocol_public_members() -> set:
    return {m for m in dir(GoldenRepoMetadataBackend) if not m.startswith("_")}


class TestGoldenRepoMetadataBackendProtocolDeclaresClearAllDedupStates:
    def test_protocol_declares_clear_all_dedup_states(self) -> None:
        assert "clear_all_dedup_states" in _protocol_public_members()
