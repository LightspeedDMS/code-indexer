"""Unit tests for the DEFAULT-OFF semantic (vector) index self-heal backfill policy,
including the count-based hardening against the silent-empty failure."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, List
from unittest import mock

import pytest

from code_indexer.server.repositories.background_jobs import (
    POD_PULL_OPS,
    _MEMORY_HEAVY_OPS,
    DuplicateJobError,
)
from code_indexer.server.services.semantic_backfill.policy import (
    SEMANTIC_BACKFILL_REINDEX_OP,
    SEMANTIC_NO_FILES_MARKER,
    SemanticBackfillPolicy,
    SemanticReindexProducedNoVectorsError,
    _EMPTY_PROBE_BYTES,
    _clone_has_only_empty_files,
    semantic_backfill_reindex_executor,
    semantic_index_present,
    semantic_index_vector_count,
)


def _write_semantic_index(repo: Path, *, vector_count: int) -> None:
    """Write a voyage-code-3 collection_meta.json with the given vector_count (the
    trustworthy completeness signal)."""
    coll = repo / ".code-indexer" / "index" / "voyage-code-3"
    coll.mkdir(parents=True, exist_ok=True)
    (coll / "collection_meta.json").write_text(
        json.dumps(
            {"name": "voyage-code-3", "hnsw_index": {"vector_count": vector_count}}
        )
    )


def _write_progress(repo: Path, *, total_files: int) -> None:
    (repo / ".code-indexer").mkdir(parents=True, exist_ok=True)
    (repo / ".code-indexer" / "indexing_progress.json").write_text(
        json.dumps(
            {"current_session": {"total_files": total_files, "files_completed": 0}}
        )
    )


def _repo(tmp_path: Path, *, vectors: int = 0) -> Path:
    (tmp_path / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)
    if vectors > 0:
        _write_semantic_index(tmp_path, vector_count=vectors)
    return tmp_path


class _GRM:
    def __init__(
        self,
        registered: set[str],
        *,
        produce_vectors: int = 0,
        files: int = 0,
        progress: str = "normal",
    ) -> None:
        # progress: "normal" -> current_session with total_files; "absent" -> no progress
        # file (the real file-less case: cidx index raises before start_session); "null" ->
        # a stale file whose current_session is null.
        self._registered = registered
        self._produce = produce_vectors
        self._files = files
        self._progress = progress
        self.reindexed: List[dict] = []

    def get_golden_repo(self, alias: str) -> Any:
        return object() if alias in self._registered else None

    def _execute_post_clone_workflow(self, clone_path, **kwargs):
        # Simulate the real workflow's on-disk effect: (maybe) write progress + the index.
        self.reindexed.append({"clone_path": clone_path, **kwargs})
        repo = Path(clone_path)
        (repo / ".code-indexer").mkdir(parents=True, exist_ok=True)
        if self._progress == "normal":
            _write_progress(repo, total_files=self._files)
        elif self._progress == "null":
            (repo / ".code-indexer" / "indexing_progress.json").write_text(
                json.dumps({"current_session": None})
            )
        elif self._progress == "corrupt":
            (repo / ".code-indexer" / "indexing_progress.json").write_text("{ not json")
        # "absent" -> write no progress file at all
        if self._produce > 0:
            _write_semantic_index(repo, vector_count=self._produce)


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
    assert SEMANTIC_BACKFILL_REINDEX_OP in _MEMORY_HEAVY_OPS
    assert SEMANTIC_BACKFILL_REINDEX_OP in POD_PULL_OPS


def test_vector_count_reads_collection_meta(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "a", vectors=280)
    assert semantic_index_vector_count(repo / ".code-indexer" / "index") == 280
    assert semantic_index_vector_count(tmp_path / "b" / ".code-indexer" / "index") == 0


def test_is_present_is_count_based_not_presence(tmp_path: Path) -> None:
    # Real vectors → present.
    assert semantic_index_present(_repo(tmp_path / "full", vectors=44805)) is True
    # An index dir with a 0-vector collection_meta (the silent-empty trap) → NOT present,
    # so the sweep re-embeds it (a presence-only check would leave it stuck).
    empty = _repo(tmp_path / "empty")
    _write_semantic_index(empty, vector_count=0)
    assert semantic_index_present(empty) is False
    # A marked no-files repo → present (don't re-embed a repo with nothing to embed).
    marked = _repo(tmp_path / "docs")
    (marked / ".code-indexer" / SEMANTIC_NO_FILES_MARKER).write_text("")
    assert semantic_index_present(marked) is True


def test_disabled_is_the_cost_gate(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "svc")
    assert (
        SemanticBackfillPolicy(_GRM({"svc"}), _BJM(), enabled=False).eligibility(repo)
        is None
    )


def test_enabled_eligible_only_for_registered(tmp_path: Path) -> None:
    reg = _repo(tmp_path / "svc")
    unreg = _repo(tmp_path / "stray")
    p = SemanticBackfillPolicy(_GRM({"svc"}), _BJM(), enabled=True)
    assert p.eligibility(reg) == {"clone_path": str(reg), "alias": "svc"}
    assert p.eligibility(unreg) is None


def test_enqueue_submits_pod_pull_job(tmp_path: Path) -> None:
    bjm = _BJM()
    p = SemanticBackfillPolicy(_GRM({"svc"}), bjm, enabled=True)
    meta = {"clone_path": str(tmp_path / "svc"), "alias": "svc"}
    p.enqueue("svc", tmp_path / "svc", meta)
    assert bjm.calls[0]["op"] == SEMANTIC_BACKFILL_REINDEX_OP
    assert bjm.calls[0]["metadata"] == meta


def test_enqueue_swallows_duplicate(tmp_path: Path) -> None:
    p = SemanticBackfillPolicy(_GRM({"svc"}), _BJM(raise_dup=True), enabled=True)
    p.enqueue("svc", tmp_path, {"clone_path": str(tmp_path), "alias": "svc"})


def test_executor_success_returns_vector_count(tmp_path: Path) -> None:
    # A repo that re-embeds to real vectors → success carrying the count (force_init=False).
    grm = _GRM(set(), produce_vectors=280, files=57)
    result = semantic_backfill_reindex_executor(
        grm, {"clone_path": str(tmp_path / "svc"), "alias": "svc"}
    )
    assert grm.reindexed[0]["force_init"] is False
    assert result == {"status": "reindexed", "alias": "svc", "vector_count": 280}


def test_executor_fails_loud_when_files_but_zero_vectors(tmp_path: Path) -> None:
    # THE hardening: had 57 indexable files but produced 0 vectors → the silent-empty
    # failure — must RAISE (job fails, retried) instead of masking as done.
    grm = _GRM(set(), produce_vectors=0, files=57)
    with pytest.raises(SemanticReindexProducedNoVectorsError):
        semantic_backfill_reindex_executor(
            grm, {"clone_path": str(tmp_path / "svc"), "alias": "svc"}
        )


def test_executor_marks_no_files_repo_as_skipped(tmp_path: Path) -> None:
    # A file-less repo with a present-but-0 progress → skip (mark), not fail.
    grm = _GRM(set(), produce_vectors=0, files=0)
    result = semantic_backfill_reindex_executor(
        grm, {"clone_path": str(tmp_path / "docs"), "alias": "docs"}
    )
    assert result["status"] == "skipped_no_files"
    assert (tmp_path / "docs" / ".code-indexer" / SEMANTIC_NO_FILES_MARKER).exists()


def test_executor_skips_when_progress_absent(tmp_path: Path) -> None:
    # Review regression: a GENUINELY file-less repo writes NO progress file (cidx index
    # raises 'No files found to index' before start_session, and the workflow tolerates it).
    # This must route to skip, NOT fail-loud (the bug: absent progress → None → was raising).
    grm = _GRM(set(), produce_vectors=0, progress="absent")
    result = semantic_backfill_reindex_executor(
        grm, {"clone_path": str(tmp_path / "docs"), "alias": "docs"}
    )
    assert result["status"] == "skipped_no_files"
    assert (tmp_path / "docs" / ".code-indexer" / SEMANTIC_NO_FILES_MARKER).exists()


def test_executor_skips_when_session_null(tmp_path: Path) -> None:
    # A stale progress file whose current_session is null → also "no session" → file-less → skip.
    grm = _GRM(set(), produce_vectors=0, progress="null")
    result = semantic_backfill_reindex_executor(
        grm, {"clone_path": str(tmp_path / "docs2"), "alias": "docs2"}
    )
    assert result["status"] == "skipped_no_files"


def test_executor_fails_loud_when_progress_corrupt(tmp_path: Path) -> None:
    # A CORRUPT (unparseable) progress file → cannot confirm file-less → fail loud rather
    # than silently mask a possible real failure.
    grm = _GRM(set(), produce_vectors=0, progress="corrupt")
    with pytest.raises(SemanticReindexProducedNoVectorsError):
        semantic_backfill_reindex_executor(
            grm, {"clone_path": str(tmp_path / "svc"), "alias": "svc"}
        )


def _seed_clone(clone: Path, files: dict[str, bytes]) -> None:
    """Write the clone's own files (the fake GRM only writes .code-indexer/)."""
    clone.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        f = clone / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)


