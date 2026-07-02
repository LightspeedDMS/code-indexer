"""Tests for Bug #1240: Per-point skip log messages must be DEBUG, not WARNING.

The temporal migration service's _build_quarter_buckets function logs one
WARNING per skipped point (missing_id_index, missing_json, timestamp_unresolved).
On large corrupt repos (e.g. ~1,914 orphans per collection x2 providers x N repos),
this floods the SQLite logs.db with rapid writes, saturating its write lock and
causing database-is-locked storms on the Logs DB and dropped logs for other components.

Fix: demote all three per-point skip messages inside _build_quarter_buckets to DEBUG.
The per-collection aggregate WARNING in _migrate_one_collection stays (intentional summary).
"""

import json
import logging
import os
import struct
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np


# ---------------------------------------------------------------------------
# Self-contained setup helpers (avoids cross-test-file imports)
# ---------------------------------------------------------------------------

MIGRATION_LOGGER = "code_indexer.services.temporal.temporal_migration_service"
_COLLECTION_NAME = "code-indexer-temporal-voyage_code_3"
_Q1_DATE = "2024-01-15T12:00:00+00:00"
_Q2_DATE = "2024-05-10T12:00:00+00:00"
_Q3_DATE = "2024-08-20T12:00:00+00:00"


def _write_id_index_bin(path: Path, id_index: Dict[str, str]) -> None:
    """Write binary id_index.bin: {point_id: relative_json_path}."""
    with open(path, "wb") as f:
        f.write(struct.pack("<I", len(id_index)))
        for point_id, rel_path in id_index.items():
            id_bytes = point_id.encode("utf-8")
            path_bytes = rel_path.encode("utf-8")
            f.write(struct.pack("<H", len(id_bytes)))
            f.write(id_bytes)
            f.write(struct.pack("<H", len(path_bytes)))
            f.write(path_bytes)


