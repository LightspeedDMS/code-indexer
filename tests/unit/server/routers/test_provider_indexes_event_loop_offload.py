"""
Discriminating tests for the P2 review finding on #1894/#1895:
`remove_provider_index()` and `bulk_add()` in
`server/routers/provider_indexes.py` are `async def` FastAPI handlers that
called the synchronous config read-modify-write helpers
(`_remove_provider_from_config` / `_append_provider_to_config`, which write
via `write_json_atomic()` -- fsync + chmod + os.replace) directly on the
event-loop thread. On a slow/`hard` NFS mount this blocks the WHOLE event
loop; `bulk_add` previously did one such blocking write PER repository,
serially, on the loop.

These tests prove the fix offloads that work to a worker thread via
`anyio.to_thread.run_sync` -- not merely that behavior is preserved. Each
test fails on the BEHAVIOUR (write happens on the event-loop thread, or one
`run_sync` call per repo) if the offload is reverted, not on a missing
symbol.
"""

import asyncio
import threading
from typing import Any, Dict, List, cast
from unittest.mock import Mock

import anyio.to_thread
from fastapi import Request

from code_indexer.server.auth.user_manager import User
from code_indexer.server.routers.provider_indexes import (
    BulkAddRequest,
    ProviderIndexRequest,
    add_provider_index,
    bulk_add,
    remove_provider_index,
)


class _FakeUser:
    username = "admin"


def _fake_config_service() -> Mock:
    service = Mock()
    service.get_config.return_value = Mock()
    return service


def test_remove_provider_index_offloads_config_write_off_event_loop_thread(
    monkeypatch,
) -> None:
    """The single write in remove_provider_index() must run on an anyio
    worker thread, not the FastAPI event-loop thread. Before the fix,
    `_remove_provider_from_config` is called directly, so the captured
    thread equals the event-loop thread and the assertion below fails on
    that BEHAVIOUR (not on an AttributeError/missing symbol)."""
    event_loop_thread = threading.current_thread()
    captured: Dict[str, threading.Thread] = {}

    def fake_remove(repo_path: str, provider_name: str) -> None:
        captured["thread"] = threading.current_thread()

    monkeypatch.setattr(
        "code_indexer.server.mcp.handlers._remove_provider_from_config",
        fake_remove,
    )
    monkeypatch.setattr(
        "code_indexer.server.mcp.handlers._resolve_golden_repo_path",
        lambda alias: "/tmp/fake-repo",
    )
    monkeypatch.setattr(
        "code_indexer.server.mcp.handlers._resolve_golden_repo_base_clone",
        lambda alias: "/tmp/fake-repo/base",
    )
    monkeypatch.setattr(
        "code_indexer.server.services.config_service.get_config_service",
        _fake_config_service,
    )
    monkeypatch.setattr(
        "code_indexer.server.services.provider_index_service.ProviderIndexService.validate_provider",
        lambda self, provider: None,
    )
    monkeypatch.setattr(
        "code_indexer.server.services.provider_index_service.ProviderIndexService.remove_provider_index",
        lambda self, base_clone, provider: {
            "removed": True,
            "collection_name": "fake-collection",
            "message": "removed",
        },
    )

    body = ProviderIndexRequest(provider="cohere", alias="myrepo")
    result = asyncio.run(
        remove_provider_index(
            body=body,
            request=cast(Request, None),
            current_user=cast(User, _FakeUser()),
        )
    )

    assert result["success"] is True
    assert "thread" in captured, "the config write helper was never invoked"
    assert captured["thread"] is not event_loop_thread, (
        "_remove_provider_from_config must run via anyio.to_thread.run_sync "
        "on a worker thread, not the event-loop thread -- otherwise a slow "
        "NFS write blocks the whole event loop at 900-repo scale"
    )


