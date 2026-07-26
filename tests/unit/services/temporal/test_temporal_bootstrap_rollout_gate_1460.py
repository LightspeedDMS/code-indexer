"""Unit tests for bootstrap_temporal_namespace_to_sister()'s rollout-safety
deletion gate (Story #1460 AC1/AC2, Epic #1454).

Story #1458 built bootstrap_temporal_namespace_to_sister() to always reclaim
(shutil.rmtree) the in-repo legacy temporal tree once its disposition's
publish (or verification) work completes. Story #1460's job: prove an
explicit `deletion_authorized` gate, defaulting to True (Story #1458's own
tests stay byte-identical), lets a caller withhold ONLY the destructive
in-repo-tree reclaim step across ALL THREE dispositions (NEEDS_BOOTSTRAP,
ALREADY_PUBLISHED, EMPTY_ARTIFACT) -- so an old/un-upgraded node that only
understands the pre-relocation clone_path/.code-indexer/index location can
still find the data via the untouched legacy tree, while a new
sister-root-resolver-aware node already finds it via the published pointer
(AC1's "mixed/bootstrap cutover state").

Real AliasManager, real filesystem, real ChunkStore/SQLite, and a real
TemporalShardResolver -- no mocking of the storage layer under test.
"""

from __future__ import annotations

import json
from pathlib import Path

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.services.temporal.temporal_bootstrap import (
    bootstrap_temporal_namespace_to_sister,
)
from code_indexer.services.temporal.temporal_bootstrap_disposition import (
    BootstrapDisposition,
)
from code_indexer.services.temporal.temporal_row_reader import read_legacy_shard_rows
from code_indexer.services.temporal.temporal_shard_resolver import (
    TemporalShardResolver,
)
from code_indexer.storage.shared.chunk_layout import write_chunks_db_discriminator
from code_indexer.storage.sqlite_chunk_store import ChunkStore


def _write_legacy_row(shard_dir: Path, point_id: str, vector, path: str) -> None:
    shard_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "id": point_id,
        "vector": vector,
        "metadata": {"language": "python"},
        "payload": {"path": path, "language": "python"},
        "chunk_text": f"content for {path}",
    }
    (shard_dir / f"vector_{point_id}.json").write_text(json.dumps(record))


def _write_valid_sister_version(version_dir: Path) -> None:
    version_dir.mkdir(parents=True, exist_ok=True)
    (version_dir / "collection_meta.json").write_text(
        json.dumps({"vector_dim": 2, "vector_size": 2})
    )
    with ChunkStore(version_dir / "chunks.db") as store:
        store.write_batch(
            [
                {
                    "id": "published1",
                    "vector": [0.5, 0.6],
                    "metadata": {},
                    "payload": {"path": "src/pub.py"},
                    "chunk_text": "published content",
                }
            ]
        )
    write_chunks_db_discriminator(version_dir)


class TestDeletionAuthorizedDefaultTruePreservesStory1458Behavior:
    def test_needs_bootstrap_default_still_reclaims(self, tmp_path: Path) -> None:
        aliases_dir = tmp_path / "aliases"
        alias_manager = AliasManager(str(aliases_dir))
        sister_root = tmp_path / "sister"
        namespace = "evolution-temporal-voyage_code_3-2024Q1"

        legacy_shard_dir = (
            tmp_path / "index" / "code-indexer-temporal-voyage_code_3-2024Q1"
        )
        _write_legacy_row(legacy_shard_dir, "row00001", [0.1, 0.2], "src/a.py")

        outcome = bootstrap_temporal_namespace_to_sister(
            alias_manager=alias_manager,
            sister_root=sister_root,
            pointer_namespace=namespace,
            legacy_shard_dir=legacy_shard_dir,
            embedder_slug="voyage_code_3",
        )

        assert outcome.disposition == BootstrapDisposition.NEEDS_BOOTSTRAP
        assert outcome.reclaimed is True
        assert outcome.deletion_gated is False
        assert not legacy_shard_dir.exists()


