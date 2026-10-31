# ^ see tests/unit/server/mcp/test_mcp_write_tools_searchable_2093.py; same app.state stand-ins, solo front door (#2327)
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.services import auto_watch_manager as awm_module
from code_indexer.server.services.auto_watch_manager import AutoWatchManager
from code_indexer.server.utils import server_managed_provider_settings as managed
from code_indexer.services.local_project_init import initialize_local_project
from tests.utils.fake_voyage_provider import running_fake_voyage_server

ALIAS = "example-repo"
KEY = "sentinel-test-key-2327"
OLD = "def alpha():\n    return 'old'\n"
NEW = "def alpha():\n    return 'new'\n"
_UNSET = object()
_STATE_ATTRS = ("activated_repo_manager", "access_filtering_service", "storage_mode")


class _Registry:
    def __init__(self, repo: Path) -> None:
        self._repo = repo

    def user_has_activated_repo(self, username: str, alias: str) -> bool:
        return bool(alias == ALIAS)

    def get_activated_repo_path(self, username: str, user_alias: str) -> str:
        return str(self._repo)


class _Access:
    def is_admin_user(self, username: str) -> bool:
        return True


def _wait_until(condition: Callable[[], bool], seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.05)
    return condition()


def _user() -> User:
    return User(
        username="example-user",
        password_hash="unused",
        role=UserRole.ADMIN,
        created_at=datetime.now(),
    )


def _call(handler: Callable[..., Dict[str, Any]], **params: Any) -> Dict[str, Any]:
    response = handler(dict(repository_alias=ALIAS, **params), _user())
    data: Dict[str, Any] = json.loads(response["content"][0]["text"])
    return data


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    repo = (tmp_path / ALIAS).resolve()
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "alpha.py").write_text(OLD)
    (repo / "src" / "beta.py").write_text("BETA = 1\n")
    initialize_local_project(repo, no_override_file=True)
    with running_fake_voyage_server(KEY) as server:
        enforce = managed.enforce_server_managed_provider_settings

        def enforce_then_route_to_fake(config: Any) -> None:
            enforce(config)
            config.voyage_ai.api_endpoint = f"{server.base_url}/v1/embeddings"

        monkeypatch.setattr(
            managed,
            "enforce_server_managed_provider_settings",
            enforce_then_route_to_fake,
        )
        monkeypatch.setenv("VOYAGE_API_KEY", KEY)
        yield repo


@pytest.fixture
def wired(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[str], AutoWatchManager]]:
    from code_indexer.server import app as app_module

    state = app_module.app.state
    saved = {name: getattr(state, name, _UNSET) for name in _STATE_ATTRS}
    managers: List[AutoWatchManager] = []

    def wire(storage_mode: str) -> AutoWatchManager:
        state.activated_repo_manager = _Registry(workspace)
        state.access_filtering_service = _Access()
        state.storage_mode = storage_mode
        manager = AutoWatchManager()
        managers.append(manager)
        monkeypatch.setattr(awm_module, "auto_watch_manager", manager)
        return manager

    try:
        yield wire
    finally:
        for manager in managers:
            manager.shutdown()
        for name, value in saved.items():
            if value is _UNSET:
                if hasattr(state, name):
                    delattr(state, name)
            else:
                setattr(state, name, value)


def _guard_infos(caplog: pytest.LogCaptureFixture) -> List[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "#2327" in r.getMessage()
    ]


def test_cluster_mode_writes_succeed_without_starting_a_watch(
    wired: Callable[[str], AutoWatchManager],
    workspace: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from code_indexer.server.mcp.handlers import files

    manager = wired("postgres")
    caplog.set_level(logging.DEBUG)

    created = _call(files.handle_create_file, file_path="src/gamma.py", content="G\n")
    edited = _call(
        files.handle_edit_file,
        file_path="src/alpha.py",
        old_string=OLD,
        new_string=NEW,
        content_hash=hashlib.sha256(OLD.encode()).hexdigest(),
    )
    deleted = _call(files.handle_delete_file, file_path="src/beta.py")

    assert created.get("success") and edited.get("success") and deleted.get("success")
    assert (workspace / "src" / "gamma.py").read_text() == "G\n"
    assert (workspace / "src" / "alpha.py").read_text() == NEW
    assert not (workspace / "src" / "beta.py").exists()
    assert manager.get_all_states() == {}
    assert not manager.is_watching(str(workspace))
    assert manager._owed_writes == {}
    assert manager._timeout_thread is None
    assert len(_guard_infos(caplog)) == 1
    noisy = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert noisy == [], [r.getMessage() for r in noisy]


def test_solo_mode_write_still_starts_the_watch(
    wired: Callable[[str], AutoWatchManager],
    workspace: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from code_indexer.server.mcp.handlers import files

    manager = wired("sqlite")
    caplog.set_level(logging.INFO)

    created = _call(files.handle_create_file, file_path="src/gamma.py", content="G\n")

    assert created.get("success")
    assert _wait_until(lambda: manager.is_watching(str(workspace)))
    assert _guard_infos(caplog) == []


def test_removal_without_a_watch_is_silent_in_cluster_mode(
    wired: Callable[[str], AutoWatchManager],
    workspace: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from code_indexer.server.mcp.handlers import files

    manager = wired("postgres")
    caplog.set_level(logging.DEBUG)
    _call(files.handle_create_file, file_path="src/gamma.py", content="G\n")

    manager.stop_watch_for_removal(str(workspace))
    manager.finish_removal(str(workspace))

    assert manager.get_all_states() == {}
    noisy = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert noisy == [], [r.getMessage() for r in noisy]