def test_executor_skips_repo_whose_only_file_is_empty(tmp_path: Path) -> None:
    # The real fleet shape: 27 golden repos are placeholders holding a single 0-byte
    # README.md. cidx DISCOVERS it (total_files=1) but there is nothing to chunk, so 0
    # vectors is correct. Before this, discovered>0 took the fail-loud branch and the repo
    # was retried every ~30min forever (585 failed jobs -> server health "degraded").
    clone = tmp_path / "placeholder"
    _seed_clone(clone, {"README.md": b""})
    grm = _GRM(set(), produce_vectors=0, files=1)
    result = semantic_backfill_reindex_executor(
        grm, {"clone_path": str(clone), "alias": "placeholder"}
    )
    assert result["status"] == "skipped_no_files"
    assert (clone / ".code-indexer" / SEMANTIC_NO_FILES_MARKER).exists()


def test_executor_skips_when_all_files_are_whitespace(tmp_path: Path) -> None:
    # A newline-only README (the other on-disk shape observed: 1 byte) is equally empty.
    clone = tmp_path / "ws"
    _seed_clone(clone, {"README.md": b"\n", "docs/notes.txt": b"   \n\t"})
    grm = _GRM(set(), produce_vectors=0, files=2)
    result = semantic_backfill_reindex_executor(
        grm, {"clone_path": str(clone), "alias": "ws"}
    )
    assert result["status"] == "skipped_no_files"