def _build_monolith_empty_json(
    index_path: Path,
    collection_name: str,
    vectors: "np.ndarray",
    shas: List[str],
    space: str = "cosine",
) -> Path:
    """Build a monolithic temporal collection with empty JSON payload files.

    Returns the collection directory path.
    """
    import hnswlib

    coll_dir = index_path / collection_name
    coll_dir.mkdir(parents=True, exist_ok=True)

    n = len(vectors)
    dim = vectors.shape[1]

    hnsw_idx = hnswlib.Index(space=space, dim=dim)
    hnsw_idx.init_index(
        max_elements=n, M=16, ef_construction=200, allow_replace_deleted=True
    )
    hnsw_idx.add_items(vectors, np.arange(n))
    hnsw_idx.save_index(str(coll_dir / "hnsw_index.bin"))

    id_mapping: Dict[str, str] = {}
    id_index_data: Dict[str, str] = {}

    for i, sha in enumerate(shas):
        point_id = f"myrepo:commit:{sha}:{i}"
        rel_path = f"{i:02x}/vector_{i}.json"
        json_path = coll_dir / rel_path
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text("{}")
        id_mapping[str(i)] = point_id
        id_index_data[point_id] = rel_path

    _write_id_index_bin(coll_dir / "id_index.bin", id_index_data)

    meta = {
        "name": collection_name,
        "vector_size": dim,
        "created_at": datetime.utcnow().isoformat(),
        "hnsw_index": {
            "version": 1,
            "vector_count": n,
            "vector_dim": dim,
            "M": 16,
            "ef_construction": 200,
            "space": space,
            "last_rebuild": datetime.utcnow().isoformat(),
            "file_size_bytes": (coll_dir / "hnsw_index.bin").stat().st_size,
            "id_mapping": id_mapping,
            "is_stale": False,
            "last_marked_stale": None,
        },
    }
    with open(coll_dir / "collection_meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    return coll_dir


def _make_git_repo_with_dated_commits(
    tmp_path: Path, date_iso_strs: List[str]
) -> Tuple[Path, List[str]]:
    """Create a real git repo with commits at specific ISO 8601 dates."""
    repo_path = tmp_path / "gitrepo"
    repo_path.mkdir()
    subprocess.run(["git", "init", str(repo_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(repo_path),
        check=True,
        capture_output=True,
    )
    shas = []
    for i, date_str in enumerate(date_iso_strs):
        (repo_path / f"file{i}.txt").write_text(f"content {i}")
        subprocess.run(
            ["git", "add", "."], cwd=str(repo_path), check=True, capture_output=True
        )
        env = dict(os.environ)
        env["GIT_COMMITTER_DATE"] = date_str
        env["GIT_AUTHOR_DATE"] = date_str
        subprocess.run(
            ["git", "commit", "-m", f"commit {i}"],
            cwd=str(repo_path),
            check=True,
            capture_output=True,
            env=env,
        )
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        shas.append(sha)
    return repo_path, shas


def _setup_monolith_with_missing_json_orphan(
    tmp_path: Path, dim: int = 8
) -> Tuple[Path, Path, Path]:
    """Build a 3-vector monolith with one JSON payload deleted (structural orphan).

    Returns (repo_path, index_path, coll_dir).
    """
    repo_path, shas = _make_git_repo_with_dated_commits(
        tmp_path, [_Q1_DATE, _Q2_DATE, _Q3_DATE]
    )
    index_path = tmp_path / "index"
    index_path.mkdir()
    coll_dir = _build_monolith_empty_json(
        index_path, _COLLECTION_NAME, np.random.rand(3, dim).astype(np.float32), shas
    )
    all_payloads = sorted(
        f for f in coll_dir.rglob("*.json") if f.name != "collection_meta.json"
    )
    assert len(all_payloads) == 3, (
        f"Setup: expected 3 payloads, got {len(all_payloads)}"
    )
    all_payloads[0].unlink()
    return repo_path, index_path, coll_dir


# ---------------------------------------------------------------------------
# Bug #1240 tests
# ---------------------------------------------------------------------------


class TestBug1240PerPointLogLevel:
    """Per-point skip messages inside _build_quarter_buckets must be at DEBUG.

    Before fix: logger.warning() per-point -> log-storm on large corrupt repos.
    After fix: logger.debug() per-point -> aggregate WARNING summary unchanged.
    """

    def test_missing_id_index_per_point_logged_at_debug(self, tmp_path, caplog):
        """'has no id_index entry' per-point message must be DEBUG, not WARNING.

        RED: fails with current code (logger.warning on line ~394).
        GREEN: passes after demoting to logger.debug.
        """
        from code_indexer.services.temporal.temporal_migration_service import (
            _build_quarter_buckets,
        )

        coll_dir = tmp_path / "coll"
        coll_dir.mkdir()

        valid_sha = "a" * 40
        (coll_dir / "valid.json").write_text(
            '{"payload": {"commit_timestamp": 1704067200}}'
        )

        # orphan_no_index is NOT in point_id_to_rel_path -> triggers missing_id_index branch
        label_to_point_id = {
            0: "orphan_no_index",
            1: f"myrepo:commit:{valid_sha}:1",
        }
        point_id_to_rel_path = {
            f"myrepo:commit:{valid_sha}:1": "valid.json",
            # "orphan_no_index" intentionally absent
        }

        with caplog.at_level(logging.DEBUG, logger=MIGRATION_LOGGER):
            buckets, drop_counts = _build_quarter_buckets(
                collection_path=coll_dir,
                label_to_point_id=label_to_point_id,
                point_id_to_rel_path=point_id_to_rel_path,
            )

        # Behavior preserved: drop count incremented and valid point bucketed
        assert drop_counts["missing_id_index"] == 1, (
            "drop_counts['missing_id_index'] must still be incremented after log-level fix"
        )
        assert len(buckets) >= 1, "Valid point must still be bucketed"

        # Bug #1240: per-point message must NOT appear at WARNING level
        warning_msgs = [
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert not any("id_index entry" in m for m in warning_msgs), (
            f"Bug #1240: 'has no id_index entry' is logged at WARNING (log-storm risk). "
            f"Found in WARNING: {[m for m in warning_msgs if 'id_index' in m]}"
        )

        # Must appear at DEBUG level (message still emitted, just quieter)
        debug_msgs = [
            r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG
        ]
        assert any("id_index entry" in m for m in debug_msgs), (
            "Per-point 'has no id_index entry' message must still be emitted at DEBUG level"
        )

    def test_missing_json_per_point_logged_at_debug(self, tmp_path, caplog):
        """'JSON payload ... not found' per-point message must be DEBUG, not WARNING.

        RED: fails with current code (logger.warning on line ~404).
        GREEN: passes after demoting to logger.debug.
        """
        from code_indexer.services.temporal.temporal_migration_service import (
            _build_quarter_buckets,
        )

        coll_dir = tmp_path / "coll"
        coll_dir.mkdir()

        valid_sha = "b" * 40
        (coll_dir / "valid.json").write_text(
            '{"payload": {"commit_timestamp": 1704067200}}'
        )

        # orphan is in id_index but its JSON file does not exist -> missing_json branch
        label_to_point_id = {
            0: "myrepo:commit:orphan_bad_sha:0",
            1: f"myrepo:commit:{valid_sha}:1",
        }
        point_id_to_rel_path = {
            "myrepo:commit:orphan_bad_sha:0": "ghost/missing.json",  # does not exist
            f"myrepo:commit:{valid_sha}:1": "valid.json",
        }

        with caplog.at_level(logging.DEBUG, logger=MIGRATION_LOGGER):
            buckets, drop_counts = _build_quarter_buckets(
                collection_path=coll_dir,
                label_to_point_id=label_to_point_id,
                point_id_to_rel_path=point_id_to_rel_path,
            )

        assert drop_counts["missing_json"] == 1, (
            "drop_counts['missing_json'] must still be incremented after log-level fix"
        )
        assert len(buckets) >= 1, "Valid point must still be bucketed"

        warning_msgs = [
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert not any("skipping point" in m for m in warning_msgs), (
            f"Bug #1240: per-point 'skipping point' is at WARNING (log-storm risk). "
            f"Found in WARNING: {[m for m in warning_msgs if 'skipping point' in m]}"
        )

        debug_msgs = [
            r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG
        ]
        assert any("not found" in m for m in debug_msgs), (
            "Per-point JSON-not-found message must still be emitted at DEBUG level"
        )

    def test_timestamp_unresolved_per_point_logged_at_debug(self, tmp_path, caplog):
        """'no commit_timestamp' per-point message must be DEBUG, not WARNING.

        RED: fails with current code (logger.warning on line ~427).
        GREEN: passes after demoting to logger.debug.
        """
        from code_indexer.services.temporal.temporal_migration_service import (
            _build_quarter_buckets,
        )

        coll_dir = tmp_path / "coll"
        coll_dir.mkdir()

        valid_sha = "c" * 40
        (coll_dir / "valid.json").write_text(
            '{"payload": {"commit_timestamp": 1704067200}}'
        )
        # unresolved: JSON exists but no commit_timestamp field; no git SHA match
        (coll_dir / "unresolved.json").write_text("{}")

        # "synthetic:ts:unresolved:0" has 4 colon-parts but parts[2]="unresolved" is
        # not a 40-char hex SHA -> _extract_sha_from_point_id returns None -> no git ts.
        # JSON exists but is {} -> no commit_timestamp -> timestamp_unresolved branch.
        label_to_point_id = {
            0: "synthetic:ts:unresolved:0",
            1: f"myrepo:commit:{valid_sha}:1",
        }
        point_id_to_rel_path = {
            "synthetic:ts:unresolved:0": "unresolved.json",
            f"myrepo:commit:{valid_sha}:1": "valid.json",
        }

        with caplog.at_level(logging.DEBUG, logger=MIGRATION_LOGGER):
            buckets, drop_counts = _build_quarter_buckets(
                collection_path=coll_dir,
                label_to_point_id=label_to_point_id,
                point_id_to_rel_path=point_id_to_rel_path,
                sha_timestamps={},  # no pre-loaded git timestamps
            )

        assert drop_counts["timestamp_unresolved"] == 1, (
            "drop_counts['timestamp_unresolved'] must still be incremented after log-level fix"
        )
        assert len(buckets) >= 1, "Valid point must still be bucketed"

        warning_msgs = [
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert not any("no commit_timestamp" in m for m in warning_msgs), (
            f"Bug #1240: per-point 'no commit_timestamp' is at WARNING (log-storm risk). "
            f"Found in WARNING: {[m for m in warning_msgs if 'commit_timestamp' in m]}"
        )

        debug_msgs = [
            r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG
        ]
        assert any("commit_timestamp" in m for m in debug_msgs), (
            "Per-point 'no commit_timestamp' message must still be emitted at DEBUG level"
        )

    def test_aggregate_structural_orphan_reported_via_raise(self, tmp_path, caplog):
        """Bug #1286 supersedes this test's original #1240 premise: a structural
        orphan no longer logs a WARNING-and-continue aggregate — it now hard-aborts
        the migration with a RuntimeError whose message carries the orphan
        breakdown (the same information the old aggregate WARNING carried, now
        delivered as a loud failure instead of a silent-success WARNING).

        Per-point messages must still never appear above DEBUG (that part of the
        #1240 fix is unrelated and remains intact).
        """
        import pytest

        from code_indexer.services.temporal.temporal_migration_service import (
            run_temporal_migration,
        )

        repo_path, index_path, coll_dir = _setup_monolith_with_missing_json_orphan(
            tmp_path
        )

        with caplog.at_level(logging.DEBUG, logger=MIGRATION_LOGGER):
            with pytest.raises(RuntimeError) as exc_info:
                run_temporal_migration(
                    index_path=index_path,
                    repo_alias="test-repo",
                    repo_path=repo_path,
                )

        # Orphan breakdown now carried in the raised exception, not a WARNING log
        assert "missing_json" in str(exc_info.value), (
            f"Expected orphan breakdown in raised message, got: {exc_info.value}"
        )
        assert not (coll_dir / "migration_complete.marker").exists(), (
            "marker must NOT be written when a structural orphan aborts the migration"
        )

        # Per-point messages must NOT appear at WARNING (that part of #1240 stands)
        warning_msgs = [
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert not any("skipping point" in m for m in warning_msgs), (
            f"Bug #1240: per-point 'skipping point' still at WARNING after fix. "
            f"WARNING messages: {warning_msgs}"
        )

    def test_drop_counts_unaffected_by_log_level_change(self, tmp_path):
        """drop_counts increments are behavior-unchanged by the log-level fix.

        All three drop categories (missing_id_index, missing_json,
        timestamp_unresolved) must still be counted correctly; only the log
        severity changes from WARNING to DEBUG.
        """
        from code_indexer.services.temporal.temporal_migration_service import (
            _build_quarter_buckets,
        )

        coll_dir = tmp_path / "coll"
        coll_dir.mkdir()

        valid_sha = "d" * 40
        (coll_dir / "valid.json").write_text(
            '{"payload": {"commit_timestamp": 1704067200}}'
        )
        (coll_dir / "unresolved.json").write_text("{}")

        label_to_point_id = {
            0: "orphan_no_index",  # missing_id_index
            1: "myrepo:commit:orphan_bad_sha:1",  # missing_json (ghost.json absent)
            2: "synthetic:ts:unresolved:2",  # timestamp_unresolved
            3: f"myrepo:commit:{valid_sha}:3",  # valid -> bucketed
        }
        point_id_to_rel_path = {
            "myrepo:commit:orphan_bad_sha:1": "ghost.json",  # does not exist
            "synthetic:ts:unresolved:2": "unresolved.json",  # exists but no ts
            f"myrepo:commit:{valid_sha}:3": "valid.json",
            # "orphan_no_index" intentionally absent
        }

        buckets, drop_counts = _build_quarter_buckets(
            collection_path=coll_dir,
            label_to_point_id=label_to_point_id,
            point_id_to_rel_path=point_id_to_rel_path,
            sha_timestamps={},
        )

        assert drop_counts["missing_id_index"] == 1, (
            "missing_id_index count unchanged by log-level fix"
        )
        assert drop_counts["missing_json"] == 1, (
            "missing_json count unchanged by log-level fix"
        )
        assert drop_counts["timestamp_unresolved"] == 1, (
            "timestamp_unresolved count unchanged by log-level fix"
        )
        total_entries = sum(len(v) for v in buckets.values())
        assert total_entries == 1, (
            "Only the 1 valid vector must be bucketed (3 orphans dropped)"
        )
