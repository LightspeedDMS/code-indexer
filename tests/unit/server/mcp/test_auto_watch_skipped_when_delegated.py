"""File CRUD tools never probe or watch the repository on a delegated pod.

``_start_auto_watch_if_needed`` (create/edit/delete_file) stat'ed the target
repository directory before starting an inotify watch. Under a delegated
content backend the repository is not on this pod -- golden writes go through
the content writer -- so there is nothing here to probe or watch. With the
local backend it is unchanged.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import MagicMock

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import files as files_handler
from code_indexer.server.services import file_crud_service as crud_module
from code_indexer.server.services import auto_watch_manager as watch_module
from code_indexer.server.startup import content_backend_wiring as backend_wiring

_ALIAS = "cidx-meta-global"


class _NonLocalBackend:
    is_local = False


def _user() -> User:
    return User(
        username="poweruser",
        password_hash="hashed_password",
        role=UserRole.POWER_USER,
        email="power@example.com",
        created_at=datetime.now(),
    )


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """cidx-meta-global registered as a write exception; a spy watch manager;
    every is_dir() under the golden repo recorded."""
    repo = tmp_path / "golden-repos" / "cidx-meta"
    repo.mkdir(parents=True)
    service = crud_module.FileCRUDService()
    service.register_write_exception(_ALIAS, repo)
    monkeypatch.setattr(crud_module, "file_crud_service", service)

    watcher = MagicMock(name="auto_watch_manager")
    monkeypatch.setattr(watch_module, "auto_watch_manager", watcher)
    monkeypatch.setattr(files_handler, "_is_write_mode_active", lambda *a: False)

    probes: List[str] = []
    original_is_dir = Path.is_dir

    def _recording_is_dir(self: Path, *args: Any, **kwargs: Any) -> bool:
        if str(self).startswith(str(repo)):
            probes.append(str(self))
        return original_is_dir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_dir", _recording_is_dir)

    return SimpleNamespace(repo=repo, watcher=watcher, probes=probes, service=service)


def test_delegated_pod_never_probes_or_watches_the_repository(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        backend_wiring, "server_content_backend", lambda: _NonLocalBackend()
    )
    resolver = MagicMock(side_effect=AssertionError("resolved a repo path"))
    monkeypatch.setattr(world.service, "get_write_exception_path", resolver)

    files_handler._start_auto_watch_if_needed(_ALIAS, _user(), "TEST-DELEGATED")

    assert world.probes == []
    resolver.assert_not_called()
    world.watcher.start_watch.assert_not_called()


def test_local_backend_still_probes_and_watches(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backend_wiring, "server_content_backend", lambda: None)

    files_handler._start_auto_watch_if_needed(_ALIAS, _user(), "TEST-LOCAL")

    assert world.probes == [str(world.repo)]
    world.watcher.start_watch.assert_called_once_with(str(world.repo))
