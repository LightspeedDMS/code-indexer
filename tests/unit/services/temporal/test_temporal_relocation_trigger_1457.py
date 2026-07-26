"""maybe_relocate_shard_to_sister_location() -- Story #1457 AC1's actual
relocation trigger.

Wires AC6's already-built build+publish machinery
(temporal_refresh_dispatch.execute_temporal_refresh_branch) into the real
temporal indexing pipeline, gated on the CIDX_SERVER_REFRESH_CONTEXT env
var (set unconditionally by build_temporal_child_env for every
server-spawned temporal child, all storage modes -- Story #1457 round 1):

- Env var ABSENT (standalone CLI, no server process): true no-op --
  Finding 1's resolution, temporal data stays entirely in-repo.
- Env var PRESENT (server-spawned refresh child, any storage mode): after
  a quarter shard's normal in-repo write+finalize (unchanged, happens
  BEFORE this function is called), ALSO builds+publishes the SAME data to
  the sister location via AC6's three-branch dispatch.

golden_repos_dir/repo_alias derivation: temporal indexing ONLY ever runs
against a golden repo's own clone directly (AC12 explicitly rejects
"temporal" in activated-repo reindex requests), so codebase_dir.name IS
the golden repo's own alias and codebase_dir.parent IS golden_repos_dir --
the SAME derivation already confirmed for the server QUERY side
(SemanticQueryManager's is_global branch / AC1-AC2 live wiring).

Real AliasManager, real ChunkStore/HNSW build, real filesystem -- no
mocking of the code under test.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.storage.postgres.temporal_child_wiring import (
    CIDX_SERVER_REFRESH_CONTEXT_ENV,
)
from code_indexer.services.temporal.temporal_relocation_trigger import (
    maybe_relocate_shard_to_sister_location,
)
from code_indexer.storage.sqlite_chunk_store import ChunkStore


def _write_local_shard_row(shard_dir, hash_prefix, point_id, commit_hash):
    shard_dir.mkdir(parents=True, exist_ok=True)
    row = {
        "id": point_id,
        "vector": [0.1, 0.2, 0.3, 0.4],
        "payload": {"commit_hash": commit_hash, "path": "src/foo.py"},
    }
    (shard_dir / f"vector_{hash_prefix}.json").write_text(json.dumps(row))


def test_no_op_by_default_even_when_server_context_present(tmp_path, monkeypatch):
    """Safety gate (code review finding, both Claude and Codex): AC1's
    relocation trigger must be explicitly opt-in, disabled by default --
    mirroring Story #1456's CIDX_CHUNKS_DB_NEW_COLLECTIONS pattern. Even
    with the server-context env var present, publishing to the sister
    location must NOT happen unless the new gate env var is explicitly
    enabled."""
    monkeypatch.setenv(CIDX_SERVER_REFRESH_CONTEXT_ENV, "1")
    monkeypatch.delenv("CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED", raising=False)

    codebase_dir = tmp_path / "golden-repos" / "evolution"
    shard_name = "code-indexer-temporal-voyage_code_3-2024Q1"
    local_shard_dir = codebase_dir / ".code-indexer" / "index" / shard_name
    _write_local_shard_row(local_shard_dir, "aaaa1111", "proj:commit:c0:0", "c0")

    maybe_relocate_shard_to_sister_location(
        codebase_dir=codebase_dir,
        shard_name=shard_name,
        local_shard_dir=local_shard_dir,
        new_commit_hashes=["c0"],
        vector_dim=4,
    )

    # No-op: no aliases directory (and therefore no pointer) was created,
    # even though server-context is present -- the new gate is OFF by
    # default and must block publication.
    assert not (tmp_path / "golden-repos" / "aliases").exists()


def test_no_op_when_server_context_env_var_absent(tmp_path, monkeypatch):
    monkeypatch.delenv(CIDX_SERVER_REFRESH_CONTEXT_ENV, raising=False)

    codebase_dir = tmp_path / "golden-repos" / "evolution"
    shard_name = "code-indexer-temporal-voyage_code_3-2024Q1"
    local_shard_dir = codebase_dir / ".code-indexer" / "index" / shard_name
    _write_local_shard_row(local_shard_dir, "aaaa1111", "proj:commit:c0:0", "c0")

    maybe_relocate_shard_to_sister_location(
        codebase_dir=codebase_dir,
        shard_name=shard_name,
        local_shard_dir=local_shard_dir,
        new_commit_hashes=["c0"],
        vector_dim=4,
    )

    # No-op: no aliases directory (and therefore no pointer) was created.
    assert not (tmp_path / "golden-repos" / "aliases").exists()


def test_publishes_new_quarter_via_create_alias_when_server_context_present(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(CIDX_SERVER_REFRESH_CONTEXT_ENV, "1")
    monkeypatch.setenv("CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED", "1")

    codebase_dir = tmp_path / "golden-repos" / "evolution"
    shard_name = "code-indexer-temporal-voyage_code_3-2024Q1"
    local_shard_dir = codebase_dir / ".code-indexer" / "index" / shard_name
    _write_local_shard_row(local_shard_dir, "aaaa1111", "proj:commit:c0:0", "c0")
    _write_local_shard_row(local_shard_dir, "bbbb2222", "proj:commit:c1:0", "c1")

    maybe_relocate_shard_to_sister_location(
        codebase_dir=codebase_dir,
        shard_name=shard_name,
        local_shard_dir=local_shard_dir,
        new_commit_hashes=["c0", "c1"],
        vector_dim=4,
    )

    aliases_dir = tmp_path / "golden-repos" / "aliases"
    alias_manager = AliasManager(str(aliases_dir))
    target_path = alias_manager.read_alias("evolution-temporal-voyage_code_3-2024Q1")
    assert target_path is not None, "expected a published sister pointer"

    store = ChunkStore(target_path + "/chunks.db", immutable=True)
    try:
        assert store.count() == 2
        assert store.read("proj:commit:c0:0") is not None
        assert store.read("proj:commit:c1:0") is not None
    finally:
        store.close()


def test_second_run_swaps_alias_and_preserves_historical_rows(tmp_path, monkeypatch):
    """A second refresh run for the SAME quarter (pointer already exists,
    Branch A) reflink-copies the current version and applies ONLY the new
    commit as delta -- the FIRST run's row must survive."""
    monkeypatch.setenv(CIDX_SERVER_REFRESH_CONTEXT_ENV, "1")
    monkeypatch.setenv("CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED", "1")

    codebase_dir = tmp_path / "golden-repos" / "evolution"
    shard_name = "code-indexer-temporal-voyage_code_3-2024Q1"
    local_shard_dir = codebase_dir / ".code-indexer" / "index" / shard_name
    _write_local_shard_row(local_shard_dir, "aaaa1111", "proj:commit:c0:0", "c0")

    maybe_relocate_shard_to_sister_location(
        codebase_dir=codebase_dir,
        shard_name=shard_name,
        local_shard_dir=local_shard_dir,
        new_commit_hashes=["c0"],
        vector_dim=4,
    )

    # Second refresh: local shard now ALSO has a new commit's row.
    _write_local_shard_row(local_shard_dir, "bbbb2222", "proj:commit:c1:0", "c1")

    maybe_relocate_shard_to_sister_location(
        codebase_dir=codebase_dir,
        shard_name=shard_name,
        local_shard_dir=local_shard_dir,
        new_commit_hashes=["c1"],
        vector_dim=4,
    )

    aliases_dir = tmp_path / "golden-repos" / "aliases"
    alias_manager = AliasManager(str(aliases_dir))
    target_path = alias_manager.read_alias("evolution-temporal-voyage_code_3-2024Q1")
    store = ChunkStore(target_path + "/chunks.db", immutable=True)
    try:
        assert store.count() == 2
        assert store.read("proj:commit:c0:0") is not None, (
            "historical row from the FIRST run must survive the SECOND run's publish"
        )
        assert store.read("proj:commit:c1:0") is not None
    finally:
        store.close()


