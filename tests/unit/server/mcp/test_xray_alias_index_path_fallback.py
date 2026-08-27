"""
xray handlers were remaining Bug #1315 sites: both ``xray.py::_resolve_repo_path``
and ``xray_batch.py::_resolve_repo_path`` called ``repos._resolve_golden_repo_path``,
which reads ONLY the ``AliasManager`` pointer file with NO fallback to the registry
row's own ``index_path``. So every xray tool (xray_search / xray_explore /
xray_dump_ast / xray_search_batch) returned ``repository_not_found`` for a ``-global``
repo whenever the filesystem alias pointer was missing (e.g. after a golden-repo
seeder/bulk-provisioning refresh emptied ``golden-repos/aliases/``) — even though
``search_code`` / ``regex_search`` resolved the SAME alias fine (they already use the
shared ``resolve_alias_or_index_path`` helper). Observed live on ``evolution-global``
and ``evolution-2-global``.

The fix routes both xray resolvers through the shared
``_utils._resolve_global_repo_path_or_index`` (registry ``index_path`` fallback),
matching the other read-only search paths. Mirrors
``test_evo66336_list_files_alias_index_path_fallback.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _build_registry(tmp_path):
    """Construct a real, schema-initialized GlobalReposSqliteBackend."""
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.sqlite_backends import GlobalReposSqliteBackend

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


# Both duplicated xray resolvers must behave identically — parametrize over them.
_XRAY_RESOLVERS = [
    "code_indexer.server.mcp.handlers.xray",
    "code_indexer.server.mcp.handlers.xray_batch",
]


@pytest.mark.parametrize("module_path", _XRAY_RESOLVERS)
def test_falls_back_to_index_path_when_alias_pointer_missing(
    tmp_path, monkeypatch, module_path
):
    import importlib

    resolver = importlib.import_module(module_path)._resolve_repo_path

    backend = _build_registry(tmp_path)
    index_dir = tmp_path / "index-evo"
    index_dir.mkdir()
    backend.register_repo("evolution-global", "evolution", None, str(index_dir))

    golden_repos_dir = tmp_path / "golden-repos"
    (golden_repos_dir / "aliases").mkdir(parents=True)
    # Deliberately NO alias pointer for evolution-global (the seeder gap).

    _patch_app_state(monkeypatch, golden_repos_dir, backend)

    # Before the fix this returned None → repository_not_found.
    assert resolver("evolution-global") == str(index_dir)


@pytest.mark.parametrize("module_path", _XRAY_RESOLVERS)
def test_returns_none_when_neither_alias_nor_valid_index_path(
    tmp_path, monkeypatch, module_path
):
    import importlib

    resolver = importlib.import_module(module_path)._resolve_repo_path

    backend = _build_registry(tmp_path)
    missing_index_path = tmp_path / "does-not-exist"
    backend.register_repo("gone-global", "gone", None, str(missing_index_path))

    golden_repos_dir = tmp_path / "golden-repos"
    (golden_repos_dir / "aliases").mkdir(parents=True)

    _patch_app_state(monkeypatch, golden_repos_dir, backend)

    assert resolver("gone-global") is None


@pytest.mark.parametrize("module_path", _XRAY_RESOLVERS)
def test_alias_pointer_present_still_wins_over_index_path(
    tmp_path, monkeypatch, module_path
):
    import importlib

    from code_indexer.global_repos.alias_manager import AliasManager

    resolver = importlib.import_module(module_path)._resolve_repo_path

    backend = _build_registry(tmp_path)
    stale_index_path = tmp_path / "stale-index"
    stale_index_path.mkdir()
    current_target = tmp_path / "current-target"
    current_target.mkdir()
    backend.register_repo("evo2-global", "evo2", None, str(stale_index_path))

    golden_repos_dir = tmp_path / "golden-repos"
    aliases_dir = golden_repos_dir / "aliases"
    aliases_dir.mkdir(parents=True)
    AliasManager(str(aliases_dir)).create_alias("evo2-global", str(current_target))

    _patch_app_state(monkeypatch, golden_repos_dir, backend)

    # The (current) alias pointer must win over the (possibly-stale) registry index_path.
    assert resolver("evo2-global") == str(current_target)
