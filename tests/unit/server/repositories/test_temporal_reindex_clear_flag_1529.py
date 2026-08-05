"""Bug #1529 review findings #1 and #5: the temporal reindex --clear decision.

Finding #1 (CRITICAL, data-destructive): the admin temporal add-index/reindex
path decided whether to append ``--clear`` by checking the OLD in-repo index
directory only. After #1529 relocated server-context temporal data to the
fixed ``{golden_repos_dir}/.temporal/{alias}/`` root, that check is
permanently False -- so ``--clear`` was ALWAYS appended, deleting the real
shards and forcing a full re-embed of the entire git history (real embedding
spend) on the first admin reindex of any relocated repo. This is the same
false-negative class already fixed once in
``temporal_shard_has_committed_rows``, simply never applied to this caller.

Finding #5: because the predicate defaulted to ``on_error="treat_absent"``, a
CORRUPT or LOCKED ``chunks.db`` was silently reported as "no data" -- which
for THIS decision means a destructive full reindex. A destructive decision
must fail loud instead.

Real filesystem, real SQLite chunk stores -- nothing mocked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pytest

from code_indexer.server.repositories.golden_repo_manager import (
    temporal_reindex_needs_clear,
)
from code_indexer.services.temporal.temporal_server_paths import (
    server_temporal_index_root,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

ALIAS = "evolution"
SHARD = "code-indexer-temporal-voyage_code_3-2024Q1"
VECTOR_SIZE = 8


def _rows() -> List[Dict[str, Any]]:
    rng = np.random.default_rng(1529)
    return [
        {
            "id": "proj:commit:aaaaaaaa:0",
            "vector": rng.standard_normal(VECTOR_SIZE).astype(np.float64).tolist(),
            "payload": {"path": "src/a.py", "commit_hash": "aaaaaaaa"},
            "chunk_text": "x",
        }
    ]


def _build_shard(index_root: Path, collection: str = SHARD) -> Path:
    store = FilesystemVectorStore(
        base_path=index_root, use_chunks_db_for_new_collections=True
    )
    store.create_collection(collection, vector_size=VECTOR_SIZE)
    store.begin_indexing(collection)
    store.upsert_points(collection, _rows())
    store.end_indexing(collection)
    return index_root / collection


def _layout(tmp_path: Path):
    golden_repos_dir = tmp_path / "golden-repos"
    in_repo_index = golden_repos_dir / ALIAS / ".code-indexer" / "index"
    in_repo_index.mkdir(parents=True)
    return golden_repos_dir, in_repo_index


def test_no_clear_when_data_lives_only_at_the_fixed_root(tmp_path: Path) -> None:
    """THE destructive bug: real data at the fixed root must NOT be wiped."""
    golden_repos_dir, in_repo_index = _layout(tmp_path)
    _build_shard(server_temporal_index_root(golden_repos_dir, ALIAS))

    assert (
        temporal_reindex_needs_clear(golden_repos_dir, ALIAS, in_repo_index) is False
    ), (
        "--clear would be appended despite real temporal data existing at the "
        "fixed root -- this deletes it and forces a full re-embed"
    )


def test_no_clear_when_data_lives_only_in_repo(tmp_path: Path) -> None:
    """Pre-relocation / standalone-shaped data must also be preserved."""
    golden_repos_dir, in_repo_index = _layout(tmp_path)
    _build_shard(in_repo_index)

    assert temporal_reindex_needs_clear(golden_repos_dir, ALIAS, in_repo_index) is False


def test_no_clear_for_bare_legacy_monolith_name(tmp_path: Path) -> None:
    """The BARE pre-#1457 monolith name must also suppress ``--clear``.

    ``TEMPORAL_COLLECTION_PREFIX`` carries a trailing hyphen, so
    ``parse_physical_temporal_name("code-indexer-temporal")`` returns None and
    the namespace is INVISIBLE to ``get_temporal_repo_status``. An
    implementation built on that helper alone therefore reports "no data" for a
    fully-populated legacy monolith and wipes it -- the exact destructive
    outcome this whole finding is about, in the one shape the status helper
    cannot see.
    """
    golden_repos_dir, in_repo_index = _layout(tmp_path)
    _build_shard(in_repo_index, collection="code-indexer-temporal")

    assert temporal_reindex_needs_clear(golden_repos_dir, ALIAS, in_repo_index) is False


def test_clear_when_there_is_genuinely_no_temporal_data(tmp_path: Path) -> None:
    """The legitimate --clear case must still fire (Bug #945's intent)."""
    golden_repos_dir, in_repo_index = _layout(tmp_path)

    assert temporal_reindex_needs_clear(golden_repos_dir, ALIAS, in_repo_index) is True


def test_global_suffixed_alias_resolves_the_same_fixed_root(tmp_path: Path) -> None:
    golden_repos_dir, in_repo_index = _layout(tmp_path)
    _build_shard(server_temporal_index_root(golden_repos_dir, ALIAS))

    assert (
        temporal_reindex_needs_clear(golden_repos_dir, f"{ALIAS}-global", in_repo_index)
        is False
    )


def test_corrupt_chunks_db_raises_instead_of_silently_clearing(
    tmp_path: Path,
) -> None:
    """Finding #5: a corrupt store must NOT be read as 'no data', because for
    this decision that means destroying whatever is really there."""
    golden_repos_dir, in_repo_index = _layout(tmp_path)
    shard = _build_shard(server_temporal_index_root(golden_repos_dir, ALIAS))
    (shard / "chunks.db").write_bytes(b"this is not a sqlite database at all")

    with pytest.raises(Exception) as exc_info:
        temporal_reindex_needs_clear(golden_repos_dir, ALIAS, in_repo_index)

    # Must not be an assertion-free silent False; the error must name the store.
    assert (
        "chunks.db" in str(exc_info.value) or "database" in str(exc_info.value).lower()
    )
