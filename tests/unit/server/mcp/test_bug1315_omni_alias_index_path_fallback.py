"""
Bug #1315: Omni cross-repo search_code (repository_alias="*") fails to resolve
the -global alias for MOST global repos on a staging cluster with many
registered repos: only repos whose on-disk AliasManager pointer JSON file
happens to exist resolve correctly; every other repo -- despite having a
perfectly valid registry row with a real, on-disk `index_path` -- fails with
"Alias for global repository '<name>-global' not found".

Root cause (confirmed empirically by the tests in this file, BEFORE any fix):
Both alias-resolution call sites --
  - MultiSearchService._get_repository_path
    (src/code_indexer/server/multi/multi_search_service.py)
  - _resolve_global_repo_target
    (src/code_indexer/server/mcp/handlers/search.py)
-- call AliasManager.read_alias(alias) and immediately raise/error out if it
returns None, with NO fallback to the registry's own `index_path` field --
even though `index_path` was written atomically at registration time and is
exactly as authoritative as the alias pointer file.

Fix: a single shared helper, `resolve_alias_or_index_path`, added to
`code_indexer.global_repos.alias_manager` (imported by both call sites
already), used by both `_get_repository_path` and `_resolve_global_repo_target`
to fall back to `repo_entry["index_path"]` (verified to exist on disk) when
the alias pointer is missing/unreadable -- logging a WARNING (Messi Rule #13,
anti-silent-failure) -- while preserving the existing hard failure when
NEITHER resolves (Messi Rule #2, anti-fallback: this is not a masking
fallback, it's using an already-trusted alternate field from the SAME
registry row).

TDD: these tests are written FIRST and must fail (RED) against the
pre-fix code, then pass (GREEN) after the fix is implemented.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import Mock

import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_user(username: str = "admin"):
    from code_indexer.server.auth.user_manager import User, UserRole

    return User(
        username=username,
        role=UserRole.ADMIN,
        password_hash="dummy",
        created_at=datetime.now(),
    )


def _make_multi_search_limits(cap: int = 1000) -> Mock:
    limits = Mock()
    limits.multi_search_max_workers = 4
    limits.multi_search_timeout_seconds = 30
    limits.omni_wildcard_expansion_cap = cap
    limits.omni_max_repos_per_search = cap
    return limits


def _make_config_service(cap: int = 1000) -> Mock:
    svc = Mock()
    cfg = Mock()
    cfg.multi_search_limits_config = _make_multi_search_limits(cap)
    svc.get_config.return_value = cfg
    return svc


def _json_result(handler_response: Dict[str, Any]) -> Dict[str, Any]:
    text = handler_response["content"][0]["text"]
    result: Dict[str, Any] = json.loads(text)
    return result


# ---------------------------------------------------------------------------
# Class 1: unit tests for the new shared helper resolve_alias_or_index_path
# ---------------------------------------------------------------------------


class TestResolveAliasOrIndexPathHelper:
    """Unit tests for the new shared helper (does not exist pre-fix -> RED)."""

    def test_returns_alias_path_when_alias_pointer_exists(self, tmp_path):
        from code_indexer.global_repos.alias_manager import (
            AliasManager,
            resolve_alias_or_index_path,
        )

        aliases_dir = tmp_path / "aliases"
        alias_manager = AliasManager(str(aliases_dir))
        target = tmp_path / "index-a"
        target.mkdir()
        alias_manager.create_alias("repo-a-global", str(target))

        result = resolve_alias_or_index_path(
            alias_manager,
            alias_name="repo-a-global",
            repo_entry={"index_path": str(tmp_path / "some-other-unused-path")},
        )

        assert result == str(target)

    def test_falls_back_to_index_path_when_alias_pointer_missing(
        self, tmp_path, caplog
    ):
        from code_indexer.global_repos.alias_manager import (
            AliasManager,
            resolve_alias_or_index_path,
        )

        aliases_dir = tmp_path / "aliases"
        alias_manager = AliasManager(str(aliases_dir))
        index_dir = tmp_path / "index-b"
        index_dir.mkdir()

        with caplog.at_level(logging.WARNING):
            result = resolve_alias_or_index_path(
                alias_manager,
                alias_name="repo-b-global",
                repo_entry={"index_path": str(index_dir)},
            )

        assert result == str(index_dir)
        assert any(
            "index_path" in record.message.lower() and "repo-b-global" in record.message
            for record in caplog.records
        ), (
            f"Expected a WARNING mentioning the index_path fallback, got: {[r.message for r in caplog.records]}"
        )

    def test_returns_none_when_neither_alias_nor_index_path_resolve(self, tmp_path):
        from code_indexer.global_repos.alias_manager import (
            AliasManager,
            resolve_alias_or_index_path,
        )

        aliases_dir = tmp_path / "aliases"
        alias_manager = AliasManager(str(aliases_dir))

        result = resolve_alias_or_index_path(
            alias_manager,
            alias_name="repo-c-global",
            repo_entry={"index_path": str(tmp_path / "does-not-exist-on-disk")},
        )

        assert result is None

    def test_returns_none_when_repo_entry_is_none(self, tmp_path):
        from code_indexer.global_repos.alias_manager import (
            AliasManager,
            resolve_alias_or_index_path,
        )

        aliases_dir = tmp_path / "aliases"
        alias_manager = AliasManager(str(aliases_dir))

        result = resolve_alias_or_index_path(
            alias_manager, alias_name="repo-d-global", repo_entry=None
        )

        assert result is None

    def test_returns_none_when_repo_entry_has_no_index_path_key(self, tmp_path):
        from code_indexer.global_repos.alias_manager import (
            AliasManager,
            resolve_alias_or_index_path,
        )

        aliases_dir = tmp_path / "aliases"
        alias_manager = AliasManager(str(aliases_dir))

        result = resolve_alias_or_index_path(
            alias_manager, alias_name="repo-e-global", repo_entry={}
        )

        assert result is None


# ---------------------------------------------------------------------------
# Class 2: MultiSearchService._get_repository_path -- real SQLite registry +
# real AliasManager (empirical repro requested by Bug #1315).
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


class TestMultiSearchServiceGetRepositoryPathIndexPathFallback:
    def _build_registry(self, tmp_path):
        return _build_registry(tmp_path)

    def test_falls_back_to_index_path_when_alias_pointer_missing(
        self, tmp_path, monkeypatch
    ):
        """The 11-repos-broken staging scenario: registry row exists with a
        real on-disk index_path, but the alias pointer JSON was never created.
        """
        from code_indexer.server.multi.multi_search_service import (
            MultiSearchService,
        )
        from code_indexer.server.multi.multi_search_config import MultiSearchConfig
        from code_indexer.server.app import app as real_app

        backend = self._build_registry(tmp_path)
        index_dir = tmp_path / "index-b"
        index_dir.mkdir()
        backend.register_repo("repo-b-global", "repo-b", None, str(index_dir))

        golden_repos_dir = tmp_path / "golden-repos"
        (golden_repos_dir / "aliases").mkdir(parents=True)
        # Deliberately do NOT create an alias pointer file for repo-b-global.

        monkeypatch.setattr(
            real_app.state, "golden_repos_dir", str(golden_repos_dir), raising=False
        )
        monkeypatch.setattr(
            real_app.state,
            "backend_registry",
            SimpleNamespace(global_repos=backend),
            raising=False,
        )

        service = MultiSearchService(
            MultiSearchConfig(max_workers=2, query_timeout_seconds=30)
        )
        result = service._get_repository_path("repo-b-global")

        assert result == str(index_dir)

    def test_still_raises_when_neither_alias_nor_valid_index_path_resolve(
        self, tmp_path, monkeypatch
    ):
        """Negative control: no alias pointer AND index_path does not exist on
        disk -- must still legitimately fail (Messi Rule #2 anti-fallback)."""
        from code_indexer.server.multi.multi_search_service import (
            MultiSearchService,
        )
        from code_indexer.server.multi.multi_search_config import MultiSearchConfig
        from code_indexer.server.app import app as real_app

        backend = self._build_registry(tmp_path)
        missing_index_path = tmp_path / "does-not-exist-c"
        backend.register_repo("repo-c-global", "repo-c", None, str(missing_index_path))

        golden_repos_dir = tmp_path / "golden-repos"
        (golden_repos_dir / "aliases").mkdir(parents=True)

        monkeypatch.setattr(
            real_app.state, "golden_repos_dir", str(golden_repos_dir), raising=False
        )
        monkeypatch.setattr(
            real_app.state,
            "backend_registry",
            SimpleNamespace(global_repos=backend),
            raising=False,
        )

        service = MultiSearchService(
            MultiSearchConfig(max_workers=2, query_timeout_seconds=30)
        )

        with pytest.raises(FileNotFoundError) as exc_info:
            service._get_repository_path("repo-c-global")

        assert "not found" in str(exc_info.value).lower()

    def test_alias_pointer_present_still_wins_over_index_path(
        self, tmp_path, monkeypatch
    ):
        """When the alias pointer DOES exist, it must still be authoritative
        (registry index_path is a fallback, not a replacement)."""
        from code_indexer.server.multi.multi_search_service import (
            MultiSearchService,
        )
        from code_indexer.server.multi.multi_search_config import MultiSearchConfig
        from code_indexer.server.app import app as real_app
        from code_indexer.global_repos.alias_manager import AliasManager

        backend = self._build_registry(tmp_path)
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

        monkeypatch.setattr(
            real_app.state, "golden_repos_dir", str(golden_repos_dir), raising=False
        )
        monkeypatch.setattr(
            real_app.state,
            "backend_registry",
            SimpleNamespace(global_repos=backend),
            raising=False,
        )

        service = MultiSearchService(
            MultiSearchConfig(max_workers=2, query_timeout_seconds=30)
        )
        result = service._get_repository_path("repo-a-global")

        assert result == str(current_target)


# ---------------------------------------------------------------------------
# Class 3: search.py::_resolve_global_repo_target -- same empirical repro for
# the DIRECT single-repo query path, to prove byte-for-byte parity with omni.
# ---------------------------------------------------------------------------


class TestResolveGlobalRepoTargetIndexPathFallback:
    def _build_registry(self, tmp_path):
        return _build_registry(tmp_path)

    def test_direct_query_falls_back_to_index_path_when_alias_pointer_missing(
        self, tmp_path, monkeypatch
    ):
        from code_indexer.server.mcp.handlers.search import (
            _resolve_global_repo_target,
        )
        from code_indexer.server.app import app as real_app

        backend = self._build_registry(tmp_path)
        index_dir = tmp_path / "index-b"
        index_dir.mkdir()
        backend.register_repo("repo-b-global", "repo-b", None, str(index_dir))

        golden_repos_dir = tmp_path / "golden-repos"
        (golden_repos_dir / "aliases").mkdir(parents=True)

        monkeypatch.setattr(
            real_app.state, "golden_repos_dir", str(golden_repos_dir), raising=False
        )
        monkeypatch.setattr(
            real_app.state,
            "backend_registry",
            SimpleNamespace(global_repos=backend),
            raising=False,
        )

        repo_entry, target_path, err = _resolve_global_repo_target(
            "repo-b-global", _make_user()
        )

        assert err is None, f"Expected success, got error: {err}"
        assert repo_entry is not None
        assert target_path == str(index_dir)

    def test_direct_query_still_errors_when_neither_resolves(
        self, tmp_path, monkeypatch
    ):
        from code_indexer.server.mcp.handlers.search import (
            _resolve_global_repo_target,
        )
        from code_indexer.server.app import app as real_app

        backend = self._build_registry(tmp_path)
        missing_index_path = tmp_path / "does-not-exist-c"
        backend.register_repo("repo-c-global", "repo-c", None, str(missing_index_path))

        golden_repos_dir = tmp_path / "golden-repos"
        (golden_repos_dir / "aliases").mkdir(parents=True)

        monkeypatch.setattr(
            real_app.state, "golden_repos_dir", str(golden_repos_dir), raising=False
        )
        monkeypatch.setattr(
            real_app.state,
            "backend_registry",
            SimpleNamespace(global_repos=backend),
            raising=False,
        )

        repo_entry, target_path, err = _resolve_global_repo_target(
            "repo-c-global", _make_user()
        )

        assert repo_entry is None
        assert target_path is None
        assert err is not None


# ---------------------------------------------------------------------------
# Class 4: end-to-end acceptance test -- search_code(repository_alias="*")
# must resolve ALL globally-active repos, not just the one with an on-disk
# alias pointer file.
# ---------------------------------------------------------------------------


class TestOmniSearchCodeAliasFallbackBug1315:
    def test_omni_star_search_resolves_all_repos_without_alias_not_found_errors(
        self, tmp_path, monkeypatch
    ):
        from code_indexer.server.mcp.handlers import search as search_module
        from code_indexer.server.mcp.handlers import _utils
        from code_indexer.server.mcp.handlers.search import search_code
        from code_indexer.server.app import app as real_app
        from code_indexer.server.multi.multi_search_service import (
            MultiSearchService,
        )
        from code_indexer.global_repos.alias_manager import AliasManager

        MultiSearchService._reset_singleton()

        backend = _build_registry(tmp_path)

        # repo-a-global: has BOTH a valid index_path AND an on-disk alias
        # pointer file (mirrors the one repo -- "click" -- that already
        # worked in the staging incident).
        idx_a = tmp_path / "index-a"
        idx_a.mkdir()
        backend.register_repo("repo-a-global", "repo-a", None, str(idx_a))

        # repo-b-global: valid index_path on disk, but NO alias pointer file
        # (mirrors the 11 broken repos in the staging incident).
        idx_b = tmp_path / "index-b"
        idx_b.mkdir()
        backend.register_repo("repo-b-global", "repo-b", None, str(idx_b))

        # repo-c-global: negative control -- no alias pointer AND index_path
        # does not exist on disk. Must still legitimately fail.
        missing_idx_c = tmp_path / "does-not-exist-c"
        backend.register_repo("repo-c-global", "repo-c", None, str(missing_idx_c))

        golden_repos_dir = tmp_path / "golden-repos"
        aliases_dir = golden_repos_dir / "aliases"
        aliases_dir.mkdir(parents=True)
        AliasManager(str(aliases_dir)).create_alias("repo-a-global", str(idx_a))
        # repo-b-global and repo-c-global: no alias pointer created on purpose.

        monkeypatch.setattr(
            real_app.state, "golden_repos_dir", str(golden_repos_dir), raising=False
        )
        monkeypatch.setattr(
            real_app.state,
            "backend_registry",
            SimpleNamespace(global_repos=backend),
            raising=False,
        )
        monkeypatch.setattr(
            _utils.app_module,
            "activated_repo_manager",
            Mock(user_has_activated_repo=Mock(return_value=False)),
            raising=False,
        )
        monkeypatch.setattr(
            _utils.app_module, "golden_repo_manager", None, raising=False
        )

        fake_config_service = _make_config_service()
        monkeypatch.setattr(
            search_module, "get_config_service", lambda: fake_config_service
        )
        monkeypatch.setattr(_utils, "get_config_service", lambda: fake_config_service)

        params = {
            "query_text": "def foo",
            "repository_alias": "*",
            "search_mode": "fts",
        }

        try:
            result = search_code(params, _make_user())
        finally:
            MultiSearchService._reset_singleton()

        inner = _json_result(result)
        assert inner.get("success") is True, f"Expected success=True, got: {inner}"

        results = inner["results"]
        errors = results.get("errors") or {}

        # Negative control must still fail with a "not found" style error.
        assert "repo-c-global" in errors, (
            f"Expected repo-c-global (no alias, no valid index_path) to still "
            f"fail. errors={errors}"
        )
        assert "not found" in errors["repo-c-global"].lower()

        # Positive: neither repo-a (has pointer) nor repo-b (missing pointer,
        # falls back to index_path) may show an alias-resolution error.
        for alias in ("repo-a-global", "repo-b-global"):
            err_text = errors.get(alias, "")
            assert "alias for global repository" not in err_text.lower(), (
                f"{alias} must not fail with an alias-not-found error "
                f"(this is the Bug #1315 regression). Got: {err_text}"
            )
            assert "not found in global repositories" not in err_text.lower(), (
                f"{alias} must not fail with a repo-not-found error. Got: {err_text}"
            )
