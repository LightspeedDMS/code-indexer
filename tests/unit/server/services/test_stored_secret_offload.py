"""Async request paths never run stored-secret encryption, the cluster gate
query or a CI-token read on the event-loop thread."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest

from code_indexer.server.services.git_credential_manager import (
    create_git_credential_manager,
)
from code_indexer.server.storage.database_manager import DatabaseSchema


class _ForgeStub:  # external forge API
    async def validate_and_discover(self, token: str, host: str) -> Dict[str, Any]:
        return {"git_user_name": "Example"}


def test_configure_credential_encrypts_off_the_event_loop(tmp_path: Path) -> None:
    db = str(tmp_path / "server.db")
    DatabaseSchema(db).initialize_database()
    mgr = create_git_credential_manager(db, str(tmp_path))
    threads: List[threading.Thread] = []
    real_gate = mgr._cipher.v2_writes_allowed

    def recording_gate() -> bool:
        threads.append(threading.current_thread())
        return real_gate()

    mgr._cipher.v2_writes_allowed = recording_gate  # type: ignore[method-assign]

    async def run() -> threading.Thread:
        with mock.patch(
            "code_indexer.server.services.git_credential_manager.get_forge_client",
            return_value=_ForgeStub(),
        ):
            await mgr.configure_credential(
                "alice", "github", "git.example.com", "ghp_" + "h" * 36
            )
        return threading.current_thread()

    loop_thread = asyncio.run(run())
    assert threads and all(t is not loop_thread for t in threads)


class _RecordingTokenManager:
    def __init__(self, threads: List[threading.Thread]) -> None:
        self._threads = threads

    def get_token(self, platform: str) -> Optional[Any]:
        self._threads.append(threading.current_thread())
        return None


@pytest.mark.parametrize(
    "check",
    [
        "check_github_api",
        "check_gitlab_api",
        "check_github_token",
        "check_gitlab_token",
    ],
)
def test_diagnostics_read_ci_tokens_off_the_event_loop(check: str) -> None:
    from code_indexer.server.services.diagnostics_service import DiagnosticsService

    service: Any = DiagnosticsService.__new__(DiagnosticsService)
    threads: List[threading.Thread] = []
    built_on: List[threading.Thread] = []

    def build() -> _RecordingTokenManager:
        built_on.append(threading.current_thread())
        return _RecordingTokenManager(threads)

    service._get_token_manager = build

    async def run() -> threading.Thread:
        await getattr(service, check)()
        return threading.current_thread()

    loop_thread = asyncio.run(run())
    assert threads and built_on
    assert all(t is not loop_thread for t in threads + built_on)