class TestDeletionAuthorizedFalseWithholdsReclaimAcrossDispositions:
    def test_needs_bootstrap_publishes_but_keeps_legacy_tree(
        self, tmp_path: Path
    ) -> None:
        aliases_dir = tmp_path / "aliases"
        alias_manager = AliasManager(str(aliases_dir))
        sister_root = tmp_path / "sister"
        namespace = "evolution-temporal-voyage_code_3-2024Q1"

        legacy_shard_dir = (
            tmp_path / "index" / "code-indexer-temporal-voyage_code_3-2024Q1"
        )
        _write_legacy_row(legacy_shard_dir, "row00001", [0.1, 0.2], "src/a.py")

        outcome = bootstrap_temporal_namespace_to_sister(
            alias_manager=alias_manager,
            sister_root=sister_root,
            pointer_namespace=namespace,
            legacy_shard_dir=legacy_shard_dir,
            embedder_slug="voyage_code_3",
            deletion_authorized=False,
        )

        assert outcome.disposition == BootstrapDisposition.NEEDS_BOOTSTRAP
        assert outcome.reclaimed is False
        assert outcome.deletion_gated is True
        assert outcome.records_migrated == 1

        # Destructive reclaim withheld: the in-repo legacy tree is still
        # physically present -- an old/un-upgraded node that only knows
        # about clone_path/.code-indexer/index can still find the data.
        assert legacy_shard_dir.exists()
        old_reader_rows = list(read_legacy_shard_rows(legacy_shard_dir))
        assert {row["id"] for row in old_reader_rows} == {"row00001"}

        # A new sister-root-resolver-aware node ALREADY finds the same
        # data via the published pointer -- AC1's "mixed/bootstrap
        # cutover state", proven with the real production resolver, not
        # a hand-rolled stand-in.
        resolver = TemporalShardResolver(
            alias_manager,
            "evolution",
            sister_root,
            tmp_path / "index",
        )
        resolved = resolver.resolve("voyage_code_3", "2024Q1")
        assert resolved is not None
        assert resolved.is_queryable is True
        with ChunkStore(resolved.path / "chunks.db") as store:
            assert store.read("row00001") is not None

    def test_already_published_keeps_legacy_tree_when_gated(
        self, tmp_path: Path
    ) -> None:
        aliases_dir = tmp_path / "aliases"
        alias_manager = AliasManager(str(aliases_dir))
        sister_root = tmp_path / "sister"
        namespace = "evolution-temporal-voyage_code_3-2024Q1"

        version_dir = sister_root / ".versioned" / namespace / "v_1700000000"
        _write_valid_sister_version(version_dir)
        alias_manager.create_alias(namespace, str(version_dir))

        legacy_shard_dir = (
            tmp_path / "index" / "code-indexer-temporal-voyage_code_3-2024Q1"
        )
        legacy_shard_dir.mkdir(parents=True, exist_ok=True)
        (legacy_shard_dir / "vector_published1.json").write_text(
            json.dumps(
                {
                    "id": "published1",
                    "vector": [0.5, 0.6],
                    "metadata": {},
                    "payload": {"path": "src/pub.py"},
                    "chunk_text": "published content",
                }
            )
        )

        outcome = bootstrap_temporal_namespace_to_sister(
            alias_manager=alias_manager,
            sister_root=sister_root,
            pointer_namespace=namespace,
            legacy_shard_dir=legacy_shard_dir,
            embedder_slug="voyage_code_3",
            deletion_authorized=False,
        )

        assert outcome.disposition == BootstrapDisposition.ALREADY_PUBLISHED
        assert outcome.reclaimed is False
        assert outcome.deletion_gated is True
        assert legacy_shard_dir.exists()

    def test_empty_artifact_keeps_dir_when_gated(self, tmp_path: Path) -> None:
        aliases_dir = tmp_path / "aliases"
        alias_manager = AliasManager(str(aliases_dir))
        sister_root = tmp_path / "sister"
        namespace = "evolution-temporal-voyage_code_3-2024Q1"

        rowless_dir = tmp_path / "index" / "code-indexer-temporal-voyage_code_3-2024Q1"
        rowless_dir.mkdir(parents=True)

        outcome = bootstrap_temporal_namespace_to_sister(
            alias_manager=alias_manager,
            sister_root=sister_root,
            pointer_namespace=namespace,
            legacy_shard_dir=rowless_dir,
            embedder_slug="voyage_code_3",
            deletion_authorized=False,
        )

        assert outcome.disposition == BootstrapDisposition.EMPTY_ARTIFACT
        assert outcome.reclaimed is False
        assert outcome.deletion_gated is True
        assert rowless_dir.exists()


