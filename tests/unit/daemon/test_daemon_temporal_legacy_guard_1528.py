"""Bug #1528: daemon-mode temporal indexing must refuse to extend a legacy
sharded temporal shard.

The daemon has its OWN temporal indexing path (``daemon/service.py``, the
Bug #473 "check temporal FIRST" branch) that never goes through `cidx
index`'s own branch, so it does not get that branch's pre-index in-place
consolidation. Without a guard, a daemon-mode incremental run against a repo
indexed before this fix would keep adding ``vector_*.json`` files, exactly
what this bug forbids.

Migrating inside a daemon RPC is deliberately NOT the answer: the migration
must hold the repo's exclusive index-mutation lock and can legitimately run
for a long time on a large repo. So the daemon FAILS LOUDLY and points at
the existing explicit command instead.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from code_indexer.daemon import service as daemon_service_module
from code_indexer.services.chunk_migration_cli import (
    find_pending_legacy_temporal_shards,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

SHARD_NAME = "code-indexer-temporal-voyage_code_3-2024Q1"
VECTOR_SIZE = 8
RNG_SEED = 1528
GUARD_CALL_NAME = "find_pending_legacy_temporal_shards"
#: The daemon's temporal branch is keyed on this kwargs flag.
TEMPORAL_FLAG_NAME = "index_commits"


def _build_collection(index_dir: Path, *, chunks_db: bool) -> Path:
    store = FilesystemVectorStore(
        base_path=index_dir, use_chunks_db_for_new_collections=chunks_db
    )
    store.create_collection(SHARD_NAME, vector_size=VECTOR_SIZE)
    store.begin_indexing(SHARD_NAME)
    rng = np.random.default_rng(RNG_SEED)
    points: List[Dict[str, Any]] = [
        {
            "id": "proj:commit:aaaaaaaa:0",
            "vector": rng.standard_normal(VECTOR_SIZE).astype(np.float64).tolist(),
            "payload": {"path": "src/a.py"},
            "chunk_text": "x",
        }
    ]
    store.upsert_points(SHARD_NAME, points)
    store.end_indexing(SHARD_NAME)
    return index_dir / SHARD_NAME


class TestFindPendingLegacyTemporalShards:
    def test_reports_a_legacy_shard(self, tmp_path: Path) -> None:
        index_dir = tmp_path / ".code-indexer" / "index"
        shard_dir = _build_collection(index_dir, chunks_db=False)

        assert find_pending_legacy_temporal_shards(index_dir) == [shard_dir]

    def test_empty_for_consolidated_or_missing_index(self, tmp_path: Path) -> None:
        index_dir = tmp_path / ".code-indexer" / "index"
        # Missing directory: nothing pending, never raises.
        assert find_pending_legacy_temporal_shards(index_dir) == []

        _build_collection(index_dir, chunks_db=True)
        assert find_pending_legacy_temporal_shards(index_dir) == []


class TestLegacyTemporalRefusalResponse:
    """The refusal must reach the operator. Real daemon-mode E2E showed a
    ``{"status": "failed", "error": ...}`` dict printing as "Unexpected
    status: failed / Message:" with the reason DROPPED -- the client contract
    (``cli_daemon_delegation``) is ``status == "error"`` plus ``message``."""

    def test_uses_the_client_error_contract(self, tmp_path: Path) -> None:
        response = daemon_service_module.legacy_temporal_refusal_response(
            [tmp_path / SHARD_NAME]
        )

        assert response["status"] == "error"
        assert "message" in response, (
            "the client reads `message`; an `error` key is silently dropped"
        )

    def test_message_names_the_shards_and_the_remedy(self, tmp_path: Path) -> None:
        response = daemon_service_module.legacy_temporal_refusal_response(
            [tmp_path / SHARD_NAME]
        )

        message = response["message"]
        assert SHARD_NAME in message
        assert "--migrate-chunks-to-sqlite" in message


def _is_temporal_branch_test(node: ast.expr) -> bool:
    """True ONLY for a test of the exact shape
    ``kwargs.get("index_commits", False)`` -- the receiver must be ``kwargs``
    and the default must be the literal ``False``, so a similar-looking test
    elsewhere can never satisfy this guard."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "get":
        return False
    if not isinstance(func.value, ast.Name) or func.value.id != "kwargs":
        return False
    if len(node.args) != 2:
        return False
    flag, default = node.args
    if not (isinstance(flag, ast.Constant) and flag.value == TEMPORAL_FLAG_NAME):
        return False
    return isinstance(default, ast.Constant) and default.value is False


def _daemon_temporal_branch() -> Optional[ast.If]:
    tree = ast.parse(inspect.getsource(daemon_service_module))
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_temporal_branch_test(node.test):
            return node
    return None


def test_daemon_temporal_branch_checks_for_legacy_shards() -> None:
    """Structural guard: the daemon's OWN temporal-indexing branch must
    consult the pending-legacy-shard predicate before indexing."""
    branch = _daemon_temporal_branch()
    assert branch is not None, (
        'daemon/service.py no longer has an `if kwargs.get("index_commits", '
        "False):` temporal branch -- this guard has drifted from the real code"
    )

    called_names = set()
    for stmt in branch.body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.id
                    if isinstance(func, ast.Name)
                    else getattr(func, "attr", None)
                )
                if name:
                    called_names.add(name)

    assert GUARD_CALL_NAME in called_names, (
        f"the daemon's temporal branch does not call {GUARD_CALL_NAME}(): a "
        "daemon-mode `cidx index --index-commits` against a pre-existing "
        "legacy temporal shard would keep adding vector_*.json files "
        "(Bug #1528)"
    )
