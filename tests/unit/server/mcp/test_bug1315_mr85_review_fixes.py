"""
Bug #1315 MR !85 review fixes: tests for the remaining call sites patched
in commit 8734f45 (fix/bug-1315-remaining-sites), covering:

  1. SCIPQueryService._resolve_alias_scip_root
     (src/code_indexer/server/services/scip_query_service.py)
     -- includes regression test for the repo_entry reuse bug (each alias
     variant must look up its own repo_entry)
  2. _resolve_temporal_repo_path_rest
     (src/code_indexer/server/routers/inline_query.py)
  3. routes.py wiki-toggle / query_submit resolve pattern
     (src/code_indexer/server/web/routes.py)

Each site is tested for:
  - "alias pointer missing, index_path fallback succeeds"
  - "alias pointer present, still wins"
"""

from __future__ import annotations

from types import SimpleNamespace


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
# Site: SCIPQueryService._resolve_alias_scip_root
# (src/code_indexer/server/services/scip_query_service.py)
# ---------------------------------------------------------------------------


class TestSCIPQueryServiceResolveAliasScipRoot:
    """Tests for _resolve_alias_scip_root's index_path fallback."""

    def test_falls_back_to_index_path_when_alias_pointer_missing(
        self, tmp_path, monkeypatch
    ):
        """When no alias pointer exists, resolve via registry index_path."""
        from code_indexer.server.services.scip_query_service import SCIPQueryService

        backend = _build_registry(tmp_path)
        index_dir = tmp_path / "index-scip"
        index_dir.mkdir()
        backend.register_repo("myrepo-global", "myrepo", None, str(index_dir))

        golden_repos_dir = tmp_path / "golden-repos"
        (golden_repos_dir / "aliases").mkdir(parents=True)
        # Deliberately no alias pointer for myrepo-global.

        _patch_app_state(monkeypatch, golden_repos_dir, backend)

        service = SCIPQueryService(golden_repos_dir=golden_repos_dir)
        result = service._resolve_alias_scip_root("myrepo")

        assert result is not None
        assert str(result) == str(index_dir)

    def test_alias_pointer_present_still_wins_over_index_path(
        self, tmp_path, monkeypatch
    ):
        """When alias pointer exists, it takes precedence over index_path."""
        from code_indexer.global_repos.alias_manager import AliasManager
        from code_indexer.server.services.scip_query_service import SCIPQueryService

        backend = _build_registry(tmp_path)
        stale_index = tmp_path / "stale-scip"
        stale_index.mkdir()
        current_target = tmp_path / "current-scip"
        current_target.mkdir()
        backend.register_repo("myrepo-global", "myrepo", None, str(stale_index))

        golden_repos_dir = tmp_path / "golden-repos"
        aliases_dir = golden_repos_dir / "aliases"
        aliases_dir.mkdir(parents=True)
        AliasManager(str(aliases_dir)).create_alias(
            "myrepo-global", str(current_target)
        )

        _patch_app_state(monkeypatch, golden_repos_dir, backend)

        service = SCIPQueryService(golden_repos_dir=golden_repos_dir)
        result = service._resolve_alias_scip_root("myrepo")

        assert result is not None
        assert str(result) == str(current_target)

    def test_returns_none_when_neither_alias_nor_index_path_resolve(
        self, tmp_path, monkeypatch
    ):
        """When neither alias pointer nor valid index_path exist, return None."""
        from code_indexer.server.services.scip_query_service import SCIPQueryService

        backend = _build_registry(tmp_path)
        missing_path = tmp_path / "does-not-exist"
        backend.register_repo("myrepo-global", "myrepo", None, str(missing_path))

        golden_repos_dir = tmp_path / "golden-repos"
        (golden_repos_dir / "aliases").mkdir(parents=True)

        _patch_app_state(monkeypatch, golden_repos_dir, backend)

        service = SCIPQueryService(golden_repos_dir=golden_repos_dir)
        result = service._resolve_alias_scip_root("myrepo")

        assert result is None

    def test_bare_fallback_uses_bare_repo_entry_not_global(self, tmp_path, monkeypatch):
        """Regression for the repo_entry reuse bug: when -global has a STALE
        index_path (non-existent directory) and bare has a valid index_path,
        the bare fallback must use the bare entry's index_path -- not the
        -global entry's stale one.

        Before the fix, a single scip_repo_entry was looked up once
        (preferring -global).  When -global existed, it was used for BOTH
        resolve calls.  The second call (bare alias) would try the -global
        entry's stale index_path and return None, even though the bare
        entry had a perfectly valid index_path.
        """
        from code_indexer.server.services.scip_query_service import SCIPQueryService

        backend = _build_registry(tmp_path)
        # -global entry with a STALE (non-existent) index_path
        stale_global = tmp_path / "stale-global-does-not-exist"
        # bare entry with a VALID index_path
        bare_index = tmp_path / "scip-bare-index"
        bare_index.mkdir()
        backend.register_repo("myrepo-global", "myrepo", None, str(stale_global))
        backend.register_repo("myrepo", "myrepo", None, str(bare_index))

        golden_repos_dir = tmp_path / "golden-repos"
        (golden_repos_dir / "aliases").mkdir(parents=True)
        # No alias pointers -- forces fallback to index_path

        _patch_app_state(monkeypatch, golden_repos_dir, backend)

        service = SCIPQueryService(golden_repos_dir=golden_repos_dir)
        result = service._resolve_alias_scip_root("myrepo")

        # With the fix: bare_index is returned via the bare entry's index_path.
        # With the bug: None -- both calls used the -global entry's stale path.
        assert result is not None
        assert result == bare_index


