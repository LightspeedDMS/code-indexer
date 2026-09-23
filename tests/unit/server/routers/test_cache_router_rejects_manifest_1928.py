"""Bug #1928 round 3 (P3 -- Opus): REST GET /cache/{handle} must reject a
handle carrying the pages-v1 discriminator prefix with a clear,
structured error pointing to cidx_fetch_cached_payload -- NOT return the
raw manifest JSON as if it were ordinary paginated content (this route's
page-indexing convention is 0-indexed, different from
cidx_fetch_cached_payload's 1-indexed contract).

Bug #1928 (Codex, post-final-round): the route DECLARES its 400/404
error responses as `CacheErrorResponse` (a flat {error, message, handle}
object), but `HTTPException(..., detail={...})` actually produces
`{"detail": {error, message, handle}}` -- a mismatch between the
documented OpenAPI schema and the real wire response. Tests here go
through a REAL TestClient hitting the mounted router (not calling the
handler function directly and inspecting the raised exception's
.detail), so they prove what a REAL CLIENT actually receives on the
wire matches the declared schema, field for field, with no extras.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.cache.payload_cache import CacheNotFoundError
from code_indexer.server.mcp.handlers import xray_truncation as xt
from code_indexer.server.routers.cache import router as cache_router


def _make_client(payload_cache: object) -> TestClient:
    app = FastAPI()
    app.include_router(cache_router)
    app.state.payload_cache = payload_cache
    return TestClient(app)


class TestCacheRouterRejectsManifestHandle:
    def test_pages_v1_handle_returns_structured_400_matching_declared_schema(
        self,
    ) -> None:
        mock_cache = MagicMock()
        client = _make_client(mock_cache)
        manifest_handle = f"{xt._PAGES_V1_HANDLE_PREFIX}some-handle"

        response = client.get(f"/cache/{manifest_handle}")

        assert response.status_code == 400
        body = response.json()
        assert body == {
            "error": "wrong_tool_for_handle",
            "message": (
                "This handle is an xray truncated-result manifest -- "
                "fetch it via the `cidx_fetch_cached_payload` MCP tool "
                "(1-indexed page parameter), not this REST endpoint."
            ),
            "handle": manifest_handle,
        }, f"body must exactly match CacheErrorResponse, no detail wrapper: {body}"
        mock_cache.retrieve.assert_not_called()


class TestCacheRouterExpiredHandleMatchesDeclaredSchema:
    def test_cache_expired_returns_structured_404_matching_declared_schema(
        self,
    ) -> None:
        mock_cache = MagicMock()
        mock_cache.retrieve.side_effect = CacheNotFoundError("handle expired")
        client = _make_client(mock_cache)

        response = client.get("/cache/some-legacy-handle")

        assert response.status_code == 404
        body = response.json()
        assert body == {
            "error": "cache_expired",
            "message": "Cache handle has expired or does not exist",
            "handle": "some-legacy-handle",
        }, f"body must exactly match CacheErrorResponse, no detail wrapper: {body}"
