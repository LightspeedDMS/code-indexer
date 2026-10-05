"""A throwaway real app (``create_app``) over an isolated server home that
leaves the process exactly as it found it.

``create_app()`` binds process-wide state as a side effect -- correct for
production (one app per process), a hazard for a test that builds its own
app: the next test file's shared app would mint tokens with its own JWT
manager while ``auth.dependencies`` validates with the throwaway one (401s).
Every binding below is recorded on the patcher BEFORE ``create_app()`` runs,
so ``undo()`` restores it together with the environment.  The e2e suite
guards the same bindings against its shared session app
(tests/e2e/server/conftest.py, ``_GUARDED_AUTH_DEPENDENCY_ATTRS`` /
``_create_app_bindings``).
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

_ABSENT = object()

# startup/app_wiring.py "Set global dependencies".
_AUTH_DEPENDENCY_ATTRS = (
    "jwt_manager",
    "user_manager",
    "oauth_manager",
    "mcp_credential_manager",
    "server_config",
    "api_key_manager",
)

# server/app.py create_app(): the managers it assigns via ``global``.
_APP_MODULE_ATTRS = (
    "jwt_manager",
    "user_manager",
    "refresh_token_manager",
    "golden_repo_manager",
    "background_job_manager",
    "job_tracker",
    "activated_repo_manager",
    "repository_listing_manager",
    "semantic_query_manager",
    "workspace_cleanup_service",
    "_server_hnsw_cache",
    "_server_fts_cache",
)


def _record(patcher: pytest.MonkeyPatch, owner: Any, name: str) -> None:
    """Make ``patcher.undo()`` put *owner.name* back as it is now.

    ``vars()`` reads the bound value without triggering server.app's PEP 562
    lazy construction; an absent name gets a placeholder that undo removes.
    """
    current = vars(owner).get(name, _ABSENT)
    patcher.setattr(owner, name, None if current is _ABSENT else current, False)


def _record_create_app_bindings(patcher: pytest.MonkeyPatch) -> None:
    from code_indexer.server import app as app_module
    from code_indexer.server.auth import dependencies
    from code_indexer.server.web import auth as web_auth
    from code_indexer.server.wiki.wiki_cache_invalidator import (
        wiki_cache_invalidator,
    )

    for name in _AUTH_DEPENDENCY_ATTRS:
        _record(patcher, dependencies, name)
    for name in _APP_MODULE_ATTRS:
        _record(patcher, app_module, name)
    # routers/inline_routes.py init_session_manager(...)
    _record(patcher, web_auth, "_session_manager")
    # startup/app_wiring.py wiki_cache_invalidator.set_wiki_cache(...)
    _record(patcher, wiki_cache_invalidator, "wiki_cache")


@contextmanager
def isolated_app(root: Path) -> Iterator[Any]:
    """Yield a real ``create_app()`` app whose server home is under *root*
    (never ~/.cidx-server); on exit restore the environment and every
    process-wide binding the app installed."""
    patcher = pytest.MonkeyPatch()
    patcher.setenv("HOME", str(root / "home"))
    patcher.setenv("CIDX_SERVER_DATA_DIR", str(root / "server"))
    try:
        _record_create_app_bindings(patcher)
        from code_indexer.server.app import create_app

        app = create_app()
        store = app.state.user_manager._sqlite_backend._conn_manager.db_path
        assert str(store).startswith(str(root)), store
        yield app
    finally:
        patcher.undo()
