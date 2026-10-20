"""Phase 6: internal-catalog visibility on the PostgreSQL-backed server.

Runs the full catalog suite (``tests/e2e/cidx_meta_suite.py``) against the
PG-backed uvicorn, so catalog search projection, the group store, the wiki
and the payload cache are exercised on PostgreSQL (the suite's revocation
test refuses a bound handle from the PG payload cache); adds a direct check
that the restricted caller's catalog handle is bound to its own view and
round-trips through the PostgreSQL payload cache.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator, Optional, Tuple

import httpx
import pytest

from tests.e2e.cidx_meta_doors import Caller
from tests.e2e.cidx_meta_suite import (
    CatalogDoorSuite,
    Callers,
    close_catalog_doors,
    open_catalog_doors,
    restricted_catalog_handle,
)
from tests.e2e.cidx_meta_world import (
    CATALOG_GLOBAL,
    provision_catalog_world,
    teardown_catalog_world,
)
from tests.e2e.helpers import require_voyage_key
from tests.e2e.server.conftest import AdminTokenProvider

CLIENT_TIMEOUT_SECONDS = 180.0
HANDLE_BINDING_MARK = ".cmv1."


@pytest.fixture(scope="module")
def catalog_callers(
    pg_server_url: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Callers]:
    """(restricted caller, admin caller) on the PG-backed server."""
    require_voyage_key()
    data_dir = os.environ.get("E2E_PG_SERVER_DATA_DIR", "")
    if not data_dir:
        pytest.skip("E2E_PG_SERVER_DATA_DIR not set")
    admin_user = os.environ["E2E_ADMIN_USER"]
    admin_pass = os.environ["E2E_ADMIN_PASS"]

    with httpx.Client(base_url=pg_server_url, timeout=CLIENT_TIMEOUT_SECONDS) as client:

        def _login() -> Tuple[str, Optional[str]]:
            resp = client.post(
                "/auth/login", json={"username": admin_user, "password": admin_pass}
            )
            resp.raise_for_status()
            return str(resp.json()["access_token"]), resp.json().get("refresh_token")

        access, refresh = _login()
        admin_tokens = AdminTokenProvider(
            login_fn=_login, initial_access_token=access, initial_refresh_token=refresh
        )
        world = provision_catalog_world(
            client,
            admin_tokens,
            Path(data_dir) / "data" / "golden-repos" / "cidx-meta",
            tmp_path_factory.mktemp("pg-catalog-repos"),
            lambda: httpx.Client(
                base_url=pg_server_url, timeout=CLIENT_TIMEOUT_SECONDS
            ),
        )
        callers = (
            Caller(
                world=world,
                restricted=True,
                admin_user=admin_user,
                admin_password=admin_pass,
            ),
            Caller(
                world=world,
                restricted=False,
                admin_user=admin_user,
                admin_password=admin_pass,
            ),
        )
        try:
            open_catalog_doors(callers)
            yield callers
        finally:
            try:
                close_catalog_doors(callers)
            finally:
                teardown_catalog_world(world)


class TestCatalogDoorsPostgres(CatalogDoorSuite):
    """Phase 6 run of every catalog door on the PostgreSQL backends."""

    def test_restricted_catalog_handle_is_bound_and_round_trips_on_postgres(
        self, catalog_callers: Callers
    ) -> None:
        restricted_caller, admin_caller = catalog_callers
        assert restricted_caller.restricted and not admin_caller.restricted
        restricted_handle = restricted_catalog_handle(restricted_caller)
        admin_handle = restricted_catalog_handle(admin_caller)
        assert HANDLE_BINDING_MARK in restricted_handle
        assert HANDLE_BINDING_MARK in admin_handle
        assert restricted_handle.rsplit(".", 1)[1] != admin_handle.rsplit(".", 1)[1]
        page = restricted_caller.mcp(
            "get_cached_content", {"handle": restricted_handle}
        )
        assert page.ok and "evidence files" in page.text, page.snippet()
        assert CATALOG_GLOBAL not in restricted_handle
