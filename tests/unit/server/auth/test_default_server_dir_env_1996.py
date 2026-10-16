"""Bug #1996: the default (JSON-file) session and user managers honour
CIDX_SERVER_DATA_DIR, like the audit logger and JWT secret manager (Bug #1778).

``session_manager.py`` builds a module-level ``PasswordChangeSessionManager()``
at import, so a hard-coded ``Path.home() / ".cidx-server"`` made every process
importing the server package create (or touch) the developer's real server
home, even when the server data dir is relocated.

Real objects, real files -- no mocks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.server.auth.session_manager import PasswordChangeSessionManager
from code_indexer.server.auth.user_manager import UserManager


@pytest.fixture
def nested_server_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    server_dir = tmp_path / "missing" / "nested" / "server-data"
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(server_dir))
    return server_dir


def test_session_manager_default_follows_server_data_dir(
    nested_server_dir: Path,
) -> None:
    mgr = PasswordChangeSessionManager()

    assert mgr.session_file_path == str(nested_server_dir / "invalidated_sessions.json")
    assert nested_server_dir.is_dir()


def test_user_manager_default_follows_server_data_dir(nested_server_dir: Path) -> None:
    mgr = UserManager()

    assert mgr.users_file_path == str(nested_server_dir / "users.json")
    assert (nested_server_dir / "users.json").is_file()


def test_defaults_fall_back_to_home_when_env_unset(
    home_in_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CIDX_SERVER_DATA_DIR", raising=False)

    sessions = PasswordChangeSessionManager()
    users = UserManager()

    home_server_dir = home_in_tmp / ".cidx-server"
    assert sessions.session_file_path == str(
        home_server_dir / "invalidated_sessions.json"
    )
    assert users.users_file_path == str(home_server_dir / "users.json")