class TestDeletionGatedReflectsPhysicalTruthNotJustTheFlag:
    """Codex review finding: deletion_gated must be True only when a REAL
    in-repo tree existed AND deletion_authorized=False caused it to be
    withheld -- never merely "the flag happened to be False this call",
    mirroring reclaimed's own physical-truth contract (True iff the tree
    is physically absent, regardless of why)."""

    def test_already_published_no_leftover_tree_reports_not_gated(
        self, tmp_path: Path
    ) -> None:
        aliases_dir = tmp_path / "aliases"
        alias_manager = AliasManager(str(aliases_dir))
        sister_root = tmp_path / "sister"
        namespace = "evolution-temporal-voyage_code_3-2024Q1"

        version_dir = sister_root / ".versioned" / namespace / "v_1700000000"
        _write_valid_sister_version(version_dir)
        alias_manager.create_alias(namespace, str(version_dir))

        legacy_shard_dir = (
            tmp_path / "index" / "code-indexer-temporal-voyage_code_3-2024Q1"
        )
        # Does not exist -- already fully reclaimed by a prior pass, so
        # there is genuinely nothing left for this gated call to withhold.

        outcome = bootstrap_temporal_namespace_to_sister(
            alias_manager=alias_manager,
            sister_root=sister_root,
            pointer_namespace=namespace,
            legacy_shard_dir=legacy_shard_dir,
            embedder_slug="voyage_code_3",
            deletion_authorized=False,
        )

        assert outcome.disposition == BootstrapDisposition.ALREADY_PUBLISHED
        # Physical truth: the tree IS absent, so reclaimed is True even
        # though deletion_authorized was False -- nothing was withheld.
        assert outcome.reclaimed is True
        assert outcome.deletion_gated is False


class TestDeletionAuthorizedGateCanBeFlippedOnLater:
    def test_gate_can_later_be_flipped_on_to_complete_reclaim(
        self, tmp_path: Path
    ) -> None:
        aliases_dir = tmp_path / "aliases"
        alias_manager = AliasManager(str(aliases_dir))
        sister_root = tmp_path / "sister"
        namespace = "evolution-temporal-voyage_code_3-2024Q1"

        legacy_shard_dir = (
            tmp_path / "index" / "code-indexer-temporal-voyage_code_3-2024Q1"
        )
        _write_legacy_row(legacy_shard_dir, "row00002", [0.7, 0.8], "src/b.py")

        gated = bootstrap_temporal_namespace_to_sister(
            alias_manager=alias_manager,
            sister_root=sister_root,
            pointer_namespace=namespace,
            legacy_shard_dir=legacy_shard_dir,
            embedder_slug="voyage_code_3",
            deletion_authorized=False,
        )
        assert gated.deletion_gated is True
        assert legacy_shard_dir.exists()

        # Operator confirms fleet-wide reader rollout and flips the gate
        # on -- a later pass over the SAME (already-published) namespace
        # now completes the reclaim.
        authorized = bootstrap_temporal_namespace_to_sister(
            alias_manager=alias_manager,
            sister_root=sister_root,
            pointer_namespace=namespace,
            legacy_shard_dir=legacy_shard_dir,
            embedder_slug="voyage_code_3",
            deletion_authorized=True,
        )

        assert authorized.disposition == BootstrapDisposition.ALREADY_PUBLISHED
        assert authorized.reclaimed is True
        assert authorized.deletion_gated is False
        assert not legacy_shard_dir.exists()
