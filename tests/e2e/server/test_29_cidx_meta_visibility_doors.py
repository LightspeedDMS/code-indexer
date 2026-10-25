"""Phase 3: every internal-catalog door through the in-process front door.

The restricted caller (granted only ``alpha``) gets only visible catalog
content or a refusal from every door in ``tests/e2e/cidx_meta_suite.py``;
the admin control sees the hidden content.  Real server, real catalog with a
dependency map, real semantic and full-text indexes.

The one substitution: while the four fixture repositories are registered,
the registration description hook (an external Claude CLI call that writes
a generated description, which the seed overwrites anyway) is skipped for
those four aliases only; every other registration keeps the real hook.  The
session-wide lifecycle stub in ``conftest.py`` already covers the lifecycle
call.  The module removes everything it created through the front doors
before later files run, whatever fails.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.e2e.cidx_meta_doors import Caller
from tests.e2e.cidx_meta_suite import (
    CatalogDoorSuite,
    Callers,
    close_catalog_doors,
    open_catalog_doors,
)
from tests.e2e.cidx_meta_world import (
    WORLD_REPOS,
    provision_catalog_world,
    sqlite_alias_lock_store,
    teardown_catalog_world,
)
from tests.e2e.helpers import require_voyage_key
from tests.e2e.server.conftest import AdminTokenProvider


@pytest.fixture(scope="module")
def catalog_callers(
    test_client: TestClient,
    test_client_data_dir: Path,
    admin_token_provider: AdminTokenProvider,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Callers]:
    require_voyage_key()
    from code_indexer.global_repos import meta_description_hook

    real_hook = meta_description_hook.on_repo_added

    def _hook_without_fixture_repos(repo_name: str, *args: Any, **kwargs: Any) -> None:
        if repo_name in WORLD_REPOS:
            return
        real_hook(repo_name, *args, **kwargs)

    def _web_client() -> httpx.Client:
        # Same app, no second lifespan, its own cookie jar.
        return TestClient(test_client.app, raise_server_exceptions=False)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            meta_description_hook, "on_repo_added", _hook_without_fixture_repos
        )
        world = provision_catalog_world(
            test_client,
            admin_token_provider,
            test_client_data_dir / "data" / "golden-repos" / "cidx-meta",
            tmp_path_factory.mktemp("catalog-repos"),
            _web_client,
            # ^ why None: the in-process server locates its locks from this process's CIDX_DATA_DIR, not test_client_data_dir
            sqlite_alias_lock_store(None),
        )
    creds = (os.environ["E2E_ADMIN_USER"], os.environ["E2E_ADMIN_PASS"])
    callers = (Caller(world, True, *creds), Caller(world, False, *creds))
    try:
        open_catalog_doors(callers)
        yield callers
    finally:
        try:
            close_catalog_doors(callers)
        finally:
            teardown_catalog_world(world)


class TestCatalogDoorsInProcess(CatalogDoorSuite):
    """Phase 3 run of every catalog door."""
