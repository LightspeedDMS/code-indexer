"""Phase 4: every internal-catalog door over the live uvicorn server.

Same suite as Phase 3 (``tests/e2e/cidx_meta_suite.py``), driven over real
HTTP: the restricted caller (granted only ``alpha``) sees only visible
catalog content or a refusal; the admin control sees the hidden content.
"""

from __future__ import annotations

from typing import Iterator

import httpx
import pytest

from tests.e2e.cidx_meta_doors import Caller
from tests.e2e.cidx_meta_suite import CatalogDoorSuite, Callers, open_catalog_doors
from tests.e2e.cidx_meta_world import provision_catalog_world
from tests.e2e.conftest import E2EConfig
from tests.e2e.helpers import require_voyage_key
from tests.e2e.server.conftest import AdminTokenProvider

CLIENT_TIMEOUT_SECONDS = 180.0


@pytest.fixture(scope="module")
def catalog_callers(
    e2e_config: E2EConfig,
    e2e_admin_token_provider: AdminTokenProvider,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Callers]:
    """(restricted caller, admin caller) on the live server."""
    require_voyage_key()
    base = e2e_config.server_url

    def _web_client() -> httpx.Client:
        return httpx.Client(base_url=base, timeout=CLIENT_TIMEOUT_SECONDS)

    with httpx.Client(base_url=base, timeout=CLIENT_TIMEOUT_SECONDS) as client:
        world = provision_catalog_world(
            client,
            e2e_admin_token_provider,
            e2e_config.server_data_dir / "data" / "golden-repos" / "cidx-meta",
            tmp_path_factory.mktemp("catalog-repos"),
            _web_client,
        )
        user, password = e2e_config.admin_user, e2e_config.admin_pass
        callers = (
            Caller(
                world=world, restricted=True, admin_user=user, admin_password=password
            ),
            Caller(
                world=world, restricted=False, admin_user=user, admin_password=password
            ),
        )
        open_catalog_doors(callers)
        yield callers


class TestCatalogDoorsLiveServer(CatalogDoorSuite):
    """Phase 4 run of every catalog door."""
