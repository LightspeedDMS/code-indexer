"""Unit tests for the DEFAULT-OFF semantic (vector) index self-heal backfill policy."""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

from code_indexer.server.repositories.background_jobs import (
    POD_PULL_OPS,
    _MEMORY_HEAVY_OPS,
    DuplicateJobError,
)
from code_indexer.server.services.semantic_backfill.policy import (
    SEMANTIC_BACKFILL_REINDEX_OP,
    SemanticBackfillPolicy,
    semantic_backfill_reindex_executor,
    semantic_index_present,
)


def _repo(tmp_path: Path, *, with_vectors: bool) -> Path:
    idx = tmp_path / ".code-indexer" / "index"
    idx.mkdir(parents=True)
    if with_vectors:
        coll = idx / "code_voyage-code-3"
        coll.mkdir()
        (coll / "hnsw_index.bin").write_bytes(b"\x00")
    return tmp_path


class _GRM:
    """Registry stub: get_golden_repo returns a truthy row only for known aliases."""

    def __init__(self, registered: set[str]) -> None:
        self._registered = registered
        self.reindexed: List[dict] = []

    def get_golden_repo(self, alias: str) -> Any:
        return object() if alias in self._registered else None

    def _execute_post_clone_workflow(self, clone_path, **kwargs):
        self.reindexed.append({"clone_path": clone_path, **kwargs})


class _BJM:
    def __init__(self, *, raise_dup: bool = False) -> None:
        self.raise_dup = raise_dup
        self.calls: List[dict] = []

    def submit_job(self, op, fn, **kw: Any) -> str:
        if self.raise_dup:
            raise DuplicateJobError(op, kw.get("repo_alias", ""), "existing")
        self.calls.append({"op": op, "fn": fn, **kw})
        return "job-1"


def test_op_is_pod_pull_and_memory_heavy() -> None:
    # A re-embed is heavy → gated by memory admission (unlike the light FTS build).
    assert SEMANTIC_BACKFILL_REINDEX_OP in _MEMORY_HEAVY_OPS
    assert SEMANTIC_BACKFILL_REINDEX_OP in POD_PULL_OPS


def test_is_present_reflects_semantic_index(tmp_path: Path) -> None:
    assert semantic_index_present(_repo(tmp_path / "a", with_vectors=True)) is True
    assert semantic_index_present(_repo(tmp_path / "b", with_vectors=False)) is False


def test_disabled_is_the_cost_gate_no_eligibility(tmp_path: Path) -> None:
    # DEFAULT OFF: even a registered repo missing its index is NOT eligible when disabled,
    # so the engine enqueues nothing (zero VoyageAI spend until the operator opts in).
    repo = _repo(tmp_path / "svc", with_vectors=False)
    p = SemanticBackfillPolicy(_GRM({"svc"}), _BJM(), enabled=False)
    assert p.eligibility(repo) is None


def test_enabled_eligible_only_for_registered_repos(tmp_path: Path) -> None:
    reg = _repo(tmp_path / "svc", with_vectors=False)
    unreg = _repo(tmp_path / "stray", with_vectors=False)
    p = SemanticBackfillPolicy(_GRM({"svc"}), _BJM(), enabled=True)
    assert p.eligibility(reg) == {"clone_path": str(reg), "alias": "svc"}
    assert p.eligibility(unreg) is None  # not a registered golden repo → never re-embed


def test_enqueue_submits_pod_pull_reindex_job(tmp_path: Path) -> None:
    bjm = _BJM()
    p = SemanticBackfillPolicy(_GRM({"svc"}), bjm, enabled=True)
    meta = {"clone_path": str(tmp_path / "svc"), "alias": "svc"}
    p.enqueue("svc", tmp_path / "svc", meta)
    assert len(bjm.calls) == 1
    call = bjm.calls[0]
    assert call["op"] == SEMANTIC_BACKFILL_REINDEX_OP
    assert call["repo_alias"] == "svc"
    assert call["metadata"] == meta
    assert call["is_admin"] is True


def test_enqueue_swallows_duplicate(tmp_path: Path) -> None:
    p = SemanticBackfillPolicy(_GRM({"svc"}), _BJM(raise_dup=True), enabled=True)
    p.enqueue(
        "svc", tmp_path, {"clone_path": str(tmp_path), "alias": "svc"}
    )  # no raise


def test_executor_reembeds_via_incremental_refresh(tmp_path: Path) -> None:
    # force_init=False is the safety contract: no --force/--clear, so the co-located
    # FTS/SCIP indexes are never wiped — only the missing semantic vectors are filled.
    grm = _GRM(set())
    result = semantic_backfill_reindex_executor(
        grm, {"clone_path": str(tmp_path / "svc"), "alias": "svc", "job_id": "j1"}
    )
    assert grm.reindexed[0]["clone_path"] == str(tmp_path / "svc")
    assert grm.reindexed[0]["force_init"] is False
    assert grm.reindexed[0]["golden_repo_alias"] == "svc"
    assert result == {
        "status": "reindexed",
        "alias": "svc",
        "clone_path": str(tmp_path / "svc"),
    }


def test_config_ships_off() -> None:
    from code_indexer.server.utils.config_manager import SemanticBackfillSweepConfig

    c = SemanticBackfillSweepConfig()
    assert c.enabled is False  # cost gate: default OFF
    assert c.batch_size == 1
    assert c.tick_interval_minutes == 30
    assert c.attempt_cooldown_seconds == 86400
