"""
EVO-66336: ``files.py::_resolve_global_repo_target`` was the 6th Bug #1315 site
that still called ``AliasManager.read_alias()`` directly with NO fallback to the
registry row's own ``index_path`` -- so ``list_files`` / ``browse_directory``
returned ``"Alias for '<x>' not found"`` for ``-global`` repos whenever the
filesystem alias pointer file was missing (e.g. after a golden-repo-seeder
refresh emptied ``golden-repos/aliases/``), even though ``search_code`` / scip /
``repository_status`` resolved the same alias fine (they already use the shared
``resolve_alias_or_index_path`` helper).

TDD: written FIRST -- ``test_falls_back_to_index_path_when_alias_pointer_missing``
must FAIL (RED) against the direct ``read_alias()`` call and PASS (GREEN) once the
site switches to ``resolve_alias_or_index_path``. Mirrors
``test_bug1315_remaining_sites_index_path_fallback.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from code_indexer.server.auth.user_manager import User


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


class TestListFilesResolverIndexPathFallback:
    """Site 6: ``server/mcp/handlers/files.py::_resolve_global_repo_target``."""

    def test_falls_back_to_index_path_when_alias_pointer_missing(
        self, tmp_path, monkeypatch
    ):
        from code_indexer.server.mcp.handlers.files import (
            _resolve_global_repo_target,
        )

        backend = _build_registry(tmp_path)
        index_dir = tmp_path / "index-b"
        index_dir.mkdir()
        backend.register_repo("repo-b-global", "repo-b", None, str(index_dir))

        golden_repos_dir = tmp_path / "golden-repos"
        (golden_repos_dir / "aliases").mkdir(parents=True)
        # Deliberately no alias pointer for repo-b-global.

        _patch_app_state(monkeypatch, golden_repos_dir, backend)

        target_path, error = _resolve_global_repo_target(
            "repo-b-global", Mock(spec=User)
        )

        assert error is None
        assert target_path == str(index_dir)

    def test_still_errors_when_neither_alias_nor_valid_index_path_resolve(
        self, tmp_path, monkeypatch
    ):
        from code_indexer.server.mcp.handlers.files import (
            _resolve_global_repo_target,
        )

        backend = _build_registry(tmp_path)
        missing_index_path = tmp_path / "does-not-exist-c"
        backend.register_repo("repo-c-global", "repo-c", None, str(missing_index_path))

        golden_repos_dir = tmp_path / "golden-repos"
        (golden_repos_dir / "aliases").mkdir(parents=True)

        _patch_app_state(monkeypatch, golden_repos_dir, backend)

        target_path, error = _resolve_global_repo_target(
            "repo-c-global", Mock(spec=User)
        )

        assert target_path is None
        assert error is not None  # mcp error-response envelope

    def test_alias_pointer_present_still_wins_over_index_path(
        self, tmp_path, monkeypatch
    ):
        from code_indexer.global_repos.alias_manager import AliasManager
        from code_indexer.server.mcp.handlers.files import (
            _resolve_global_repo_target,
        )

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

        target_path, error = _resolve_global_repo_target(
            "repo-a-global", Mock(spec=User)
        )

        assert error is None
        assert target_path == str(current_target)
