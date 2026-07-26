"""Unit tests for bootstrap_temporal_namespace_to_sister() (Story #1458
AC1a / AC1's binding of Story #1457's AC11).

Story #1457's AC11 (one-time proactive bootstrap of pre-existing in-repo
temporal shards to the sister location) was explicitly left structurally
blocked on Story #1458's per-repo fleet-migration job, write lock, and
in-process reclamation context. This module is that completion: it composes
the forward-compatible primitives Story #1457 already built
(`classify_bootstrap_disposition`, `read_legacy_shard_rows`) with Story
#1457's own build/publish primitives (`build_fresh_consolidated_temporal_
version`, `publish_temporal_shard_version`) to actually perform the
migrate-then-reclaim / sweep-as-empty-artifact / verify-and-reclaim
dispositions for ONE (embedder, quarter) namespace.

Real AliasManager, real filesystem, real ChunkStore/SQLite -- no mocking of
the storage layer under test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.services.temporal.temporal_bootstrap_disposition import (
    BootstrapDisposition,
)
from code_indexer.services.temporal.temporal_bootstrap import (
    bootstrap_temporal_namespace_to_sister,
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
    """Build a genuinely valid, queryable chunks_db-layout sister version
    -- real committed data + discriminator, matching what
    build_fresh_consolidated_temporal_version actually produces (not an
    empty chunks.db.touch() stand-in)."""
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


class TestBootstrapAlreadyPublished:
    def test_reclaims_leftover_in_repo_tree_when_already_published(
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
        # Codex round-6 CRITICAL finding #2: the leftover legacy row's id
        # must genuinely be ALREADY covered by the published sister
        # version -- "published1" is the exact id _write_valid_sister_
        # version() writes -- to faithfully simulate a real
        # already-published scenario (a mismatched/unrelated id here
        # would instead reproduce the exploit the CRITICAL #2 fix closes).
        #
        # Round-8 CRITICAL finding: "covered" now means CONTENT-equal, not
        # merely id-equal -- write the legacy row byte-identical to
        # _write_valid_sister_version()'s hardcoded "published1" record
        # (same metadata/payload/chunk_text) instead of the generic
        # _write_legacy_row() helper's different shape, so this test
        # exercises the genuine ALREADY_PUBLISHED reclaim path rather than
        # accidentally tripping the new content-mismatch refusal.
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
        )

        assert outcome.disposition == BootstrapDisposition.ALREADY_PUBLISHED
        assert outcome.reclaimed is True
        assert not legacy_shard_dir.exists()
        # The published pointer is untouched -- still points at the same version.
        assert alias_manager.read_alias(namespace) == str(version_dir)

    def test_noop_when_already_published_and_no_leftover_tree(
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
        # Does not exist -- already fully reclaimed.

        outcome = bootstrap_temporal_namespace_to_sister(
            alias_manager=alias_manager,
            sister_root=sister_root,
            pointer_namespace=namespace,
            legacy_shard_dir=legacy_shard_dir,
            embedder_slug="voyage_code_3",
        )

        assert outcome.disposition == BootstrapDisposition.ALREADY_PUBLISHED
        assert outcome.reclaimed is True


class TestBootstrapAlreadyPublishedValidatesPointerTarget:
    """Codex Finding #5 (CRITICAL): ALREADY_PUBLISHED disposition is
    determined solely by alias_exists() -- a dangling/stale/invalid alias
    pointer must NEVER trigger shutil.rmtree on the only intact copy of
    the legacy temporal data. The pointer TARGET must be resolved and
    validated as a real, queryable sister version first."""

    def test_refuses_reclaim_when_alias_points_at_a_nonexistent_directory(
        self, tmp_path: Path
    ) -> None:
        aliases_dir = tmp_path / "aliases"
        alias_manager = AliasManager(str(aliases_dir))
        sister_root = tmp_path / "sister"
        namespace = "evolution-temporal-voyage_code_3-2024Q1"

        # A dangling pointer: alias file exists, but its target directory
        # does not (simulates a stale/corrupted pointer, or a version that
        # was deleted out from under the alias).
        dangling_target = sister_root / ".versioned" / namespace / "v_1700000000"
        alias_manager.create_alias(namespace, str(dangling_target))
        assert not dangling_target.exists()

        legacy_shard_dir = (
            tmp_path / "index" / "code-indexer-temporal-voyage_code_3-2024Q1"
        )
        _write_legacy_row(legacy_shard_dir, "onlycopy1", [0.1, 0.2], "src/a.py")

        with pytest.raises(RuntimeError):
            bootstrap_temporal_namespace_to_sister(
                alias_manager=alias_manager,
                sister_root=sister_root,
                pointer_namespace=namespace,
                legacy_shard_dir=legacy_shard_dir,
                embedder_slug="voyage_code_3",
            )

        assert legacy_shard_dir.exists(), (
            "Bug: the ONLY intact copy of this temporal data was deleted "
            "based on alias EXISTENCE alone, despite the pointer target "
            "not actually existing on disk."
        )

    def test_refuses_reclaim_when_alias_target_exists_but_is_not_a_valid_version(
        self, tmp_path: Path
    ) -> None:
        aliases_dir = tmp_path / "aliases"
        alias_manager = AliasManager(str(aliases_dir))
        sister_root = tmp_path / "sister"
        namespace = "evolution-temporal-voyage_code_3-2024Q1"

        # The target directory exists but is NOT a real, committed,
        # queryable chunks_db version (no discriminator, no data) --
        # simulates a crashed/partial publish or filesystem corruption.
        invalid_target = sister_root / ".versioned" / namespace / "v_1700000000"
        invalid_target.mkdir(parents=True)

        alias_manager.create_alias(namespace, str(invalid_target))

        legacy_shard_dir = (
            tmp_path / "index" / "code-indexer-temporal-voyage_code_3-2024Q1"
        )
        _write_legacy_row(legacy_shard_dir, "onlycopy2", [0.3, 0.4], "src/b.py")

        with pytest.raises(RuntimeError):
            bootstrap_temporal_namespace_to_sister(
                alias_manager=alias_manager,
                sister_root=sister_root,
                pointer_namespace=namespace,
                legacy_shard_dir=legacy_shard_dir,
                embedder_slug="voyage_code_3",
            )

        assert legacy_shard_dir.exists(), (
            "Bug: the ONLY intact copy of this temporal data was deleted "
            "even though the pointer target exists but is not a valid, "
            "queryable consolidated version."
        )

    def test_refuses_reclaim_when_alias_target_is_outside_the_expected_namespace_directory(
        self, tmp_path: Path
    ) -> None:
        """Codex Finding #5 (CRITICAL, round 2): content validity alone is
        NOT sufficient -- a pointer that resolves to a coincidentally
        valid, queryable chunks_db version LOCATED SOMEWHERE ELSE (not the
        expected sister_root/.versioned/{pointer_namespace}/ directory)
        must still be refused. Otherwise a corrupted/malicious/mistaken
        pointer could validate against an unrelated database and trigger
        deletion of the only intact legacy copy."""
        aliases_dir = tmp_path / "aliases"
        alias_manager = AliasManager(str(aliases_dir))
        sister_root = tmp_path / "sister"
        namespace = "evolution-temporal-voyage_code_3-2024Q1"

        # A genuinely VALID, queryable chunks_db version -- but at the
        # WRONG location (outside sister_root/.versioned/{namespace}/).
        wrong_location = tmp_path / "unrelated-elsewhere" / "v_1700000000"
        _write_valid_sister_version(wrong_location)
        alias_manager.create_alias(namespace, str(wrong_location))

        legacy_shard_dir = (
            tmp_path / "index" / "code-indexer-temporal-voyage_code_3-2024Q1"
        )
        _write_legacy_row(legacy_shard_dir, "onlycopy3", [0.5, 0.6], "src/c.py")

        with pytest.raises(RuntimeError):
            bootstrap_temporal_namespace_to_sister(
                alias_manager=alias_manager,
                sister_root=sister_root,
                pointer_namespace=namespace,
                legacy_shard_dir=legacy_shard_dir,
                embedder_slug="voyage_code_3",
            )

        assert legacy_shard_dir.exists(), (
            "Bug: the ONLY intact copy of this temporal data was deleted "
            "based on a pointer target that was content-valid but NOT "
            "confined to the expected namespace directory."
        )

    def test_pointer_target_opened_in_immutable_mode_not_mutable(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Codex Finding #5 (CRITICAL, round 2): validating an
        ALREADY_PUBLISHED pointer's target must NEVER open it with a
        MUTABLE ChunkStore -- this project's CLAUDE.md "Golden Repo
        Versioned Path" invariant is absolute: NEVER modify/checkout/index
        inside .versioned/. Opening mutable (even read-only-in-practice)
        risks creating/touching journal files inside the immutable
        snapshot tree."""
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
        # No leftover legacy tree -- exercises the "noop, nothing to
        # reclaim" ALREADY_PUBLISHED branch, still calling validation.

        seen_immutable_flags = []
        original_init = ChunkStore.__init__

        def _spy_init(self, db_path, *, immutable=False, expected_dim=None):
            seen_immutable_flags.append(immutable)
            return original_init(
                self, db_path, immutable=immutable, expected_dim=expected_dim
            )

        # Patch the class directly at its actual origin
        # (code_indexer.storage.sqlite_chunk_store.ChunkStore) -- this is
        # the SAME class object regardless of which module imports it, so
        # this also covers the open_chunk_store_for_path() indirection
        # temporal_bootstrap.py now uses (Codex Finding #5 round-2 fix),
        # not just a direct ChunkStore(...) construction.
        monkeypatch.setattr(ChunkStore, "__init__", _spy_init)

        bootstrap_temporal_namespace_to_sister(
            alias_manager=alias_manager,
            sister_root=sister_root,
            pointer_namespace=namespace,
            legacy_shard_dir=legacy_shard_dir,
            embedder_slug="voyage_code_3",
        )

        assert seen_immutable_flags, "ChunkStore was never opened for validation"
        assert all(seen_immutable_flags), (
            f"Bug: the ALREADY_PUBLISHED pointer target was opened with "
            f"immutable=False at least once: {seen_immutable_flags} -- "
            f"a mutable connection into .versioned/ violates this "
            f"project's absolute invariant."
        )


