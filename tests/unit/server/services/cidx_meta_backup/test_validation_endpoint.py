"""Unit tests for Story #926 backup config validation endpoint."""

from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient


_ELEVATION_QUALNAME = "require_elevation.<locals>._check"


def _build_client(monkeypatch, config_service, ssh_key_manager, bootstrap):
    from fastapi.routing import APIRoute

    from code_indexer.server.web import routes

    app = FastAPI()
    app.include_router(routes.web_router, prefix="/admin")

    # Bypass require_elevation() dependencies so tests don't need TOTP setup.
    for route in routes.web_router.routes:
        if not isinstance(route, APIRoute):
            continue
        for dep in route.dependencies or []:
            dep_callable = getattr(dep, "dependency", None)
            if (
                dep_callable
                and getattr(dep_callable, "__qualname__", "") == _ELEVATION_QUALNAME
            ):
                app.dependency_overrides[dep_callable] = lambda: None

    monkeypatch.setattr(routes, "_require_admin_session", lambda request: object())
    monkeypatch.setattr(
        routes, "validate_login_csrf_token", lambda request, token: True
    )
    monkeypatch.setattr(
        routes,
        "_create_config_page_response",
        lambda request,
        session,
        success_message=None,
        error_message=None,
        validation_errors=None: HTMLResponse(
            content=success_message or error_message or ""
        ),
    )
    monkeypatch.setattr(routes, "_get_ssh_key_manager", lambda: ssh_key_manager)
    monkeypatch.setattr(
        routes, "get_config_service", lambda: config_service, raising=False
    )
    monkeypatch.setattr(
        "code_indexer.server.services.config_service.get_config_service",
        lambda: config_service,
    )
    monkeypatch.setattr(
        "code_indexer.server.services.cidx_meta_backup.bootstrap.CidxMetaBackupBootstrap",
        lambda: bootstrap,
    )
    return TestClient(app)


def test_ssh_url_with_known_host_passes(tmp_path, monkeypatch):
    """# Story #926 AC1: git@ SSH URL passes when SSHKeyManager knows the hostname."""
    from code_indexer.server.services.config_service import ConfigService
    from code_indexer.server.services.ssh_key_manager import KeyListResult, KeyMetadata

    config_service = ConfigService(server_dir_path=str(tmp_path / "server"))
    ssh_key_manager = MagicMock()
    ssh_key_manager.list_keys.return_value = KeyListResult(
        managed=[
            KeyMetadata(
                name="github",
                fingerprint="fp",
                key_type="ed25519",
                private_path="/tmp/id",
                public_path="/tmp/id.pub",
                hosts=["github.com"],
            )
        ]
    )
    bootstrap = MagicMock()
    client = _build_client(monkeypatch, config_service, ssh_key_manager, bootstrap)

    response = client.post(
        "/admin/config/cidx_meta_backup",
        data={"enabled": "true", "remote_url": "git@github.com:org/repo.git"},
    )

    assert response.status_code == 200
    bootstrap.bootstrap.assert_called_once()


def test_ssh_url_with_unknown_host_fails(tmp_path, monkeypatch):
    """# Story #926 AC1: SSH URL is rejected when no configured SSH key owns that host."""
    from code_indexer.server.services.config_service import ConfigService
    from code_indexer.server.services.ssh_key_manager import KeyListResult

    config_service = ConfigService(server_dir_path=str(tmp_path / "server"))
    ssh_key_manager = MagicMock()
    ssh_key_manager.list_keys.return_value = KeyListResult(managed=[])
    bootstrap = MagicMock()
    client = _build_client(monkeypatch, config_service, ssh_key_manager, bootstrap)

    response = client.post(
        "/admin/config/cidx_meta_backup",
        data={"enabled": "true", "remote_url": "git@gitlab.example.com:org/repo.git"},
    )

    assert response.status_code == 200
    assert "No SSH key configured for gitlab.example.com" in response.text
    bootstrap.bootstrap.assert_not_called()


def test_file_url_skips_ssh_check(tmp_path, monkeypatch):
    """# Story #926 AC1: file:// URLs bypass SSH host validation."""
    from code_indexer.server.services.config_service import ConfigService

    config_service = ConfigService(server_dir_path=str(tmp_path / "server"))
    ssh_key_manager = MagicMock()
    bootstrap = MagicMock()
    client = _build_client(monkeypatch, config_service, ssh_key_manager, bootstrap)

    response = client.post(
        "/admin/config/cidx_meta_backup",
        data={"enabled": "true", "remote_url": "file:///tmp/bare.git"},
    )

    assert response.status_code == 200
    ssh_key_manager.list_keys.assert_not_called()
    bootstrap.bootstrap.assert_called_once()


def test_https_url_skips_ssh_check(tmp_path, monkeypatch):
    """# Story #926 AC1: https:// URLs bypass SSH host validation."""
    from code_indexer.server.services.config_service import ConfigService

    config_service = ConfigService(server_dir_path=str(tmp_path / "server"))
    ssh_key_manager = MagicMock()
    bootstrap = MagicMock()
    client = _build_client(monkeypatch, config_service, ssh_key_manager, bootstrap)

    response = client.post(
        "/admin/config/cidx_meta_backup",
        data={"enabled": "true", "remote_url": "https://github.com/org/repo.git"},
    )

    assert response.status_code == 200
    ssh_key_manager.list_keys.assert_not_called()
    bootstrap.bootstrap.assert_called_once()
