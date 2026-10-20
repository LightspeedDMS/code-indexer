"""Phase 3: every internal-catalog door through the in-process front door.

The restricted caller (granted only ``alpha``) gets only visible catalog
content or a refusal from every door in ``tests/e2e/cidx_meta_doors.py``;
the admin control sees the hidden content.  Real server, real catalog with a
dependency map, real semantic and full-text indexes; no mocks.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.e2e.cidx_meta_doors import Caller, CatalogDoorSuite, Callers
from tests.e2e.cidx_meta_world import provision_catalog_world
from tests.e2e.helpers import require_voyage_key
from tests.e2e.server.conftest import AdminTokenProvider


@pytest.fixture(scope="module")
def catalog_callers(
    test_client: TestClient,
    test_client_data_dir: Path,
    admin_token_provider: AdminTokenProvider,
    tmp_path_factory: pytest.TempPathFactory,
) -> Callers:
    require_voyage_key()

    def _web_client() -> httpx.Client:
        # Same app, no second lifespan, its own cookie jar.
        return TestClient(test_client.app, raise_server_exceptions=False)

    world = provision_catalog_world(
        test_client,
        admin_token_provider,
        test_client_data_dir / "data" / "golden-repos" / "cidx-meta",
        tmp_path_factory.mktemp("catalog-repos"),
        _web_client,
    )
    creds = (os.environ["E2E_ADMIN_USER"], os.environ["E2E_ADMIN_PASS"])
    return Caller(world, True, *creds), Caller(world, False, *creds)


class TestCatalogDoorsInProcess(CatalogDoorSuite):
    """Phase 3 run of every catalog door."""