def test_forwards_force_rebuild_to_dispatch(tmp_path, monkeypatch):
    """Story #1457 HIGH #11 (2026-07-23 code review): the caller's local-
    repair signal (was_stale in temporal_indexer.py) must reach the
    dispatch layer as force_rebuild, so a locally-repaired shard with an
    empty commit delta still gets its sister HNSW index rebuilt on
    republish. execute_temporal_refresh_branch has its own dedicated
    forwarding coverage (Branch A -> copy_and_extend) in
    test_temporal_refresh_dispatch_1457.py; this test proves only that
    THIS trigger function forwards the argument through to it."""
    monkeypatch.setenv(CIDX_SERVER_REFRESH_CONTEXT_ENV, "1")
    monkeypatch.setenv("CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED", "1")

    codebase_dir = tmp_path / "golden-repos" / "evolution"
    shard_name = "code-indexer-temporal-voyage_code_3-2024Q1"
    local_shard_dir = codebase_dir / ".code-indexer" / "index" / shard_name
    _write_local_shard_row(local_shard_dir, "aaaa1111", "proj:commit:c0:0", "c0")

    with patch(
        "code_indexer.services.temporal.temporal_relocation_trigger"
        ".execute_temporal_refresh_branch"
    ) as mock_execute:
        maybe_relocate_shard_to_sister_location(
            codebase_dir=codebase_dir,
            shard_name=shard_name,
            local_shard_dir=local_shard_dir,
            new_commit_hashes=[],
            vector_dim=4,
            force_rebuild=True,
        )

    assert mock_execute.call_count == 1
    _, call_kwargs = mock_execute.call_args
    assert call_kwargs.get("force_rebuild") is True, (
        "maybe_relocate_shard_to_sister_location must forward "
        "force_rebuild=True to execute_temporal_refresh_branch -- got "
        f"kwargs: {call_kwargs}"
    )


