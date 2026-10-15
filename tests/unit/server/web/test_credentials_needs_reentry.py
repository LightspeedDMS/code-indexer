"""The Web credential pages show stored credentials that no key decrypts as
needing re-entry, never their stored value."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader

from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.services.token_encryption import (
    derive_key_from_salt,
    encrypt_token,
)
from tests.unit.server._isolated_app import isolated_app

PASSWORD = "Example-Config-Admin-Passw0rd!"
_CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')
_TEMPLATES = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "code_indexer"
    / "server"
    / "web"
    / "templates"
)


@pytest.mark.timeout(180)
def test_config_page_marks_undecryptable_ci_token(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    root = tmp_path_factory.mktemp("ci-token-needs-reentry")
    foreign = encrypt_token(
        "glpat-" + "e" * 24, derive_key_from_salt("another-node.example")
    )
    with isolated_app(root) as app:
        from code_indexer.server.web.routes import _get_token_manager

        backend = _get_token_manager()._sqlite_backend
        assert backend is not None
        backend.save_token("gitlab", foreign, None)
        name = f"admin-{uuid.uuid4().hex[:8]}"
        app.state.user_manager.create_user(name, PASSWORD, UserRole.ADMIN)
        client = TestClient(app, follow_redirects=False)
        match = _CSRF.search(client.get("/login").text)
        assert match
        login = client.post(
            "/login",
            data={"username": name, "password": PASSWORD, "csrf_token": match.group(1)},
        )
        assert login.status_code == 303
        html = client.get("/admin/config").text
    assert "Needs re-entry" in html
    assert foreign not in html


def test_owner_partial_names_the_node_that_could_not_decrypt() -> None:
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)
    html = env.get_template("partials/git_credentials_list.html").render(
        git_credentials=[
            {
                "credential_id": "cred-1",
                "forge_type": "github",
                "forge_host": "git.example.com",
                "token_suffix": "????",
                "needs_reentry": True,
                "needs_reentry_node": "node-a",
            }
        ]
    )
    assert "needs re-entry (node node-a)" in html


def test_git_credentials_partial_marks_needs_reentry() -> None:
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=True)
    html = env.get_template("partials/git_credentials_list.html").render(
        git_credentials=[
            {
                "credential_id": "cred-1",
                "forge_type": "github",
                "forge_host": "git.example.com",
                "token_suffix": "????",
                "needs_reentry": True,
            },
            {
                "credential_id": "cred-2",
                "forge_type": "gitlab",
                "forge_host": "gitlab.example.com",
                "token_suffix": "ab12",
                "needs_reentry": False,
            },
        ]
    )
    assert html.count("needs re-entry") == 1
    assert "****ab12" in html
    assert "****????" not in html
