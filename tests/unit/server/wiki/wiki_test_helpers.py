"""Shared test helpers for wiki unit tests."""

import json
from pathlib import Path

import pytest


def make_aliases_dir(golden_repos_dir: str, alias: str, target_path: str) -> None:
    """Write a {alias}-global.json alias file inside golden_repos_dir/aliases/."""
    aliases_dir = Path(golden_repos_dir) / "aliases"
    aliases_dir.mkdir(parents=True, exist_ok=True)
    alias_file = aliases_dir / f"{alias}-global.json"
    alias_data = {
        "target_path": target_path,
        "created_at": "2026-01-01T00:00:00+00:00",
        "last_refresh": "2026-01-01T00:00:00+00:00",
        "repo_name": alias,
    }
    alias_file.write_text(json.dumps(alias_data))


def install_unauthenticated_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install what hybrid auth reads -- the process-wide session manager and
    the module-level user/jwt managers -- so an unauthenticated request is
    judged by the auth code itself, not by whether an earlier test in the run
    happened to initialise them (run alone, the route raised "Session manager
    not initialized" and answered 500)."""
    from unittest.mock import MagicMock

    from code_indexer.server.auth import dependencies as auth_deps
    from code_indexer.server.web import auth as web_auth

    web_config = MagicMock()
    web_config.host = "localhost"
    monkeypatch.setattr(
        web_auth,
        "_session_manager",
        web_auth.SessionManager("test-secret-key-for-testing", web_config),
    )
    monkeypatch.setattr(auth_deps, "user_manager", MagicMock())
    monkeypatch.setattr(auth_deps, "jwt_manager", MagicMock())