def test_corrupt_row_in_local_shard_fails_loud_instead_of_silently_publishing(
    tmp_path, monkeypatch
):
    """Story #1457 CRITICAL #4 (2026-07-23 code review): a corrupt row
    file must fail the relocation trigger loudly -- never silently drop
    the row and publish an incomplete result."""
    monkeypatch.setenv(CIDX_SERVER_REFRESH_CONTEXT_ENV, "1")
    monkeypatch.setenv("CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED", "1")

    codebase_dir = tmp_path / "golden-repos" / "evolution"
    shard_name = "code-indexer-temporal-voyage_code_3-2024Q1"
    local_shard_dir = codebase_dir / ".code-indexer" / "index" / shard_name
    _write_local_shard_row(local_shard_dir, "aaaa1111", "proj:commit:c0:0", "c0")
    (local_shard_dir / "vector_corrupt.json").write_text("{not valid json")

    import pytest

    with pytest.raises(RuntimeError, match="vector_corrupt.json"):
        maybe_relocate_shard_to_sister_location(
            codebase_dir=codebase_dir,
            shard_name=shard_name,
            local_shard_dir=local_shard_dir,
            new_commit_hashes=["c0"],
            vector_dim=4,
        )

    # No pointer was published -- the incomplete result must never reach
    # publication (AliasManager's constructor creates its directory as a
    # side effect regardless, so check for the absent POINTER, not the
    # directory).
    aliases_dir = tmp_path / "golden-repos" / "aliases"
    alias_manager = AliasManager(str(aliases_dir))
    assert alias_manager.read_alias("evolution-temporal-voyage_code_3-2024Q1") is None


def test_codebase_dir_with_global_suffix_publishes_under_normalized_alias(
    tmp_path, monkeypatch
):
    """Story #1457 HIGH #7 (2026-07-23 code review), defense-in-depth
    symmetry: even if codebase_dir's own directory name ends with
    '-global' (never realistic in production -- golden repo directories
    are always bare names, but the coordinator's fix explicitly requires
    normalizing on BOTH the publish and query sides), the publish
    namespace must still use the normalized bare alias, so it can never
    drift from the query side's own normalization."""
    monkeypatch.setenv(CIDX_SERVER_REFRESH_CONTEXT_ENV, "1")
    monkeypatch.setenv("CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED", "1")

    codebase_dir = tmp_path / "golden-repos" / "evolution-global"
    shard_name = "code-indexer-temporal-voyage_code_3-2024Q1"
    local_shard_dir = codebase_dir / ".code-indexer" / "index" / shard_name
    _write_local_shard_row(local_shard_dir, "aaaa1111", "proj:commit:c0:0", "c0")

    maybe_relocate_shard_to_sister_location(
        codebase_dir=codebase_dir,
        shard_name=shard_name,
        local_shard_dir=local_shard_dir,
        new_commit_hashes=["c0"],
        vector_dim=4,
    )

    aliases_dir = tmp_path / "golden-repos" / "aliases"
    alias_manager = AliasManager(str(aliases_dir))
    assert (
        alias_manager.read_alias("evolution-temporal-voyage_code_3-2024Q1") is not None
    ), "publish must use the NORMALIZED (bare) alias, matching the query side"
