"""GitHub Issue #1459 AC4: web/routes.py's _detect_index_flags() temporal
detection must route through the shared TemporalShardResolver-based
get_temporal_repo_status() helper (via a new optional repo_alias param)
instead of scanning only the local clone path -- so a golden repo whose
temporal data has relocated to Story #1457's sister location is still
reported has_temporal=True, never False.

_detect_index_flags() is a pure, directly-callable function (see the
existing test_repository_health_dynamic_semantic.py convention for
similarly-shaped helpers in this codebase) -- called directly here, real
filesystem + real AliasManager, zero mocking of resolver internals.
"""

from __future__ import annotations

from pathlib import Path

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server.web.routes import _detect_index_flags


def _write_committed_row(shard_dir: Path) -> None:
    nested = shard_dir / "a"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "vector_abc123.json").write_text('{"point_id": "p1"}')


def test_local_clone_only_temporal_data_still_detected(tmp_path, monkeypatch):
    """REGRESSION SAFETY: pre-relocation local-clone temporal data is still
    correctly detected -- unaffected by the new repo_alias param."""
    clone_path = tmp_path / "clone"
    shard_dir = (
        clone_path
        / ".code-indexer"
        / "index"
        / "code-indexer-temporal-voyage_code_3-2024Q1"
    )
    _write_committed_row(shard_dir)
    (shard_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")

    server_data_dir = tmp_path / "server-data"
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(server_data_dir))

    flags = _detect_index_flags(str(clone_path), repo_alias="myrepo")

    assert flags["has_temporal"] is True


def test_no_repo_alias_preserves_pre_existing_local_scan_only_behavior(tmp_path):
    """Backward compatibility: repo_alias omitted (default None) behaves
    byte-identically to the pre-#1459 local-scan-only implementation."""
    clone_path = tmp_path / "clone"
    shard_dir = (
        clone_path
        / ".code-indexer"
        / "index"
        / "code-indexer-temporal-voyage_code_3-2024Q1"
    )
    _write_committed_row(shard_dir)
    (shard_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")

    flags = _detect_index_flags(str(clone_path))

    assert flags["has_temporal"] is True


def test_sister_relocated_temporal_data_is_detected_not_reported_missing(
    tmp_path, monkeypatch
):
    """THE ACTUAL BUG FIX: temporal data relocated to the sister location
    (real alias pointer, real hnsw_index.bin), ZERO local clone copy --
    must be reported has_temporal=True, never False."""
    clone_path = tmp_path / "clone"
    (clone_path / ".code-indexer" / "index").mkdir(parents=True)

    server_data_dir = tmp_path / "server-data"
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(server_data_dir))
    golden_repos_dir = server_data_dir / "data" / "golden-repos"

    sister_version_dir = (
        golden_repos_dir
        / ".versioned"
        / "myrepo-temporal-voyage_code_3-2024Q1"
        / "v_1700000000"
    )
    sister_version_dir.mkdir(parents=True)
    (sister_version_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")
    AliasManager(str(golden_repos_dir / "aliases")).create_alias(
        "myrepo-temporal-voyage_code_3-2024Q1", str(sister_version_dir)
    )

    flags = _detect_index_flags(str(clone_path), repo_alias="myrepo")

    assert flags["has_temporal"] is True


def test_no_temporal_data_anywhere_reports_false(tmp_path, monkeypatch):
    """Neither local clone nor sister location has temporal data --
    has_temporal remains False (no false positive introduced)."""
    clone_path = tmp_path / "clone"
    (clone_path / ".code-indexer" / "index").mkdir(parents=True)

    server_data_dir = tmp_path / "server-data"
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(server_data_dir))

    flags = _detect_index_flags(str(clone_path), repo_alias="myrepo")

    assert flags["has_temporal"] is False