class TestBootstrapAlreadyPublishedValidatesContentCoverage:
    """Codex round-6 CRITICAL finding #2: the pointer validator only
    proved confinement, canonical shape, that the store opens, and
    count() > 0 -- it NEVER checked that the published target actually
    CONTAINS the legacy records it's about to delete. Real repro: give
    the published target ONE unrelated record; validation passes; the
    legacy tree (containing a DIFFERENT record that exists nowhere else)
    gets deleted, permanently losing that data."""

    def test_refuses_reclaim_when_published_target_does_not_contain_the_legacy_ids(
        self, tmp_path: Path
    ) -> None:
        aliases_dir = tmp_path / "aliases"
        alias_manager = AliasManager(str(aliases_dir))
        sister_root = tmp_path / "sister"
        namespace = "evolution-temporal-voyage_code_3-2024Q1"

        # A genuinely VALID, queryable chunks_db version -- but its ONE
        # record ("published1") is UNRELATED to the legacy data below.
        version_dir = sister_root / ".versioned" / namespace / "v_1700000000"
        _write_valid_sister_version(version_dir)
        alias_manager.create_alias(namespace, str(version_dir))

        legacy_shard_dir = (
            tmp_path / "index" / "code-indexer-temporal-voyage_code_3-2024Q1"
        )
        _write_legacy_row(
            legacy_shard_dir, "only-copy-unrelated", [0.9, 0.9], "src/unique.py"
        )

        with pytest.raises(RuntimeError):
            bootstrap_temporal_namespace_to_sister(
                alias_manager=alias_manager,
                sister_root=sister_root,
                pointer_namespace=namespace,
                legacy_shard_dir=legacy_shard_dir,
                embedder_slug="voyage_code_3",
            )

        assert legacy_shard_dir.exists(), (
            "Bug: the ONLY surviving copy of 'only-copy-unrelated' was "
            "deleted even though the published sister version does NOT "
            "actually contain that record -- content-validity and "
            "confinement checks alone are not proof the published "
            "version covers THIS legacy data."
        )
        with ChunkStore(version_dir / "chunks.db", immutable=True) as store:
            assert "only-copy-unrelated" not in store.all_point_ids()


