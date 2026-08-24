"""Issue #1634: `_resolve_repo_path` (and the synchronous filesystem calls
it transitively performs via `_resolve_golden_repo_path` ->
`AliasManager.read_alias` -- `alias_file.exists()` + `open()` +
`json.load()` against the NFS-backed golden-repos aliases directory) runs
directly on the event-loop thread inside ``regex_search``'s single-repo
branch AND, far worse, inside ``_execute_omni_search``'s per-alias loop --
multiplying the blocking surface up to 50x for one omni-search request.

This project's own documented Production Scale invariant: "NEVER call a
synchronous filesystem/network function directly inside `async def` ...
Offload with `anyio.to_thread.run_sync(...)`" (CLAUDE.md). On this
project's `hard` NFSv3 shared golden-repos mount, `alias_file.exists()`
can block in uninterruptible kernel retry FOREVER if the mount wedges --
one omni request can issue up to 50 of these, any ONE of which can hang
the whole event loop.

Discriminating test strategy: thread-identity capture, mirroring the
established pattern in test_regex_routes_constructor_offload_1609.py --
un-offloaded code ALWAYS records the same OS thread identity as the
caller; a genuine anyio.to_thread.run_sync offload ALWAYS records a
different one. ``AliasManager.read_alias`` is spied (thread-identity
capture, delegates to the REAL implementation) rather than mocked, and
real alias JSON files are written via a real ``AliasManager`` against a
real temp-dir "golden-repos/aliases" directory -- the actual filesystem
resolution mechanism runs for real; only its calling thread is inspected.

Downstream search execution (``_execute_single_search``) is mocked in
these tests: it is an unrelated collaborator, not the alias-resolution
mechanism under test here (that mechanism -- RegexSearchService
construction -- is already covered for real by
test_regex_routes_constructor_offload_1609.py).
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import anyio.to_thread
import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.server import app as app_module
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.routes.regex_routes import (
    RegexSearchRequest,
    _execute_omni_search,
    regex_search,
)

_STUB_RESULT: Dict[str, Any] = {
    "matches": [],
    "total_matches": 0,
    "truncated": False,
    "read_capped": False,
    "search_engine": "ripgrep",
    "search_time_ms": 1.0,
}


def _make_user(role: UserRole = UserRole.NORMAL_USER) -> User:
    return User(
        username="testuser",
        password_hash="$2b$12$x",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _install_read_alias_thread_spy() -> Tuple[List[int], Callable[..., Any]]:
    """Wrap the real AliasManager.read_alias so each call records the OS
    thread identity it executed on, then delegates to the real
    implementation (real alias_file.exists() + open() + json.load())."""
    threads: List[int] = []
    real_read_alias = AliasManager.read_alias

    def _spy_read_alias(self: AliasManager, alias_name: str) -> Any:
        threads.append(threading.get_ident())
        return real_read_alias(self, alias_name)

    return threads, _spy_read_alias


def _patch_golden_repos_dir(golden_repos_dir: str):
    """Context manager-free save/restore helper for app.state.golden_repos_dir."""
    original = getattr(app_module.app.state, "golden_repos_dir", None)
    app_module.app.state.golden_repos_dir = golden_repos_dir

    class _Restorer:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *exc: Any) -> None:
            app_module.app.state.golden_repos_dir = original

    return _Restorer()


def _patch_config_service():
    mock_config = MagicMock()
    mock_config.search_limits_config.timeout_seconds = 30
    mock_get_config_service = MagicMock()
    mock_get_config_service.return_value.get_config.return_value = mock_config
    return patch(
        "code_indexer.server.routes.regex_routes.get_config_service",
        mock_get_config_service,
    )


def _setup_real_aliases(golden_repos_dir, tmp_path, count: int) -> List[str]:
    """Create `count` real alias pointer files via a real AliasManager,
    each pointing to a distinct real temp directory. Returns alias names."""
    aliases_dir = golden_repos_dir / "aliases"
    alias_manager = AliasManager(str(aliases_dir))
    aliases: List[str] = []
    for i in range(count):
        alias_name = f"repo{i}-global"
        target_dir = tmp_path / f"repo{i}"
        target_dir.mkdir()
        alias_manager.create_alias(alias_name, str(target_dir))
        aliases.append(alias_name)
    return aliases


class TestSingleAliasResolutionOffload:
    """The synchronous alias-resolution filesystem calls performed by
    AliasManager.read_alias (via _resolve_repo_path) must run off the
    event-loop thread when reached from regex_search's single-repo
    branch."""

    @pytest.mark.asyncio
    async def test_single_alias_resolution_runs_off_event_loop_thread(self, tmp_path):
        golden_repos_dir = tmp_path / "golden-repos"
        _setup_real_aliases(golden_repos_dir, tmp_path, count=1)

        main_thread_id = threading.get_ident()
        threads, spy_read_alias = _install_read_alias_thread_spy()

        user = _make_user(UserRole.NORMAL_USER)
        body = RegexSearchRequest(pattern="whatever", repository_alias="repo0-global")

        with (
            _patch_golden_repos_dir(str(golden_repos_dir)),
            patch.object(AliasManager, "read_alias", spy_read_alias),
            patch(
                "code_indexer.server.routes.regex_routes._execute_single_search",
                AsyncMock(return_value=dict(_STUB_RESULT)),
            ),
            _patch_config_service(),
        ):
            result = await regex_search(body, user)

        assert result == _STUB_RESULT
        assert threads, "AliasManager.read_alias was never called"
        assert all(tid != main_thread_id for tid in threads), (
            "_resolve_repo_path's AliasManager.read_alias() ran on the "
            "event-loop (calling) thread instead of being offloaded via "
            "anyio.to_thread.run_sync"
        )


class TestOmniAliasResolutionOffload:
    """The synchronous alias-resolution filesystem calls performed for
    EACH alias in an omni (multi-repo) search must run off the event-loop
    thread -- and must do so via a SINGLE batched offload, not one
    anyio.to_thread.run_sync call per alias (avoiding excessive
    thread-pool churn for up to 50 aliases)."""

    @pytest.mark.asyncio
    async def test_omni_alias_resolution_runs_off_event_loop_thread(self, tmp_path):
        golden_repos_dir = tmp_path / "golden-repos"
        aliases = _setup_real_aliases(golden_repos_dir, tmp_path, count=5)

        main_thread_id = threading.get_ident()
        threads, spy_read_alias = _install_read_alias_thread_spy()

        user = _make_user(UserRole.NORMAL_USER)
        body = RegexSearchRequest(pattern="whatever", repository_alias=aliases)

        with (
            _patch_golden_repos_dir(str(golden_repos_dir)),
            patch.object(AliasManager, "read_alias", spy_read_alias),
            patch(
                "code_indexer.server.routes.regex_routes._execute_single_search",
                AsyncMock(return_value=dict(_STUB_RESULT)),
            ),
        ):
            result = await _execute_omni_search(body, aliases, user, timeout_seconds=30)

        assert result["repos_searched"] == 5
        assert threads, "AliasManager.read_alias was never called"
        assert all(tid != main_thread_id for tid in threads), (
            "_execute_omni_search's per-alias AliasManager.read_alias() "
            "call ran on the event-loop (calling) thread instead of being "
            "offloaded via anyio.to_thread.run_sync"
        )

    @pytest.mark.asyncio
    async def test_omni_alias_resolution_uses_single_batched_offload_for_50_aliases(
        self, tmp_path
    ):
        golden_repos_dir = tmp_path / "golden-repos"
        aliases = _setup_real_aliases(golden_repos_dir, tmp_path, count=50)

        user = _make_user(UserRole.NORMAL_USER)
        body = RegexSearchRequest(pattern="whatever", repository_alias=aliases)

        call_count = {"n": 0}
        real_run_sync = anyio.to_thread.run_sync

        async def _counting_run_sync(*args: Any, **kwargs: Any) -> Any:
            call_count["n"] += 1
            return await real_run_sync(*args, **kwargs)

        with (
            _patch_golden_repos_dir(str(golden_repos_dir)),
            patch(
                "code_indexer.server.routes.regex_routes.anyio.to_thread.run_sync",
                _counting_run_sync,
            ),
            patch(
                "code_indexer.server.routes.regex_routes._execute_single_search",
                AsyncMock(return_value=dict(_STUB_RESULT)),
            ),
        ):
            result = await _execute_omni_search(body, aliases, user, timeout_seconds=30)

        assert result["repos_searched"] == 50
        assert call_count["n"] == 1, (
            "expected exactly ONE anyio.to_thread.run_sync call for alias "
            f"resolution across all 50 aliases (batched offload), got "
            f"{call_count['n']} -- issuing one offload per alias would "
            "create excessive thread-pool churn"
        )


class TestBehaviorPreservation:
    """Offloading alias resolution must not change WHAT resolves to WHAT --
    same aliases resolve to the same paths, and error cases (missing
    alias, malformed alias file) still behave identically to the
    pre-fix synchronous implementation."""

    @pytest.mark.asyncio
    async def test_single_alias_resolves_to_correct_path(self, tmp_path):
        golden_repos_dir = tmp_path / "golden-repos"
        aliases = _setup_real_aliases(golden_repos_dir, tmp_path, count=1)
        expected_target = str(tmp_path / "repo0")

        user = _make_user(UserRole.NORMAL_USER)
        body = RegexSearchRequest(pattern="whatever", repository_alias=aliases[0])

        captured: Dict[str, Any] = {}

        async def _capture_repo_path(
            _body: Any, repo_path_str: str, **kwargs: Any
        ) -> Dict[str, Any]:
            captured["repo_path_str"] = repo_path_str
            return dict(_STUB_RESULT)

        with (
            _patch_golden_repos_dir(str(golden_repos_dir)),
            patch(
                "code_indexer.server.routes.regex_routes._execute_single_search",
                _capture_repo_path,
            ),
            _patch_config_service(),
        ):
            result = await regex_search(body, user)

        assert result == _STUB_RESULT
        assert captured["repo_path_str"] == expected_target

    @pytest.mark.asyncio
    async def test_single_alias_missing_returns_404(self, tmp_path):
        golden_repos_dir = tmp_path / "golden-repos"
        (golden_repos_dir / "aliases").mkdir(parents=True)

        user = _make_user(UserRole.NORMAL_USER)
        body = RegexSearchRequest(
            pattern="whatever", repository_alias="does-not-exist-global"
        )

        from fastapi import HTTPException

        with (
            _patch_golden_repos_dir(str(golden_repos_dir)),
            _patch_config_service(),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await regex_search(body, user)

        assert exc_info.value.status_code == 404
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail.get("error_code") == "repository_not_found"

    @pytest.mark.asyncio
    async def test_single_alias_malformed_json_treated_as_not_found(self, tmp_path):
        golden_repos_dir = tmp_path / "golden-repos"
        aliases_dir = golden_repos_dir / "aliases"
        aliases_dir.mkdir(parents=True)
        (aliases_dir / "broken-global.json").write_text("{ not valid json ]")

        user = _make_user(UserRole.NORMAL_USER)
        body = RegexSearchRequest(pattern="whatever", repository_alias="broken-global")

        from fastapi import HTTPException

        with (
            _patch_golden_repos_dir(str(golden_repos_dir)),
            _patch_config_service(),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await regex_search(body, user)

        assert exc_info.value.status_code == 404
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail.get("error_code") == "repository_not_found"

    @pytest.mark.asyncio
    async def test_omni_missing_and_malformed_aliases_recorded_as_errors(
        self, tmp_path
    ):
        golden_repos_dir = tmp_path / "golden-repos"
        aliases = _setup_real_aliases(golden_repos_dir, tmp_path, count=2)
        aliases_dir = golden_repos_dir / "aliases"
        (aliases_dir / "broken-global.json").write_text("{ not valid json ]")

        all_aliases = aliases + ["broken-global", "missing-entirely-global"]

        user = _make_user(UserRole.NORMAL_USER)
        body = RegexSearchRequest(pattern="whatever", repository_alias=all_aliases)

        captured_paths: List[str] = []

        async def _capture_repo_path(
            _body: Any, repo_path_str: str, **kwargs: Any
        ) -> Dict[str, Any]:
            captured_paths.append(repo_path_str)
            return dict(_STUB_RESULT)

        with (
            _patch_golden_repos_dir(str(golden_repos_dir)),
            patch(
                "code_indexer.server.routes.regex_routes._execute_single_search",
                _capture_repo_path,
            ),
        ):
            result = await _execute_omni_search(
                body, all_aliases, user, timeout_seconds=30
            )

        assert result["repos_searched"] == 2
        assert set(captured_paths) == {
            str(tmp_path / "repo0"),
            str(tmp_path / "repo1"),
        }
        assert "broken-global" in result["errors"]
        assert "missing-entirely-global" in result["errors"]
