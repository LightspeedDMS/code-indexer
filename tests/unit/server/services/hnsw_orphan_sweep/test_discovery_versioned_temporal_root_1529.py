"""Bug #1529 review finding #6: sweep's golden_repos_dir derivation is broken
for versioned-topology golden repos.

``_golden_candidates`` derived the fixed temporal root as
``server_temporal_index_root(repo_root.parent, alias)``. That is only correct
when ``get_actual_repo_path()`` returns the FLAT clone
(``{golden_repos_dir}/{alias}/``). For a repo whose resolver returns the
VERSIONED form (``{golden_repos_dir}/.versioned/{alias}/v_*/`` -- the exact
trap ``feedback_versioned_path_trap`` exists for), ``repo_root.parent`` is
``{golden_repos_dir}/.versioned/{alias}``, so the computed temporal root is
``.../.versioned/{alias}/.temporal/{alias}``: a directory that never exists.

The walk therefore yields NOTHING for those repos, silently. Their temporal
shards are never swept and never repaired, quietly voiding Epic #1333's
zero-tolerance orphan guarantee for exactly the repos on the versioned
topology -- with no error and no log.

``resolve_golden_repo_coordinates`` already handles BOTH layouts and returns
identical coordinates for each; it is the correct primitive here.

Real filesystem layouts -- only the two repo-manager enumeration primitives
are stubbed (they are DB-backed).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

from code_indexer.server.services.hnsw_orphan_sweep.discovery import (
    enumerate_sweep_candidates,
)
from code_indexer.services.temporal.temporal_server_paths import (
    server_temporal_index_root,
)

ALIAS = "myrepo"

#: One concrete version directory. The exact value is irrelevant -- only the
#: `v_` prefix is structural (VERSION_DIR_PREFIX), which is what makes
#: resolve_golden_repo_coordinates recognize the versioned layout.
VERSION_DIR = "v_1720000000"

#: An arbitrary but valid embedder/quarter shard name in the physical form
#: `code-indexer-temporal-{embedder_slug}-{quarter}`.
TEMPORAL_SHARD = "code-indexer-temporal-voyage_code_3-2026Q1"

#: voyage-code-3's real embedding width. Never read by discovery (which keys
#: only on the hnsw_index.bin + collection_meta.json PAIR), but a realistic
#: value keeps the fixture honest.
VOYAGE_CODE_3_VECTOR_DIM = 1024


def _make_collection(base: Path, *segments: str) -> Path:
    """Create a directory that satisfies discovery's definition of a real
    HNSW collection: an hnsw_index.bin with a sibling collection_meta.json."""
    coll = base.joinpath(*segments)
    coll.mkdir(parents=True, exist_ok=True)
    (coll / "hnsw_index.bin").write_bytes(b"fake-index-bytes")
    (coll / "collection_meta.json").write_text(
        json.dumps({"vector_dim": VOYAGE_CODE_3_VECTOR_DIM})
    )
    return coll


def _managers(repo_root: Path):
    golden_mgr = MagicMock()
    golden_mgr.list_golden_repos.return_value = [{"alias": ALIAS}]
    golden_mgr.get_actual_repo_path.return_value = str(repo_root)

    activated_mgr = MagicMock()
    activated_mgr.list_all_activated_repositories.return_value = []
    return golden_mgr, activated_mgr


def _temporal_candidates(golden_mgr, activated_mgr):
    return [
        c
        for c in enumerate_sweep_candidates(golden_mgr, activated_mgr)
        if c.kind == "golden_temporal"
    ]


def test_versioned_topology_repo_temporal_shards_are_swept(tmp_path: Path) -> None:
    """THE bug: a versioned-form repo_root must still find the fixed root."""
    golden_repos_dir = tmp_path / "golden-repos"
    repo_root = golden_repos_dir / ".versioned" / ALIAS / VERSION_DIR
    repo_root.mkdir(parents=True)

    temporal_root = server_temporal_index_root(golden_repos_dir, ALIAS)
    _make_collection(temporal_root, TEMPORAL_SHARD)

    candidates = _temporal_candidates(*_managers(repo_root))

    assert len(candidates) == 1, (
        "a versioned-topology golden repo's temporal shards were not "
        "discovered -- they would never be swept or repaired"
    )
    assert candidates[0].repo_root == temporal_root
    assert candidates[0].alias == ALIAS
    assert (
        candidates[0].sort_key
        == f"golden_temporal:{ALIAS}:{TEMPORAL_SHARD}/hnsw_index.bin"
    )


def test_flat_topology_repo_still_swept_unchanged(tmp_path: Path) -> None:
    """Control: the already-working flat layout must be unaffected."""
    golden_repos_dir = tmp_path / "golden-repos"
    repo_root = golden_repos_dir / ALIAS
    repo_root.mkdir(parents=True)

    temporal_root = server_temporal_index_root(golden_repos_dir, ALIAS)
    _make_collection(temporal_root, TEMPORAL_SHARD)

    candidates = _temporal_candidates(*_managers(repo_root))

    assert len(candidates) == 1
    assert candidates[0].repo_root == temporal_root


def test_unrecognized_topology_warns_rather_than_skipping_silently(
    tmp_path: Path, caplog
) -> None:
    """An underivable layout must be VISIBLE, not a silent coverage hole.

    A silent skip is what made this finding invisible in the first place, so
    the degraded case has to say so.
    """
    repo_root = tmp_path / "somewhere-else" / ALIAS
    repo_root.mkdir(parents=True)

    golden_mgr, activated_mgr = _managers(repo_root)

    with caplog.at_level(logging.WARNING):
        candidates = _temporal_candidates(golden_mgr, activated_mgr)

    assert candidates == []
    assert any(
        record.levelno >= logging.WARNING and ALIAS in record.getMessage()
        for record in caplog.records
    ), "an underivable temporal root must be logged at WARNING, never skipped silently"