class TestBootstrapAlreadyPublishedValidatesContentMatch:
    """Round-8 CRITICAL finding (Codex empirical reproduction): the
    pointer validator's content-coverage check (round-6 CRITICAL #2)
    only verified that the legacy row's ID is PRESENT in the published
    store -- it never compared the actual field content between the
    legacy row and the published row sharing that ID. Real repro proved:
    a legacy row id='row-1' with chunk_text='original text' and vector
    [0.1,0.2,0.3,0.4], and a published row with the SAME id but
    chunk_text='different text' and vector [0.9,0.8,0.7,0.6] --
    completely different content -- passed validation, authorizing
    deletion of the ONLY correct copy of that data. 'ID exists in both
    places' is necessary but not sufficient; content must match too."""

    def test_refuses_reclaim_when_published_row_content_does_not_match_legacy_row(
        self, tmp_path: Path
    ) -> None:
        aliases_dir = tmp_path / "aliases"
        alias_manager = AliasManager(str(aliases_dir))
        sister_root = tmp_path / "sister"
        namespace = "evolution-temporal-voyage_code_3-2024Q1"

        # A published version whose "row-1" record has the SAME id as the
        # legacy row below, but COMPLETELY DIFFERENT content (vector and
        # chunk_text) -- simulating a corrupted/mismatched published row.
        version_dir = sister_root / ".versioned" / namespace / "v_1700000000"
        version_dir.mkdir(parents=True, exist_ok=True)
        (version_dir / "collection_meta.json").write_text(
            json.dumps({"vector_dim": 4, "vector_size": 4})
        )
        with ChunkStore(version_dir / "chunks.db") as store:
            store.write_batch(
                [
                    {
                        "id": "row-1",
                        "vector": [0.9, 0.8, 0.7, 0.6],
                        "metadata": {"language": "python"},
                        "payload": {"path": "src/pub.py", "language": "python"},
                        "chunk_text": "different text",
                    }
                ]
            )
        write_chunks_db_discriminator(version_dir)
        alias_manager.create_alias(namespace, str(version_dir))

        legacy_shard_dir = (
            tmp_path / "index" / "code-indexer-temporal-voyage_code_3-2024Q1"
        )
        legacy_shard_dir.mkdir(parents=True, exist_ok=True)
        legacy_record = {
            "id": "row-1",
            "vector": [0.1, 0.2, 0.3, 0.4],
            "metadata": {"language": "python"},
            "payload": {"path": "src/pub.py", "language": "python"},
            "chunk_text": "original text",
        }
        (legacy_shard_dir / "vector_row-1.json").write_text(json.dumps(legacy_record))

        with pytest.raises(RuntimeError):
            bootstrap_temporal_namespace_to_sister(
                alias_manager=alias_manager,
                sister_root=sister_root,
                pointer_namespace=namespace,
                legacy_shard_dir=legacy_shard_dir,
                embedder_slug="voyage_code_3",
            )

        assert legacy_shard_dir.exists(), (
            "Bug: the ONLY correct copy of 'row-1' (chunk_text='original "
            "text') was deleted even though the published copy under the "
            "SAME id has completely different content (chunk_text="
            "'different text') -- ID presence alone was treated as proof "
            "of content equivalence."
        )
        with ChunkStore(version_dir / "chunks.db", immutable=True) as store:
            stored = store.read("row-1")
        assert stored["chunk_text"] == "different text", (
            "The published store's mismatched row must remain untouched "
            "-- this test proves the REFUSAL, not a repair."
        )


