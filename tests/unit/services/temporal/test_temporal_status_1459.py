"""Tests for the shared resolver-aware temporal-status helper (GitHub Issue
#1459 AC4, Story #1457 follow-on).

`get_temporal_repo_status()` is the ONE reusable answer to "does this repo
have temporal data, and is it queryable" -- built entirely on Story #1457's
`TemporalShardResolver`, never a parallel sister-root scan. Five
observability/inspection call sites route through this exact function
instead of scanning the local clone directly.

These tests use a REAL AliasManager against a real tmp_path directory and
real marker files on disk (never a mock of AliasManager or the resolver's
own logic) -- Messi Rule #1 anti-mock.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.services.temporal.temporal_shard_resolver import (
    TemporalShardResolver,
    TemporalShardSource,
)
from code_indexer.services.temporal.temporal_status import (
    TemporalRepoStatus,
    get_temporal_repo_status,
)


def _legacy_index_path(tmp_path: Path) -> Path:
    return tmp_path / "clone" / ".code-indexer" / "index"


def _golden_repos_dir(tmp_path: Path) -> Path:
    return tmp_path / "golden-repos"


def _write_committed_row(shard_dir: Path) -> None:
    nested = shard_dir / "a" / "b" / "c" / "d"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "vector_abc123.json").write_text('{"point_id": "p1"}')


def test_no_temporal_data_anywhere_returns_all_false(tmp_path):
    """Neither the local clone nor the sister location has any temporal
    directory or alias pointer -- has_data/is_queryable both False."""
    legacy_index_path = _legacy_index_path(tmp_path)
    legacy_index_path.mkdir(parents=True)

    result = get_temporal_repo_status(
        golden_repos_dir=_golden_repos_dir(tmp_path),
        repo_alias="evolution",
        legacy_index_path=legacy_index_path,
    )

    assert result == TemporalRepoStatus(
        has_data=False, is_queryable=False, resolved_path=None, resolved_source=None
    )


def test_no_temporal_data_when_legacy_index_path_does_not_exist(tmp_path):
    """legacy_index_path itself may not exist yet (repo never indexed) --
    must not raise."""
    result = get_temporal_repo_status(
        golden_repos_dir=_golden_repos_dir(tmp_path),
        repo_alias="evolution",
        legacy_index_path=_legacy_index_path(tmp_path),
    )

    assert result.has_data is False
    assert result.is_queryable is False


def test_local_clone_only_queryable_data_is_detected(tmp_path):
    """REGRESSION SAFETY: pre-relocation local-clone-only temporal data
    (real committed rows + hnsw_index.bin) is still correctly detected as
    present and queryable -- the pre-existing behavior these 5 call sites
    already had must not regress."""
    legacy_index_path = _legacy_index_path(tmp_path)
    shard_dir = legacy_index_path / "code-indexer-temporal-voyage_code_3-2024Q1"
    _write_committed_row(shard_dir)
    (shard_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")

    result = get_temporal_repo_status(
        golden_repos_dir=_golden_repos_dir(tmp_path),
        repo_alias="evolution",
        legacy_index_path=legacy_index_path,
    )

    assert result.has_data is True
    assert result.is_queryable is True
    assert result.resolved_source == TemporalShardSource.IN_REPO_LEGACY
    assert result.resolved_path == shard_dir


def test_local_clone_only_crash_window_is_not_reported_queryable(tmp_path):
    """REGRESSION SAFETY + queryability-aware reporting: committed rows
    exist locally but hnsw_index.bin has not been written yet (crash
    window) -- has_data True, is_queryable MUST be False (never conflate
    catalog-presence with queryability, per AC4's binding requirement)."""
    legacy_index_path = _legacy_index_path(tmp_path)
    shard_dir = legacy_index_path / "code-indexer-temporal-voyage_code_3-2024Q1"
    _write_committed_row(shard_dir)

    result = get_temporal_repo_status(
        golden_repos_dir=_golden_repos_dir(tmp_path),
        repo_alias="evolution",
        legacy_index_path=legacy_index_path,
    )

    assert result.has_data is True
    assert result.is_queryable is False
    assert result.resolved_source == TemporalShardSource.IN_REPO_LEGACY


def test_sister_relocated_data_is_detected_as_present_and_queryable(tmp_path):
    """THE ACTUAL BUG FIX: temporal data has relocated to the sister
    location (real alias pointer + real hnsw_index.bin at the sister
    target) with ZERO copy left in the local clone -- must be detected as
    present and queryable via the resolver, not reported as "not indexed"
    (which a bare local-clone scan would incorrectly do)."""
    golden_repos_dir = _golden_repos_dir(tmp_path)
    legacy_index_path = _legacy_index_path(tmp_path)
    legacy_index_path.mkdir(parents=True)  # exists, but empty -- no local copy

    sister_version_dir = (
        golden_repos_dir
        / ".versioned"
        / "evolution-temporal-voyage_code_3-2024Q1"
        / "v_1700000000"
    )
    sister_version_dir.mkdir(parents=True)
    (sister_version_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")

    aliases_dir = golden_repos_dir / "aliases"
    alias_manager = AliasManager(str(aliases_dir))
    alias_manager.create_alias(
        "evolution-temporal-voyage_code_3-2024Q1", str(sister_version_dir)
    )

    result = get_temporal_repo_status(
        golden_repos_dir=golden_repos_dir,
        repo_alias="evolution",
        legacy_index_path=legacy_index_path,
    )

    assert result.has_data is True
    assert result.is_queryable is True
    assert result.resolved_source == TemporalShardSource.SISTER_POINTER
    assert result.resolved_path == sister_version_dir


def test_sister_pointer_takes_precedence_over_stale_local_clone_copy(tmp_path):
    """Pointer-first semantics (Story #1457 AC8): once a sister pointer
    exists for a namespace, it is authoritative even if a stale local copy
    has not been cleaned up yet."""
    golden_repos_dir = _golden_repos_dir(tmp_path)
    legacy_index_path = _legacy_index_path(tmp_path)
    stale_shard_dir = legacy_index_path / "code-indexer-temporal-voyage_code_3-2024Q1"
    _write_committed_row(stale_shard_dir)
    (stale_shard_dir / "hnsw_index.bin").write_bytes(b"stale-hnsw")

    sister_version_dir = (
        golden_repos_dir
        / ".versioned"
        / "evolution-temporal-voyage_code_3-2024Q1"
        / "v_1700000001"
    )
    sister_version_dir.mkdir(parents=True)
    (sister_version_dir / "hnsw_index.bin").write_bytes(b"fresh-hnsw")

    aliases_dir = golden_repos_dir / "aliases"
    AliasManager(str(aliases_dir)).create_alias(
        "evolution-temporal-voyage_code_3-2024Q1", str(sister_version_dir)
    )

    result = get_temporal_repo_status(
        golden_repos_dir=golden_repos_dir,
        repo_alias="evolution",
        legacy_index_path=legacy_index_path,
    )

    assert result.resolved_source == TemporalShardSource.SISTER_POINTER
    assert result.resolved_path == sister_version_dir


def test_multiple_embedder_slugs_all_considered(tmp_path):
    """Two configured embedders: one still local (queryable), one fully
    relocated to the sister location (queryable) -- both discovered via
    enumeration, has_data/is_queryable both True."""
    golden_repos_dir = _golden_repos_dir(tmp_path)
    legacy_index_path = _legacy_index_path(tmp_path)

    local_shard_dir = legacy_index_path / "code-indexer-temporal-voyage_code_3-2024Q1"
    _write_committed_row(local_shard_dir)
    (local_shard_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")

    sister_version_dir = (
        golden_repos_dir
        / ".versioned"
        / "evolution-temporal-embed_v4_0-2024Q2"
        / "v_1700000002"
    )
    sister_version_dir.mkdir(parents=True)
    (sister_version_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")
    AliasManager(str(golden_repos_dir / "aliases")).create_alias(
        "evolution-temporal-embed_v4_0-2024Q2", str(sister_version_dir)
    )

    result = get_temporal_repo_status(
        golden_repos_dir=golden_repos_dir,
        repo_alias="evolution",
        legacy_index_path=legacy_index_path,
    )

    assert result.has_data is True
    assert result.is_queryable is True


def test_quarter_less_monolith_form_is_detected(tmp_path):
    """The quarter-less monolith physical form (no -YYYYQN suffix) is
    correctly enumerated and resolved too."""
    legacy_index_path = _legacy_index_path(tmp_path)
    shard_dir = legacy_index_path / "code-indexer-temporal-voyage_code_3"
    _write_committed_row(shard_dir)
    (shard_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")

    result = get_temporal_repo_status(
        golden_repos_dir=_golden_repos_dir(tmp_path),
        repo_alias="evolution",
        legacy_index_path=legacy_index_path,
    )

    assert result.has_data is True
    assert result.is_queryable is True
    assert result.resolved_path == shard_dir


def test_sister_relocated_data_detected_with_no_local_clone_directory_at_all(
    tmp_path,
):
    """The local clone's .code-indexer/index/ directory may not exist AT
    ALL (never created, or fully cleaned up post-relocation) -- sister data
    must still be discovered purely from the aliases directory scan."""
    golden_repos_dir = _golden_repos_dir(tmp_path)
    legacy_index_path = _legacy_index_path(tmp_path)  # never created

    sister_version_dir = (
        golden_repos_dir
        / ".versioned"
        / "evolution-temporal-voyage_code_3-2024Q3"
        / "v_1700000003"
    )
    sister_version_dir.mkdir(parents=True)
    (sister_version_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")
    AliasManager(str(golden_repos_dir / "aliases")).create_alias(
        "evolution-temporal-voyage_code_3-2024Q3", str(sister_version_dir)
    )

    result = get_temporal_repo_status(
        golden_repos_dir=golden_repos_dir,
        repo_alias="evolution",
        legacy_index_path=legacy_index_path,
    )

    assert result.has_data is True
    assert result.is_queryable is True
    assert result.resolved_source == TemporalShardSource.SISTER_POINTER


def test_stray_non_directory_entry_in_legacy_index_path_is_skipped(tmp_path):
    """A stray regular file directly under legacy_index_path (never a real
    temporal shard directory) is skipped by the enumeration scan, not
    treated as a candidate."""
    legacy_index_path = _legacy_index_path(tmp_path)
    legacy_index_path.mkdir(parents=True)
    (legacy_index_path / "some-stray-file.txt").write_text("not a shard dir")

    result = get_temporal_repo_status(
        golden_repos_dir=_golden_repos_dir(tmp_path),
        repo_alias="evolution",
        legacy_index_path=legacy_index_path,
    )

    assert result.has_data is False


def test_alias_file_with_empty_remainder_after_prefix_is_skipped(tmp_path):
    """An alias pointer file literally named '{repo_alias}-temporal-.json'
    (empty embedder_slug remainder after stripping the prefix) is skipped
    -- never crashes, never counted as a valid candidate."""
    golden_repos_dir = _golden_repos_dir(tmp_path)
    aliases_dir = golden_repos_dir / "aliases"
    aliases_dir.mkdir(parents=True)
    (aliases_dir / "evolution-temporal-.json").write_text("{}")

    result = get_temporal_repo_status(
        golden_repos_dir=golden_repos_dir,
        repo_alias="evolution",
        legacy_index_path=_legacy_index_path(tmp_path),
    )

    assert result.has_data is False


@pytest.mark.skipif(
    os.geteuid() == 0, reason="permission bits are bypassed when running as root"
)
def test_legacy_index_path_permission_error_is_logged_and_treated_as_empty(
    tmp_path,
):
    """A legacy_index_path that exists but cannot be listed (e.g. a
    permission problem) is logged and treated as "no in-repo candidates
    found this call" -- fail-open, never raises."""
    legacy_index_path = _legacy_index_path(tmp_path)
    legacy_index_path.mkdir(parents=True)
    original_mode = legacy_index_path.stat().st_mode
    legacy_index_path.chmod(0)
    try:
        result = get_temporal_repo_status(
            golden_repos_dir=_golden_repos_dir(tmp_path),
            repo_alias="evolution",
            legacy_index_path=legacy_index_path,
        )
    finally:
        legacy_index_path.chmod(stat.S_IMODE(original_mode) | stat.S_IRWXU)

    assert result.has_data is False


@pytest.mark.skipif(
    os.geteuid() == 0, reason="permission bits are bypassed when running as root"
)
def test_aliases_dir_permission_error_is_logged_and_treated_as_empty(tmp_path):
    """An aliases_dir that exists but cannot be globbed (e.g. a permission
    problem) is treated as "no sister-pointer candidates found this call"
    -- fail-open, never raises.

    Note: empirically, `pathlib.Path.glob()` in this Python version
    silently swallows `PermissionError` internally (returns an empty
    result rather than propagating), so this specific scenario does not
    actually exercise the production `except OSError` block around the
    glob() call -- that block remains defense-in-depth for Python versions
    /filesystems where glob() does propagate. This test verifies the
    OBSERVABLE fail-open behavior end-to-end regardless of which layer
    absorbs the error.
    """
    golden_repos_dir = _golden_repos_dir(tmp_path)
    aliases_dir = golden_repos_dir / "aliases"
    aliases_dir.mkdir(parents=True)
    original_mode = aliases_dir.stat().st_mode
    aliases_dir.chmod(0)
    try:
        result = get_temporal_repo_status(
            golden_repos_dir=golden_repos_dir,
            repo_alias="evolution",
            legacy_index_path=_legacy_index_path(tmp_path),
        )
    finally:
        aliases_dir.chmod(stat.S_IMODE(original_mode) | stat.S_IRWXU)

    assert result.has_data is False


def test_toctou_race_where_catalog_reports_a_quarter_resolve_no_longer_finds(
    tmp_path,
):
    """TOCTOU safety: catalog() can report a quarter that no longer
    resolves by the time resolve() is called (e.g. a concurrent cleanup
    landed between the two calls) -- get_temporal_repo_status() must skip
    that entry rather than crash, while still correctly picking up a
    SEPARATE, genuinely resolvable quarter.

    Patches ONLY TemporalShardResolver.catalog() (one method on the real
    resolver class) to inject one extra, deliberately non-resolvable
    quarter alongside the real ones it already found -- resolve() itself,
    AliasManager, and the filesystem remain 100% real and unmocked. This
    is the only way to deterministically land the race window; documented
    per Messi Rule #1's "last resort, document why".
    """
    legacy_index_path = _legacy_index_path(tmp_path)
    real_shard_dir = legacy_index_path / "code-indexer-temporal-voyage_code_3-2024Q1"
    _write_committed_row(real_shard_dir)
    (real_shard_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")

    original_catalog = TemporalShardResolver.catalog

    def _catalog_with_phantom_quarter(self, embedder_slug):
        real_quarters = original_catalog(self, embedder_slug)
        return [*real_quarters, "2099Q4"]

    with patch.object(TemporalShardResolver, "catalog", _catalog_with_phantom_quarter):
        result = get_temporal_repo_status(
            golden_repos_dir=_golden_repos_dir(tmp_path),
            repo_alias="evolution",
            legacy_index_path=legacy_index_path,
        )

    assert result.has_data is True
    assert result.is_queryable is True
    assert result.resolved_path == real_shard_dir
