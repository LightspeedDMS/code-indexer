"""
Unit tests for the memories REST router (Story #877).

Endpoints under test:
  POST   /api/v1/memories          — create memory
  PUT    /api/v1/memories/{id}     — edit memory (If-Match required)
  DELETE /api/v1/memories/{id}     — delete memory (If-Match required)
"""

from typing import Iterator, Optional
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cidx_memory.server.routes import ROUTER_PREFIX, current_principal, router
from cidx_sdk import Principal
from cidx_memory.server.memory_schema import MemorySchemaValidationError
from cidx_memory.server.memory_store_service import (
    ConflictError,
    MemoryStoreService,
    NotFoundError,
    RateLimitError,
    StaleContentError,
)
from tests.unit.server._memory_feature import memory_context_installed


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_VALID_PAYLOAD = {
    "type": "architectural-fact",
    "scope": "global",
    "scope_target": None,
    "referenced_repo": None,
    "summary": "Test summary",
    "evidence": [{"file": "src/foo.py", "lines": "1-10"}],
    "body": "",
}

_WRITE_RESPONSE = {
    "id": "abc123",
    "content_hash": "deadbeef",
    "path": "/memories/abc123.md",
}


def _build_test_app() -> FastAPI:
    """Factory: a minimal FastAPI app with the memories router mounted where
    the core mounts it, the caller a fixed alice principal (the core's
    authentication is not under test here)."""
    test_app = FastAPI()
    test_app.include_router(router, prefix=ROUTER_PREFIX)
    test_app.dependency_overrides[current_principal] = lambda: Principal(
        username="alice", role="normal_user"
    )
    return test_app


