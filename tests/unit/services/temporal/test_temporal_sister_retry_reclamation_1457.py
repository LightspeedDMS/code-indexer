"""Sister-retry-on-reclamation nested-pin mechanics (Story #1457 AC8 Step 6,
round-27/29/33/35/41 corrections).

_execute_pinned_shard_read() closes the AC14 accepted residual: an
IN_REPO_LEGACY read that fails mid-flight because a DIFFERENT worker
process reclaimed the in-repo tree (a real, non-atomic shutil.rmtree race
-- the root directory can still "exist" while the SPECIFIC file the read
just tried to open has already been deleted, per round-29's precision
fix). Detection relies SOLELY on re-resolving via the resolver (never a
racy in-repo path-existence check): on a low-level filesystem/SQLite
error for an originally-IN_REPO_LEGACY read, a NESTED resolver.pin() call
(round-33: reusing the SAME pin mechanism, on top of the still-active
outer pin) both re-resolves (detection) and retries (recovery) in one
call -- EXACTLY one sister-read attempt, never a bare re-call to search().

Three real, distinct outcomes, each proven with real infrastructure (real
TemporalShardResolver, QueryTracker, AliasManager -- no mocking of the
code under test; only the query_fn read callback itself is a
test-controlled closure, since simulating a REAL concurrent worker
process reclaiming a directory mid-read is impractical in a unit test --
this closure performs REAL side effects, e.g. a REAL AliasManager.create_alias
call, to simulate the race deterministically):
  1. Benign reclamation, retry succeeds -- the common case.
  2. NOT the benign case (no sister pointer appears) -- original exception
     re-raised unchanged, query_fn never called a second time.
  3. Benign reclamation, but the retry itself ALSO fails -- wrapped in
     _TemporalFailureAlreadyRecorded, record_temporal_failure() called
     EXACTLY once.
"""

from __future__ import annotations

from unittest.mock import patch

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.services.temporal.temporal_fusion_dispatch import (
    _TemporalFailureAlreadyRecorded,
    _execute_pinned_shard_read,
)
from code_indexer.services.temporal.temporal_shard_resolver import (
    TemporalShardResolver,
)


def _make_resolver_with_legacy_shard(tmp_path, repo_alias="evolution"):
    aliases_dir = tmp_path / "aliases"
    sister_root = tmp_path / "sister"
    legacy_index_path = tmp_path / "index"
    alias_manager = AliasManager(str(aliases_dir))

    legacy_shard = legacy_index_path / "code-indexer-temporal-voyage_code_3-2024Q1"
    legacy_shard.mkdir(parents=True)
    (legacy_shard / "vector_abc.json").write_text('{"id": "p1"}')

    query_tracker = QueryTracker()
    resolver = TemporalShardResolver(
        alias_manager=alias_manager,
        repo_alias=repo_alias,
        sister_root=sister_root,
        legacy_index_path=legacy_index_path,
        query_tracker=query_tracker,
    )
    return resolver, alias_manager, sister_root


def test_benign_reclamation_retry_succeeds(tmp_path):
    """First read fails (simulating reclamation-during-read); the sister
    pointer now exists (published concurrently by another worker); the
    nested-pin retry succeeds -- returns the retried result, no exception."""
    resolver, alias_manager, sister_root = _make_resolver_with_legacy_shard(tmp_path)
    version_dir = sister_root / ".versioned" / "ns" / "v_1700000000"
    version_dir.mkdir(parents=True)

    calls = []

    def query_fn(physical_name):
        calls.append(physical_name)
        if len(calls) == 1:
            # Simulate: a different worker reclaimed the in-repo tree
            # AND published the sister version, both mid-flight.
            alias_manager.create_alias(
                "evolution-temporal-voyage_code_3-2024Q1", str(version_dir)
            )
            raise FileNotFoundError("collection_meta.json")
        return f"result-from-{physical_name}"

    with patch(
        "code_indexer.services.temporal.temporal_fusion_dispatch.record_temporal_failure"
    ) as mock_failure:
        result, eviction_path, coll_name = _execute_pinned_shard_read(
            resolver, "voyage_code_3", "2024Q1", query_fn
        )

    assert len(calls) == 2, "query_fn must be retried exactly once"
    assert result == f"result-from-{coll_name}"
    assert eviction_path == version_dir
    mock_failure.assert_not_called()


def test_not_benign_case_reraises_original_exception(tmp_path):
    """First read fails, but NO sister pointer ever appears -- the nested
    pin resolves back to IN_REPO_LEGACY, proving this was NOT reclamation.
    The ORIGINAL exception propagates unchanged; query_fn is never
    called a second time."""
    resolver, _alias_manager, _sister_root = _make_resolver_with_legacy_shard(tmp_path)

    calls = []
    original_exc = FileNotFoundError("chunks.db")

    def query_fn(physical_name):
        calls.append(physical_name)
        raise original_exc

    with patch(
        "code_indexer.services.temporal.temporal_fusion_dispatch.record_temporal_failure"
    ) as mock_failure:
        try:
            _execute_pinned_shard_read(resolver, "voyage_code_3", "2024Q1", query_fn)
            assert False, "expected FileNotFoundError to propagate"
        except FileNotFoundError as caught:
            assert caught is original_exc

    assert len(calls) == 1, "query_fn must NOT be retried when not the benign case"
    mock_failure.assert_not_called()


def test_sister_retry_also_fails_wraps_and_records_once(tmp_path):
    """Benign reclamation IS detected (sister pointer exists), but the
    retried read ALSO fails -- wrapped in _TemporalFailureAlreadyRecorded,
    record_temporal_failure() called EXACTLY once."""
    resolver, alias_manager, sister_root = _make_resolver_with_legacy_shard(tmp_path)
    version_dir = sister_root / ".versioned" / "ns" / "v_1700000000"
    version_dir.mkdir(parents=True)

    calls = []

    def query_fn(physical_name):
        calls.append(physical_name)
        if len(calls) == 1:
            alias_manager.create_alias(
                "evolution-temporal-voyage_code_3-2024Q1", str(version_dir)
            )
        raise FileNotFoundError(f"failure on call {len(calls)}")

    with patch(
        "code_indexer.services.temporal.temporal_fusion_dispatch.record_temporal_failure"
    ) as mock_failure:
        try:
            _execute_pinned_shard_read(resolver, "voyage_code_3", "2024Q1", query_fn)
            assert False, "expected _TemporalFailureAlreadyRecorded"
        except _TemporalFailureAlreadyRecorded:
            pass

    assert len(calls) == 2, "the sister-path retry must have been attempted"
    mock_failure.assert_called_once()
