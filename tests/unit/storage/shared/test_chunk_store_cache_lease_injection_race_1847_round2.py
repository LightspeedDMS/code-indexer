"""Bug #1847 Round 2, Defect 1 -- get_global_chunk_store_cache()'s lease
configuration is silently ignorable.

Round 1 leased all three cross-node readers (FTS, IdIndex, ChunkStore).
For ChunkStore, the singleton getter accepts ``lease_root``/``is_versioned_
snapshot`` kwargs but only APPLIES them the very first time the singleton
is constructed:

    def get_global_chunk_store_cache(*, lease_root=None, is_versioned_snapshot=None):
        if _global_chunk_store_cache_instance is None:
            with _global_chunk_store_cache_lock:
                if _global_chunk_store_cache_instance is None:
                    _global_chunk_store_cache_instance = ChunkStoreThreadCache(
                        lease_root=lease_root, is_versioned_snapshot=is_versioned_snapshot)
        return _global_chunk_store_cache_instance

Two real production callers pass NO lease kwargs at all --
``FilesystemBackend.get_vector_store_client()`` (backends/filesystem_
backend.py:215) and ``snapshot_cache_invalidation.py``'s stale-prefix
invalidation path (line 188). If either of those runs before ``server/
startup/lifespan.py``'s real, leased construction call (line ~1527) in a
server process, the singleton is permanently constructed UNLEASED and
lifespan's real ``lease_root`` is discarded with NO exception and NO
warning. chunks.db reader leases are then never published in that
process, and ``CleanupManager`` proceeds as if no reader existed --
exactly the silent-corruption-risk class Bug #1845/#1847 exist to
eliminate, reintroduced one layer down.

Fix (option 2, agreed in this round's pair-negotiation): make an
already-unleased singleton a LOUD error the moment a caller tries to
wire real lease configuration onto it, rather than silently discarding
that configuration. A caller supplying no lease kwargs (the CLI/solo
path, or any server caller that only wants the existing instance) never
trips this -- CLI/solo behavior is completely unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.storage.shared import chunk_store_cache


@pytest.fixture(autouse=True)
def _reset_singleton():
    chunk_store_cache.reset_global_chunk_store_cache()
    yield
    chunk_store_cache.reset_global_chunk_store_cache()


class TestServerWiringRejectsAlreadyUnleasedSingleton:
    def test_server_wiring_raises_when_lease_less_caller_already_won_construction(
        self, tmp_path: Path
    ) -> None:
        """RED on round-1 (unmodified) code: ``ChunkStoreLeaseConfiguration
        Error`` does not exist at all -- the loud-failure capability this
        fix adds is simply absent, so this raises ``AttributeError``
        before the real assertion is even reached. That absence IS the
        defect: round-1 code has no mechanism to prevent the silent
        discard this test's setup reproduces.

        Setup reproduces the real race directly: a lease-less caller
        (standing in for ``FilesystemBackend``/``snapshot_cache_
        invalidation.py``) constructs the singleton first, exactly as
        production ordering can allow. ``lifespan.py``'s real, later,
        leased call must then be rejected loudly rather than silently
        returning the stale unleased instance with the supplied
        ``lease_root`` thrown away.
        """
        unleased = chunk_store_cache.get_global_chunk_store_cache()
        assert unleased._lease_root is None, (
            "precondition broken: the lease-less caller must win "
            "construction first, or this test proves nothing about the "
            "race"
        )

        lease_root = tmp_path / "cidx-meta"
        with pytest.raises(chunk_store_cache.ChunkStoreLeaseConfigurationError):
            chunk_store_cache.get_global_chunk_store_cache(
                lease_root=lease_root,
                is_versioned_snapshot=lambda p: True,
            )


class TestLeaseLessCallsAndCliSoloPathUnaffected:
    def test_lease_less_call_against_existing_leased_singleton_is_a_no_op(
        self, tmp_path: Path
    ) -> None:
        """A caller supplying no lease kwargs against an ALREADY-leased
        singleton (e.g. a second, later server-side touch) must never
        trip the guard and must keep receiving the same leased instance
        unchanged.
        """
        lease_root = tmp_path / "cidx-meta"
        leased = chunk_store_cache.get_global_chunk_store_cache(
            lease_root=lease_root, is_versioned_snapshot=lambda p: True
        )

        same = chunk_store_cache.get_global_chunk_store_cache()

        assert same is leased
        assert same._lease_root == lease_root

    def test_leased_call_against_already_leased_singleton_is_a_no_op(
        self, tmp_path: Path
    ) -> None:
        """Two server-side callers both supplying the SAME real lease
        configuration (e.g. concurrent startup on a multi-worker process)
        must not raise -- only a MISMATCH between "already unleased" and
        "now being asked to lease" is an error.
        """
        lease_root = tmp_path / "cidx-meta"
        predicate = lambda p: True  # noqa: E731

        first = chunk_store_cache.get_global_chunk_store_cache(
            lease_root=lease_root, is_versioned_snapshot=predicate
        )
        second = chunk_store_cache.get_global_chunk_store_cache(
            lease_root=lease_root, is_versioned_snapshot=predicate
        )

        assert first is second
        assert second._lease_root == lease_root

    def test_cli_solo_path_construction_remains_unleased_and_unchanged(
        self,
    ) -> None:
        """No kwargs supplied anywhere -- the CLI/solo path -- must
        continue to construct (and keep) an unleased singleton with no
        error, preserving pre-existing behavior exactly (Bug #1467/#1468:
        the CLI/solo path legitimately has no lease root).
        """
        cache = chunk_store_cache.get_global_chunk_store_cache()
        assert cache._lease_root is None

        same = chunk_store_cache.get_global_chunk_store_cache()
        assert same is cache
        assert same._lease_root is None
