"""
Unit tests for Bug #939: legacy /admin/settings/file-content-limits route removed.

AC3: The standalone page route GET /admin/settings/file-content-limits must return
404 after the route handler and template are deleted.

RED phase: Returns non-404 (redirects to login or 200) until the route is deleted.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.web.routes import web_router


def test_file_content_limits_page_returns_404():
    """GET /admin/settings/file-content-limits must return 404 after route deletion."""
    app = FastAPI()
    app.include_router(web_router, prefix="/admin")
    client = TestClient(app, follow_redirects=False)

    response = client.get("/admin/settings/file-content-limits")

    assert response.status_code == 404, (
        f"Expected 404 (route deleted), got {response.status_code}. "
        "The /admin/settings/file-content-limits route must be removed."
    )
