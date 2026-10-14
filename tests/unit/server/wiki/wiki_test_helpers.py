"""Shared test helpers for wiki unit tests."""

import json
import shutil
import tempfile
import weakref
from pathlib import Path
from typing import Any

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


def app_golden_repos_dir(app: Any, suffix: str = "-golden-repos") -> Path:
    """A fresh golden-repos directory (with ``aliases/``) owned by ``app``.

    Fresh per app: a fixed "<repo>.parent / golden-repos-test" was
    /tmp/golden-repos-test for every TemporaryDirectory repo -- shared by
    concurrent xdist workers, whose alias files overwrote each other. Removed
    when ``app`` is garbage-collected, or at interpreter exit at the latest,
    so no run leaves directories behind.
    """
    path = Path(tempfile.mkdtemp(suffix=suffix))
    weakref.finalize(app, shutil.rmtree, str(path), True)
    (path / "aliases").mkdir()
    return path


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