class TestBootstrapEmptyArtifact:
    def test_removes_rowless_directory_without_publishing(self, tmp_path: Path) -> None:
        aliases_dir = tmp_path / "aliases"
        alias_manager = AliasManager(str(aliases_dir))
        sister_root = tmp_path / "sister"
        namespace = "evolution-temporal-voyage_code_3-2024Q2"

        legacy_shard_dir = (
            tmp_path / "index" / "code-indexer-temporal-voyage_code_3-2024Q2"
        )
        legacy_shard_dir.mkdir(parents=True)
        # No vector_*.json rows -- a rowless empty artifact (Finding 6 /
        # Story #1458 AC1a).

        outcome = bootstrap_temporal_namespace_to_sister(
            alias_manager=alias_manager,
            sister_root=sister_root,
            pointer_namespace=namespace,
            legacy_shard_dir=legacy_shard_dir,
            embedder_slug="voyage_code_3",
        )

        assert outcome.disposition == BootstrapDisposition.EMPTY_ARTIFACT
        assert outcome.reclaimed is True
        assert not legacy_shard_dir.exists()
        assert not alias_manager.alias_exists(namespace)


class TestBootstrapNeedsBootstrap:
    def test_builds_publishes_and_reclaims_real_rows(self, tmp_path: Path) -> None:
        aliases_dir = tmp_path / "aliases"
        alias_manager = AliasManager(str(aliases_dir))
        sister_root = tmp_path / "sister"
        namespace = "evolution-temporal-voyage_code_3-2024Q3"

        legacy_shard_dir = (
            tmp_path / "index" / "code-indexer-temporal-voyage_code_3-2024Q3"
        )
        _write_legacy_row(legacy_shard_dir, "row0001", [0.1, 0.2, 0.3], "src/a.py")
        _write_legacy_row(legacy_shard_dir, "row0002", [0.4, 0.5, 0.6], "src/b.py")

        outcome = bootstrap_temporal_namespace_to_sister(
            alias_manager=alias_manager,
            sister_root=sister_root,
            pointer_namespace=namespace,
            legacy_shard_dir=legacy_shard_dir,
            embedder_slug="voyage_code_3",
        )

        assert outcome.disposition == BootstrapDisposition.NEEDS_BOOTSTRAP
        assert outcome.reclaimed is True
        assert alias_manager.alias_exists(namespace)
        assert not legacy_shard_dir.exists()

        published_path = Path(alias_manager.read_alias(namespace))
        with ChunkStore(published_path / "chunks.db", immutable=True) as store:
            assert store.count() == 2
            row1 = store.read("row0001")
            row2 = store.read("row0002")
        assert row1["chunk_text"] == "content for src/a.py"
        assert row2["chunk_text"] == "content for src/b.py"

    def test_second_call_after_success_is_already_published_noop(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        aliases_dir = tmp_path / "aliases"
        alias_manager = AliasManager(str(aliases_dir))
        sister_root = tmp_path / "sister"
        namespace = "evolution-temporal-voyage_code_3-2024Q4"

        legacy_shard_dir = (
            tmp_path / "index" / "code-indexer-temporal-voyage_code_3-2024Q4"
        )
        _write_legacy_row(legacy_shard_dir, "row0003", [0.7, 0.8], "src/c.py")

        first = bootstrap_temporal_namespace_to_sister(
            alias_manager=alias_manager,
            sister_root=sister_root,
            pointer_namespace=namespace,
            legacy_shard_dir=legacy_shard_dir,
            embedder_slug="voyage_code_3",
        )
        assert first.disposition == BootstrapDisposition.NEEDS_BOOTSTRAP

        import code_indexer.services.temporal.temporal_bootstrap as mod

        build_calls = {"n": 0}
        original_build = mod.build_fresh_consolidated_temporal_version

        def _counting_build(*args, **kwargs):
            build_calls["n"] += 1
            return original_build(*args, **kwargs)

        monkeypatch.setattr(
            mod, "build_fresh_consolidated_temporal_version", _counting_build
        )

        second = bootstrap_temporal_namespace_to_sister(
            alias_manager=alias_manager,
            sister_root=sister_root,
            pointer_namespace=namespace,
            legacy_shard_dir=legacy_shard_dir,
            embedder_slug="voyage_code_3",
        )

        assert second.disposition == BootstrapDisposition.ALREADY_PUBLISHED
        assert build_calls["n"] == 0


class TestBootstrapAlreadyPublishedPointerTargetShapeAndSymlinkHardening:
    """Codex CRITICAL finding (round 4): confinement-under-namespace alone
    is not sufficient -- the target must ALSO match the canonical
    v_<digits> leaf shape (rejecting e.g. a 'current' pointer target), and
    the namespace directory itself must not be a symlink (which would let
    resolve() transparently follow it to an unrelated, coincidentally-
    valid database)."""

    def test_refuses_reclaim_when_pointer_target_leaf_is_not_a_canonical_v_timestamp(
        self, tmp_path: Path
    ) -> None:
        aliases_dir = tmp_path / "aliases"
        alias_manager = AliasManager(str(aliases_dir))
        sister_root = tmp_path / "sister"
        namespace = "evolution-temporal-voyage_code_3-2024Q1"

        # Content-valid, genuinely confined to the right namespace
        # directory -- but the leaf is "current", not a canonical
        # v_<digits> snapshot name.
        non_canonical_target = sister_root / ".versioned" / namespace / "current"
        _write_valid_sister_version(non_canonical_target)
        alias_manager.create_alias(namespace, str(non_canonical_target))

        legacy_shard_dir = (
            tmp_path / "index" / "code-indexer-temporal-voyage_code_3-2024Q1"
        )
        _write_legacy_row(legacy_shard_dir, "onlycopy4", [0.7, 0.8], "src/d.py")

        with pytest.raises(RuntimeError):
            bootstrap_temporal_namespace_to_sister(
                alias_manager=alias_manager,
                sister_root=sister_root,
                pointer_namespace=namespace,
                legacy_shard_dir=legacy_shard_dir,
                embedder_slug="voyage_code_3",
            )

        assert legacy_shard_dir.exists(), (
            "Bug: the ONLY intact copy of this temporal data was deleted "
            "based on a pointer target that was content-valid and "
            "namespace-confined, but did NOT match the canonical "
            "v_<digits> snapshot leaf shape."
        )

    def test_refuses_reclaim_when_namespace_directory_itself_is_a_symlink(
        self, tmp_path: Path
    ) -> None:
        aliases_dir = tmp_path / "aliases"
        alias_manager = AliasManager(str(aliases_dir))
        sister_root = tmp_path / "sister"
        namespace = "evolution-temporal-voyage_code_3-2024Q1"

        # A genuinely valid, canonical v_<digits> version -- but at an
        # UNRELATED location, reached only because the expected namespace
        # directory ITSELF is a symlink into it.
        unrelated_version = tmp_path / "unrelated-elsewhere" / "v_1700000000"
        _write_valid_sister_version(unrelated_version)

        expected_ns_dir = sister_root / ".versioned" / namespace
        expected_ns_dir.parent.mkdir(parents=True, exist_ok=True)
        expected_ns_dir.symlink_to(unrelated_version.parent, target_is_directory=True)

        target_path = expected_ns_dir / "v_1700000000"
        alias_manager.create_alias(namespace, str(target_path))

        legacy_shard_dir = (
            tmp_path / "index" / "code-indexer-temporal-voyage_code_3-2024Q1"
        )
        _write_legacy_row(legacy_shard_dir, "onlycopy5", [0.9, 1.0], "src/e.py")

        with pytest.raises(RuntimeError):
            bootstrap_temporal_namespace_to_sister(
                alias_manager=alias_manager,
                sister_root=sister_root,
                pointer_namespace=namespace,
                legacy_shard_dir=legacy_shard_dir,
                embedder_slug="voyage_code_3",
            )

        assert legacy_shard_dir.exists(), (
            "Bug: the ONLY intact copy of this temporal data was deleted "
            "based on a pointer target reached only via a SYMLINKED "
            "namespace directory -- resolve() transparently followed it "
            "to an unrelated, coincidentally-valid database."
        )
