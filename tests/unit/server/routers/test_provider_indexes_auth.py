"""Tests verifying provider_indexes router requires admin authentication.

Story #490: All provider index management endpoints must require admin auth.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.routers.provider_indexes import router


@pytest.fixture
def app_with_router():
    """Create a minimal FastAPI app with the provider_indexes router."""
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client(app_with_router):
    return TestClient(app_with_router, raise_server_exceptions=False)


class TestProviderIndexesAuthRequired:
    """All provider index endpoints must require admin authentication."""

    def test_list_providers_requires_auth(self, client):
        """GET /api/admin/provider-indexes/providers returns 401/403 without auth."""
        response = client.get("/api/admin/provider-indexes/providers")
        assert response.status_code in (401, 403)

    def test_get_status_requires_auth(self, client):
        """GET /api/admin/provider-indexes/status returns 401/403 without auth."""
        response = client.get("/api/admin/provider-indexes/status?alias=test")
        assert response.status_code in (401, 403)

    def test_add_requires_auth(self, client):
        """POST /api/admin/provider-indexes/add returns 401/403 without auth."""
        response = client.post(
            "/api/admin/provider-indexes/add",
            json={"provider": "voyage-ai", "alias": "test"},
        )
        assert response.status_code in (401, 403)

    def test_recreate_requires_auth(self, client):
        """POST /api/admin/provider-indexes/recreate returns 401/403 without auth."""
        response = client.post(
            "/api/admin/provider-indexes/recreate",
            json={"provider": "voyage-ai", "alias": "test"},
        )
        assert response.status_code in (401, 403)

    def test_remove_requires_auth(self, client):
        """POST /api/admin/provider-indexes/remove returns 401/403 without auth."""
        response = client.post(
            "/api/admin/provider-indexes/remove",
            json={"provider": "voyage-ai", "alias": "test"},
        )
        assert response.status_code in (401, 403)

    def test_bulk_add_requires_auth(self, client):
        """POST /api/admin/provider-indexes/bulk-add returns 401/403 without auth."""
        response = client.post(
            "/api/admin/provider-indexes/bulk-add",
            json={"provider": "voyage-ai"},
        )
        assert response.status_code in (401, 403)