# ---------------------------------------------------------------------------
# Site: _resolve_temporal_repo_path_rest
# (src/code_indexer/server/routers/inline_query.py)
# ---------------------------------------------------------------------------


class TestInlineQueryTemporalRepoPathFallback:
    """Tests for _resolve_temporal_repo_path_rest's index_path fallback."""

    def test_falls_back_to_index_path_when_alias_pointer_missing(
        self, tmp_path, monkeypatch
    ):
        from code_indexer.server.routers.inline_query import (
            _resolve_temporal_repo_path_rest,
        )
        from code_indexer.server.app import app as real_app

        backend = _build_registry(tmp_path)
        index_dir = tmp_path / "index-temporal"
        index_dir.mkdir()
        backend.register_repo("trepo-global", "trepo", None, str(index_dir))

        golden_repos_dir = tmp_path / "golden-repos"
        (golden_repos_dir / "aliases").mkdir(parents=True)

        _patch_app_state(monkeypatch, golden_repos_dir, backend)

        # Signature: (repository_alias, current_user, app, activated_repo_manager)
        resolved, err = _resolve_temporal_repo_path_rest(
            "trepo-global", None, real_app, None
        )

        assert err is None
        assert resolved == str(index_dir)

    def test_alias_pointer_present_still_wins(self, tmp_path, monkeypatch):
        from code_indexer.global_repos.alias_manager import AliasManager
        from code_indexer.server.routers.inline_query import (
            _resolve_temporal_repo_path_rest,
        )
        from code_indexer.server.app import app as real_app

        backend = _build_registry(tmp_path)
        stale_index = tmp_path / "stale-temporal"
        stale_index.mkdir()
        current_target = tmp_path / "current-temporal"
        current_target.mkdir()
        backend.register_repo("trepo-global", "trepo", None, str(stale_index))

        golden_repos_dir = tmp_path / "golden-repos"
        aliases_dir = golden_repos_dir / "aliases"
        aliases_dir.mkdir(parents=True)
        AliasManager(str(aliases_dir)).create_alias("trepo-global", str(current_target))

        _patch_app_state(monkeypatch, golden_repos_dir, backend)

        resolved, err = _resolve_temporal_repo_path_rest(
            "trepo-global", None, real_app, None
        )

        assert err is None
        assert resolved == str(current_target)


# ---------------------------------------------------------------------------
# Site: routes.py resolve pattern (wiki-toggle / query_submit)
#
# Testing the shared resolve_alias_or_index_path call pattern used by
# routes.py sites (wiki-toggle, query_submit, query_results_partial_post).
# A full functional test would require mounting the Starlette app; we
# test the resolve helper pattern in isolation since the routes.py sites
# are thin wrappers around it.
# ---------------------------------------------------------------------------


class TestRoutesResolvePattern:
    """Tests for the resolve_alias_or_index_path pattern used in routes.py."""

    def test_falls_back_to_index_path_when_alias_pointer_missing(self, tmp_path):
        from code_indexer.global_repos.alias_manager import (
            AliasManager,
            resolve_alias_or_index_path,
        )

        backend = _build_registry(tmp_path)
        index_dir = tmp_path / "wiki-index"
        index_dir.mkdir()
        backend.register_repo("wiki-repo-global", "wiki-repo", None, str(index_dir))

        aliases_dir = tmp_path / "golden-repos" / "aliases"
        aliases_dir.mkdir(parents=True)
        alias_manager = AliasManager(str(aliases_dir))

        # Simulate the routes.py pattern: look up the -global repo_entry
        wiki_repo_entry = backend.get_repo("wiki-repo-global")
        result = resolve_alias_or_index_path(
            alias_manager,
            alias_name="wiki-repo-global",
            repo_entry=wiki_repo_entry,
        )

        assert result == str(index_dir)

    def test_alias_pointer_present_still_wins(self, tmp_path):
        from code_indexer.global_repos.alias_manager import (
            AliasManager,
            resolve_alias_or_index_path,
        )

        backend = _build_registry(tmp_path)
        stale_index = tmp_path / "stale-wiki"
        stale_index.mkdir()
        current_target = tmp_path / "current-wiki"
        current_target.mkdir()
        backend.register_repo("wiki-repo-global", "wiki-repo", None, str(stale_index))

        aliases_dir = tmp_path / "golden-repos" / "aliases"
        aliases_dir.mkdir(parents=True)
        alias_manager = AliasManager(str(aliases_dir))
        alias_manager.create_alias("wiki-repo-global", str(current_target))

        wiki_repo_entry = backend.get_repo("wiki-repo-global")
        result = resolve_alias_or_index_path(
            alias_manager,
            alias_name="wiki-repo-global",
            repo_entry=wiki_repo_entry,
        )

        assert result == str(current_target)

    def test_query_submit_pattern_with_synthetic_repo_entry(self, tmp_path):
        """The query_submit route constructs repo_entry from the request data
        as {"index_path": target_repo.get("path")} rather than a registry
        lookup. Verify this pattern works with the fallback."""
        from code_indexer.global_repos.alias_manager import (
            AliasManager,
            resolve_alias_or_index_path,
        )

        index_dir = tmp_path / "query-index"
        index_dir.mkdir()

        aliases_dir = tmp_path / "golden-repos" / "aliases"
        aliases_dir.mkdir(parents=True)
        alias_manager = AliasManager(str(aliases_dir))

        # Synthetic repo_entry as routes.py builds it
        rest_repo_entry = {"index_path": str(index_dir)}
        result = resolve_alias_or_index_path(
            alias_manager,
            alias_name="some-repo-global",
            repo_entry=rest_repo_entry,
        )

        assert result == str(index_dir)
