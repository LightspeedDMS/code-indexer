"""
Bug #1315 (extension): the shared `resolve_alias_or_index_path` helper is
already approved and wired into two call sites (MultiSearchService and
_resolve_global_repo_target). This file extends coverage to the 3 remaining
call sites that still called `AliasManager.read_alias(alias)` directly with
no fallback to the registry row's own `index_path` field:

  - SemanticSearchService._get_repository_path
    (src/code_indexer/server/services/search_service.py)
  - SCIPMultiService._get_repository_path
    (src/code_indexer/server/multi/scip_multi_service.py)
  - RepositoryStatsService.get_repository_metadata
    (src/code_indexer/server/services/stats_service.py)

TDD: these tests are written FIRST and must fail (RED) against the
pre-fix code (direct `read_alias` call, no fallback), then pass (GREEN)
after each site is switched to `resolve_alias_or_index_path`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _build_registry(tmp_path):
    """Construct a real, schema-initialized GlobalReposSqliteBackend."""
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.sqlite_backends import (
        GlobalReposSqliteBackend,
    )

    db_path = tmp_path / "global_repos.db"
    DatabaseSchema(str(db_path)).initialize_database()
    return GlobalReposSqliteBackend(str(db_path))


def _patch_app_state(monkeypatch, golden_repos_dir, backend):
    from code_indexer.server.app import app as real_app

    monkeypatch.setattr(
        real_app.state, "golden_repos_dir", str(golden_repos_dir), raising=False
    )
    monkeypatch.setattr(
        real_app.state,
        "backend_registry",
        SimpleNamespace(global_repos=backend),
        raising=False,
    )


# ---------------------------------------------------------------------------
# Site 1: SemanticSearchService._get_repository_path
# (src/code_indexer/server/services/search_service.py)
# ---------------------------------------------------------------------------


class TestSearchServiceGetRepositoryPathIndexPathFallback:
    def test_falls_back_to_index_path_when_alias_pointer_missing(
        self, tmp_path, monkeypatch
    ):
        from code_indexer.server.services.search_service import (
            SemanticSearchService,
        )

        backend = _build_registry(tmp_path)
        index_dir = tmp_path / "index-b"
        index_dir.mkdir()
        backend.register_repo("repo-b-global", "repo-b", None, str(index_dir))

        golden_repos_dir = tmp_path / "golden-repos"
        (golden_repos_dir / "aliases").mkdir(parents=True)
        # Deliberately no alias pointer for repo-b-global.

        _patch_app_state(monkeypatch, golden_repos_dir, backend)

        service = SemanticSearchService()
        result = service._get_repository_path("repo-b-global")

        assert result == str(index_dir)

    def test_still_raises_when_neither_alias_nor_valid_index_path_resolve(
        self, tmp_path, monkeypatch
    ):
        from code_indexer.server.services.search_service import (
            SemanticSearchService,
        )

        backend = _build_registry(tmp_path)
        missing_index_path = tmp_path / "does-not-exist-c"
        backend.register_repo("repo-c-global", "repo-c", None, str(missing_index_path))

        golden_repos_dir = tmp_path / "golden-repos"
        (golden_repos_dir / "aliases").mkdir(parents=True)

        _patch_app_state(monkeypatch, golden_repos_dir, backend)

        service = SemanticSearchService()

        with pytest.raises(FileNotFoundError) as exc_info:
            service._get_repository_path("repo-c-global")

        assert "not found" in str(exc_info.value).lower()

    def test_alias_pointer_present_still_wins_over_index_path(
        self, tmp_path, monkeypatch
    ):
        from code_indexer.server.services.search_service import (
            SemanticSearchService,
        )
        from code_indexer.global_repos.alias_manager import AliasManager

        backend = _build_registry(tmp_path)
        stale_index_path = tmp_path / "stale-index-a"
        stale_index_path.mkdir()
        current_target = tmp_path / "current-index-a"
        current_target.mkdir()
        backend.register_repo("repo-a-global", "repo-a", None, str(stale_index_path))

        golden_repos_dir = tmp_path / "golden-repos"
        aliases_dir = golden_repos_dir / "aliases"
        aliases_dir.mkdir(parents=True)
        AliasManager(str(aliases_dir)).create_alias(
            "repo-a-global", str(current_target)
        )

        _patch_app_state(monkeypatch, golden_repos_dir, backend)

        service = SemanticSearchService()
        result = service._get_repository_path("repo-a-global")

        assert result == str(current_target)


# ---------------------------------------------------------------------------
# Site 2: SCIPMultiService._get_repository_path
# (src/code_indexer/server/multi/scip_multi_service.py)
# ---------------------------------------------------------------------------


class TestSCIPMultiServiceGetRepositoryPathIndexPathFallback:
    def test_falls_back_to_index_path_when_alias_pointer_missing(
        self, tmp_path, monkeypatch
    ):
        from code_indexer.server.multi.scip_multi_service import SCIPMultiService

        backend = _build_registry(tmp_path)
        index_dir = tmp_path / "index-b"
        index_dir.mkdir()
        backend.register_repo("repo-b-global", "repo-b", None, str(index_dir))

        golden_repos_dir = tmp_path / "golden-repos"
        (golden_repos_dir / "aliases").mkdir(parents=True)

        _patch_app_state(monkeypatch, golden_repos_dir, backend)

        service = SCIPMultiService()
        result = service._get_repository_path("repo-b-global")

        assert result == str(index_dir)

    def test_still_raises_when_neither_alias_nor_valid_index_path_resolve(
        self, tmp_path, monkeypatch
    ):
        from code_indexer.server.multi.scip_multi_service import SCIPMultiService

        backend = _build_registry(tmp_path)
        missing_index_path = tmp_path / "does-not-exist-c"
        backend.register_repo("repo-c-global", "repo-c", None, str(missing_index_path))

        golden_repos_dir = tmp_path / "golden-repos"
        (golden_repos_dir / "aliases").mkdir(parents=True)

        _patch_app_state(monkeypatch, golden_repos_dir, backend)

        service = SCIPMultiService()

        with pytest.raises(FileNotFoundError) as exc_info:
            service._get_repository_path("repo-c-global")

        assert "not found" in str(exc_info.value).lower()

    def test_alias_pointer_present_still_wins_over_index_path(
        self, tmp_path, monkeypatch
    ):
        from code_indexer.server.multi.scip_multi_service import SCIPMultiService
        from code_indexer.global_repos.alias_manager import AliasManager

        backend = _build_registry(tmp_path)
        stale_index_path = tmp_path / "stale-index-a"
        stale_index_path.mkdir()
        current_target = tmp_path / "current-index-a"
        current_target.mkdir()
        backend.register_repo("repo-a-global", "repo-a", None, str(stale_index_path))

        golden_repos_dir = tmp_path / "golden-repos"
        aliases_dir = golden_repos_dir / "aliases"
        aliases_dir.mkdir(parents=True)
        AliasManager(str(aliases_dir)).create_alias(
            "repo-a-global", str(current_target)
        )

        _patch_app_state(monkeypatch, golden_repos_dir, backend)

        service = SCIPMultiService()
        result = service._get_repository_path("repo-a-global")

        assert result == str(current_target)


# ---------------------------------------------------------------------------
# Site 3: RepositoryStatsService.get_repository_metadata
# (src/code_indexer/server/services/stats_service.py)
# ---------------------------------------------------------------------------


class TestStatsServiceGetRepositoryMetadataIndexPathFallback:
    def test_falls_back_to_index_path_when_alias_pointer_missing(
        self, tmp_path, monkeypatch
    ):
        from code_indexer.server.services.stats_service import (
            RepositoryStatsService,
        )

        backend = _build_registry(tmp_path)
        index_dir = tmp_path / "index-b"
        index_dir.mkdir()
        backend.register_repo("repo-b-global", "repo-b", None, str(index_dir))

        golden_repos_dir = tmp_path / "golden-repos"
        (golden_repos_dir / "aliases").mkdir(parents=True)

        _patch_app_state(monkeypatch, golden_repos_dir, backend)

        service = RepositoryStatsService()
        result = service.get_repository_metadata("repo-b-global")

        assert result["clone_path"] == str(index_dir)

    def test_still_raises_when_neither_alias_nor_valid_index_path_resolve(
        self, tmp_path, monkeypatch
    ):
        from code_indexer.server.services.stats_service import (
            RepositoryStatsService,
        )

        backend = _build_registry(tmp_path)
        missing_index_path = tmp_path / "does-not-exist-c"
        backend.register_repo("repo-c-global", "repo-c", None, str(missing_index_path))

        golden_repos_dir = tmp_path / "golden-repos"
        (golden_repos_dir / "aliases").mkdir(parents=True)

        _patch_app_state(monkeypatch, golden_repos_dir, backend)

        service = RepositoryStatsService()

        with pytest.raises(FileNotFoundError) as exc_info:
            service.get_repository_metadata("repo-c-global")

        assert "not found" in str(exc_info.value).lower()

    def test_alias_pointer_present_still_wins_over_index_path(
        self, tmp_path, monkeypatch
    ):
        from code_indexer.server.services.stats_service import (
            RepositoryStatsService,
        )
        from code_indexer.global_repos.alias_manager import AliasManager

        backend = _build_registry(tmp_path)
        stale_index_path = tmp_path / "stale-index-a"
        stale_index_path.mkdir()
        current_target = tmp_path / "current-index-a"
        current_target.mkdir()
        backend.register_repo("repo-a-global", "repo-a", None, str(stale_index_path))

        golden_repos_dir = tmp_path / "golden-repos"
        aliases_dir = golden_repos_dir / "aliases"
        aliases_dir.mkdir(parents=True)
        AliasManager(str(aliases_dir)).create_alias(
            "repo-a-global", str(current_target)
        )

        _patch_app_state(monkeypatch, golden_repos_dir, backend)

        service = RepositoryStatsService()
        result = service.get_repository_metadata("repo-a-global")

        assert result["clone_path"] == str(current_target)
