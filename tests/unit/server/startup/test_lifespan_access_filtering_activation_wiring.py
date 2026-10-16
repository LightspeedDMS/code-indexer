"""The access filtering service built at startup can resolve the caller's
own activations (public #1984).

Runs the REAL startup path (``TestClient(create_app())`` as a context
manager) against an isolated server data dir, then asserts the service the
query front doors read from ``app.state`` holds the same ActivatedRepoManager
the routes use. Without it, rows from a custom-alias activation are dropped.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.slow
def test_startup_access_filtering_service_holds_the_activated_repo_manager(
    monkeypatch, tmp_path
) -> None:
    from code_indexer.server.app import create_app
    from code_indexer.server.services.config_service import reset_config_service

    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path))
    reset_config_service()
    try:
        app = create_app()
        with TestClient(app):
            service = app.state.access_filtering_service
            activated_repo_manager = app.state.activated_repo_manager
            assert service is not None
            assert activated_repo_manager is not None
            assert service._activated_repo_manager is activated_repo_manager
    finally:
        reset_config_service()