def test_add_provider_index_offloads_config_write_off_event_loop_thread(
    monkeypatch,
) -> None:
    """add_provider_index() -> _submit_index_job() has the IDENTICAL
    synchronous config-write-inside-async-handler defect already fixed in
    remove_provider_index()/bulk_add(): _append_provider_to_config() (fsync
    + chmod + os.replace via write_json_atomic) must run on an anyio
    worker thread, not the event-loop thread. Before the fix, it is called
    directly, so the captured thread equals the event-loop thread and this
    fails on that BEHAVIOUR (not on a missing symbol)."""
    event_loop_thread = threading.current_thread()
    captured: Dict[str, threading.Thread] = {}

    def fake_append(repo_path: str, provider_name: str) -> bool:
        captured["thread"] = threading.current_thread()
        return True

    monkeypatch.setattr(
        "code_indexer.server.mcp.handlers._append_provider_to_config",
        fake_append,
    )
    monkeypatch.setattr(
        "code_indexer.server.mcp.handlers._resolve_golden_repo_path",
        lambda alias: "/tmp/fake-repo",
    )
    monkeypatch.setattr(
        "code_indexer.server.mcp.handlers._resolve_golden_repo_base_clone",
        lambda alias: "/tmp/fake-repo/base",
    )
    monkeypatch.setattr(
        "code_indexer.server.services.config_service.get_config_service",
        _fake_config_service,
    )
    monkeypatch.setattr(
        "code_indexer.server.services.provider_index_service.ProviderIndexService.validate_provider",
        lambda self, provider: None,
    )

    class _FakeBGM:
        def submit_job(self, **kwargs: Any) -> str:
            return "job-456"

    class _FakeAppState:
        background_job_manager = _FakeBGM()

    class _FakeApp:
        state = _FakeAppState()

    class _FakeRequest:
        app = _FakeApp()

    body = ProviderIndexRequest(provider="cohere", alias="myrepo")
    result = asyncio.run(
        add_provider_index(
            body=body,
            request=cast(Request, _FakeRequest()),
            current_user=cast(User, _FakeUser()),
        )
    )

    assert result["success"] is True
    assert "thread" in captured, "the config write helper was never invoked"
    assert captured["thread"] is not event_loop_thread, (
        "_append_provider_to_config must run via anyio.to_thread.run_sync "
        "on a worker thread, not the event-loop thread -- otherwise a slow "
        "NFS write blocks the whole event loop at 900-repo scale"
    )


def test_bulk_add_offloads_whole_batch_in_a_single_run_sync_call(
    monkeypatch,
) -> None:
    """bulk_add() must offload the ENTIRE synchronous batch (path
    resolution + status check + config write for every repo) via ONE
    anyio.to_thread.run_sync call -- not one call per repo, which just
    serializes N thread hops back onto the event loop. Before the fix, the
    loop never calls anyio.to_thread.run_sync at all, so call_count stays 0
    and this fails on that BEHAVIOUR."""
    event_loop_thread = threading.current_thread()
    call_count = {"n": 0}
    write_threads: List[threading.Thread] = []

    real_run_sync = anyio.to_thread.run_sync

    async def counting_run_sync(func, *args):
        call_count["n"] += 1
        return await real_run_sync(func, *args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", counting_run_sync)

    def fake_append(repo_path: str, provider_name: str) -> bool:
        write_threads.append(threading.current_thread())
        return True

    repos = [
        {"alias_name": "repo1", "category": ""},
        {"alias_name": "repo2", "category": ""},
        {"alias_name": "repo3", "category": ""},
    ]

    monkeypatch.setattr(
        "code_indexer.server.mcp.handlers._append_provider_to_config",
        fake_append,
    )
    monkeypatch.setattr(
        "code_indexer.server.mcp.handlers._resolve_golden_repo_path",
        lambda alias: f"/tmp/{alias}",
    )
    monkeypatch.setattr(
        "code_indexer.server.mcp.handlers._resolve_golden_repo_base_clone",
        lambda alias: f"/tmp/{alias}/base",
    )
    monkeypatch.setattr(
        "code_indexer.server.mcp.handlers._list_global_repos",
        lambda: repos,
    )
    monkeypatch.setattr(
        "code_indexer.server.services.config_service.get_config_service",
        _fake_config_service,
    )
    monkeypatch.setattr(
        "code_indexer.server.services.provider_index_service.ProviderIndexService.validate_provider",
        lambda self, provider: None,
    )
    monkeypatch.setattr(
        "code_indexer.server.services.provider_index_service.ProviderIndexService.get_provider_index_status",
        lambda self, repo_path, alias: {},
    )

    class _FakeBGM:
        def submit_job(self, **kwargs: Any) -> str:
            return "job-123"

    class _FakeAppState:
        background_job_manager = _FakeBGM()

    class _FakeApp:
        state = _FakeAppState()

    class _FakeRequest:
        app = _FakeApp()

    body = BulkAddRequest(provider="cohere")
    result = asyncio.run(
        bulk_add(
            body=body,
            request=cast(Request, _FakeRequest()),
            current_user=cast(User, _FakeUser()),
        )
    )

    assert result["jobs_created"] == 3
    assert call_count["n"] == 1, (
        "expected the entire batch offloaded via exactly ONE "
        f"anyio.to_thread.run_sync call, got {call_count['n']} -- one call "
        "per repo just serializes N thread hops back onto the event loop"
    )
    assert write_threads, "the per-repo config write was never invoked"
    assert all(t is not event_loop_thread for t in write_threads), (
        "bulk config writes must run off the event-loop thread"
    )