def test_executor_still_fails_loud_when_a_file_has_content(tmp_path: Path) -> None:
    # Guard the guard: ONE non-empty file among empties means there WAS something to
    # embed, so 0 vectors is still the silent-empty failure and must raise.
    clone = tmp_path / "svc"
    _seed_clone(clone, {"README.md": b"", "main.py": b"print('x')\n"})
    grm = _GRM(set(), produce_vectors=0, files=2)
    with pytest.raises(SemanticReindexProducedNoVectorsError):
        semantic_backfill_reindex_executor(
            grm, {"clone_path": str(clone), "alias": "svc"}
        )


def test_executor_still_fails_loud_when_clone_files_are_missing(tmp_path: Path) -> None:
    # The failure this guard exists for: progress claims files but the clone is empty /
    # removed. "No files seen" is NOT "files present but empty" -> must still raise.
    clone = tmp_path / "vanished"
    grm = _GRM(set(), produce_vectors=0, files=57)
    with pytest.raises(SemanticReindexProducedNoVectorsError):
        semantic_backfill_reindex_executor(
            grm, {"clone_path": str(clone), "alias": "vanished"}
        )


def test_executor_fails_loud_when_a_big_file_is_not_read(tmp_path: Path) -> None:
    # A file larger than the probe is assumed to carry content without reading it all --
    # so it must NOT be mistaken for empty even though we never inspect its bytes.
    clone = tmp_path / "big"
    _seed_clone(clone, {"blob.bin": b" " * (_EMPTY_PROBE_BYTES + 1)})
    grm = _GRM(set(), produce_vectors=0, files=1)
    with pytest.raises(SemanticReindexProducedNoVectorsError):
        semantic_backfill_reindex_executor(
            grm, {"clone_path": str(clone), "alias": "big"}
        )


def test_empty_check_ignores_git_and_cidx_dirs(tmp_path: Path) -> None:
    # .git and .code-indexer are not the repo's content: a placeholder repo still counts
    # as empty even though those dirs are full of real bytes.
    clone = tmp_path / "onlymeta"
    _seed_clone(
        clone, {"README.md": b"", ".git/config": b"[core]\n", ".code-indexer/x": b"zz"}
    )
    assert _clone_has_only_empty_files(clone) is True


def test_empty_check_false_when_a_subdirectory_cannot_be_listed(tmp_path: Path) -> None:
    # Review finding: os.walk SWALLOWS listing errors by default. A clone with an empty
    # README plus an unreadable src/ would walk as "only an empty file" -> marked skip,
    # masking a repo that may well have content. The onerror hook must surface it so the
    # repo keeps failing loud instead.
    clone = tmp_path / "locked"
    _seed_clone(clone, {"README.md": b"", "src/main.py": b"print('x')\n"})
    locked = clone / "src"
    os.chmod(locked, 0o000)
    try:
        if os.access(locked, os.R_OK):  # running as root: chmod does not restrict
            pytest.skip("cannot make a directory unreadable as this user")
        assert _clone_has_only_empty_files(clone) is False
    finally:
        os.chmod(locked, 0o755)


def test_empty_check_false_when_a_file_cannot_be_read(tmp_path: Path) -> None:
    # The per-file arm of the same guarantee, driven without relying on filesystem perms.
    clone = tmp_path / "unreadable"
    _seed_clone(clone, {"README.md": b""})

    def _boom(self: Path, *a: object, **k: object) -> bytes:
        raise OSError("nope")

    with mock.patch.object(Path, "read_bytes", _boom):
        assert _clone_has_only_empty_files(clone) is False


def test_empty_check_false_for_absent_clone(tmp_path: Path) -> None:
    assert _clone_has_only_empty_files(tmp_path / "nope") is False


def test_config_ships_off() -> None:
    from code_indexer.server.utils.config_manager import SemanticBackfillSweepConfig

    c = SemanticBackfillSweepConfig()
    assert c.enabled is False
    assert c.batch_size == 1
    assert c.tick_interval_minutes == 30
    assert c.attempt_cooldown_seconds == 86400