def _client(service: Optional[object]) -> Iterator[TestClient]:
    """A client of the app while the memory feature runs with *service* as
    its store (None: the store is not running)."""
    with memory_context_installed(store=service):
        yield TestClient(_build_test_app(), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_service():
    """MagicMock spec'd to MemoryStoreService."""
    return MagicMock(spec=MemoryStoreService)


@pytest.fixture()
def client(mock_service):
    yield from _client(mock_service)


@pytest.fixture()
def client_no_service():
    yield from _client(None)


# ---------------------------------------------------------------------------
# 1. POST /api/v1/memories — create
# ---------------------------------------------------------------------------


def test_create_memory_success_returns_201(client, mock_service):
    mock_service.create_memory.return_value = _WRITE_RESPONSE

    resp = client.post("/api/v1/memories", json=_VALID_PAYLOAD)

    assert resp.status_code == 201
    data = resp.json()
    assert data["id"] == "abc123"
    assert data["content_hash"] == "deadbeef"
    assert data["path"] == "/memories/abc123.md"
    mock_service.create_memory.assert_called_once()
    call_kwargs = mock_service.create_memory.call_args
    assert call_kwargs.kwargs["username"] == "alice"


def test_create_memory_validation_error_returns_422(client, mock_service):
    mock_service.create_memory.side_effect = MemorySchemaValidationError(
        "summary", "summary is too long"
    )

    resp = client.post("/api/v1/memories", json=_VALID_PAYLOAD)

    assert resp.status_code == 422
    assert "summary is too long" in resp.json()["detail"]


def test_create_memory_rate_limit_returns_429(client, mock_service):
    mock_service.create_memory.side_effect = RateLimitError("rate limit exceeded")

    resp = client.post("/api/v1/memories", json=_VALID_PAYLOAD)

    assert resp.status_code == 429
    assert "rate limit exceeded" in resp.json()["detail"]


def test_create_memory_conflict_returns_423(client, mock_service):
    mock_service.create_memory.side_effect = ConflictError("memory locked")

    resp = client.post("/api/v1/memories", json=_VALID_PAYLOAD)

    assert resp.status_code == 423
    assert "memory locked" in resp.json()["detail"]


def test_create_memory_service_unavailable_returns_503(client_no_service):
    resp = client_no_service.post("/api/v1/memories", json=_VALID_PAYLOAD)

    assert resp.status_code == 503
    assert "not available" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 2. PUT /api/v1/memories/{memory_id} — edit
# ---------------------------------------------------------------------------


def test_edit_memory_missing_if_match_returns_428(client, mock_service):
    resp = client.put("/api/v1/memories/abc123", json=_VALID_PAYLOAD)

    assert resp.status_code == 428
    assert "If-Match" in resp.json()["detail"]


def test_edit_memory_success_returns_200(client, mock_service):
    mock_service.edit_memory.return_value = _WRITE_RESPONSE

    resp = client.put(
        "/api/v1/memories/abc123",
        json=_VALID_PAYLOAD,
        headers={"If-Match": "deadbeef"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "abc123"
    assert data["content_hash"] == "deadbeef"
    mock_service.edit_memory.assert_called_once()
    call_args = mock_service.edit_memory.call_args
    assert call_args.args[0] == "abc123"
    assert call_args.kwargs["expected_content_hash"] == "deadbeef"
    assert call_args.kwargs["username"] == "alice"


def test_edit_memory_stale_content_returns_409_with_current_hash(client, mock_service):
    mock_service.edit_memory.side_effect = StaleContentError("newhash", "hash mismatch")

    resp = client.put(
        "/api/v1/memories/abc123",
        json=_VALID_PAYLOAD,
        headers={"If-Match": "oldhash"},
    )

    assert resp.status_code == 409
    data = resp.json()
    assert "hash mismatch" in data["detail"]
    assert data["current_content_hash"] == "newhash"


def test_edit_memory_not_found_returns_404(client, mock_service):
    mock_service.edit_memory.side_effect = NotFoundError("memory not found")

    resp = client.put(
        "/api/v1/memories/abc123",
        json=_VALID_PAYLOAD,
        headers={"If-Match": "deadbeef"},
    )

    assert resp.status_code == 404
    assert "memory not found" in resp.json()["detail"]


def test_edit_memory_conflict_returns_423(client, mock_service):
    mock_service.edit_memory.side_effect = ConflictError("memory locked")

    resp = client.put(
        "/api/v1/memories/abc123",
        json=_VALID_PAYLOAD,
        headers={"If-Match": "deadbeef"},
    )

    assert resp.status_code == 423
    assert "memory locked" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# 3. DELETE /api/v1/memories/{memory_id} — delete
# ---------------------------------------------------------------------------


def test_delete_memory_missing_if_match_returns_428(client, mock_service):
    resp = client.delete("/api/v1/memories/abc123")

    assert resp.status_code == 428
    assert "If-Match" in resp.json()["detail"]


def test_delete_memory_success_returns_204(client, mock_service):
    mock_service.delete_memory.return_value = None

    resp = client.delete(
        "/api/v1/memories/abc123",
        headers={"If-Match": "deadbeef"},
    )

    assert resp.status_code == 204
    assert resp.content == b""
    mock_service.delete_memory.assert_called_once()
    call_args = mock_service.delete_memory.call_args
    assert call_args.args[0] == "abc123"
    assert call_args.kwargs["expected_content_hash"] == "deadbeef"
    assert call_args.kwargs["username"] == "alice"


def test_delete_memory_stale_content_returns_409(client, mock_service):
    mock_service.delete_memory.side_effect = StaleContentError(
        "newhash", "hash mismatch on delete"
    )

    resp = client.delete(
        "/api/v1/memories/abc123",
        headers={"If-Match": "oldhash"},
    )

    assert resp.status_code == 409
    data = resp.json()
    assert "hash mismatch on delete" in data["detail"]
    assert data["current_content_hash"] == "newhash"


def test_delete_memory_not_found_returns_404(client, mock_service):
    mock_service.delete_memory.side_effect = NotFoundError("memory not found")

    resp = client.delete(
        "/api/v1/memories/abc123",
        headers={"If-Match": "deadbeef"},
    )

    assert resp.status_code == 404
    assert "memory not found" in resp.json()["detail"]
