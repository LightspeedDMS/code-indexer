"""Bug #1876 real-handler integration proof and registered-dispatch coverage.

Reuses this codebase's own established real-handler pattern from
``test_regex_search_e2e_bounded_read_1601.py``: a real git repository on
disk, its absolute path passed directly as ``repository_alias`` (no
"-global" suffix), resolved by ``_legacy._resolve_repo_path``'s real
"full path + is-git-repo" branch -- no mocking of repository resolution,
``RegexSearchService``, or the ripgrep subprocess anywhere in this file.

The async tests in this file call ``handle_regex_search`` directly; they are
handler integration tests, not full MCP JSON-RPC E2E tests.  The synchronous
registry test covers the production dispatch target.  Together they complement
(but do not replace) the service-layer tests in
``tests/unit/global_repos/`` and the xray-layer tests in
``tests/unit/xray/`` -- those prove the fix at the unit closest to the
bug; this file proves the SAME fix is reachable and correct through the
real, user-facing MCP front door end to end.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import pytest

import code_indexer.server.services.config_service as config_service_module
from code_indexer.global_repos.trigram_index_manager import TrigramIndexManager
from code_indexer.server import app as app_module
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers.search import (
    _register,
    handle_regex_search,
    handle_regex_search_sync,
)
from code_indexer.server.services.config_service import (
    ConfigService,
    reset_config_service,
    set_config_service,
)

_PLACEHOLDER_PASSWORD_HASH = "not-a-real-hash-test-placeholder"
_GLOBAL_STATE_LOCK = asyncio.Lock()
_MISSING = object()


@pytest.fixture(autouse=True)
def _no_lazy_build(monkeypatch):
    # Matches tests/unit/global_repos/test_regex_search_trigram_prefilter.py's
    # convention: these repos are small and short-lived, so the background
    # lazy trigram-build thread (daemon=True; harmless to test correctness
    # but noisy -- it can log after pytest's capture stream has closed) is
    # disabled rather than raced.
    monkeypatch.setenv("CIDX_TRIGRAM_LAZY_BUILD", "0")


def _make_user() -> User:
    return User(
        username="e2e_test_user_1876",
        password_hash=_PLACEHOLDER_PASSWORD_HASH,
        role=UserRole.NORMAL_USER,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _enter_real_state(tmp_path):
    previous_golden_dir = getattr(app_module.app.state, "golden_repos_dir", _MISSING)
    app_module.app.state.golden_repos_dir = str(tmp_path)
    previous_config_service = config_service_module._config_service
    real_service = ConfigService(server_dir_path=str(tmp_path / "cfgdir"))
    set_config_service(real_service)
    return previous_golden_dir, previous_config_service


def _exit_real_state(previous_golden_dir, previous_config_service) -> None:
    if previous_golden_dir is _MISSING:
        if hasattr(app_module.app.state, "golden_repos_dir"):
            delattr(app_module.app.state, "golden_repos_dir")
    else:
        app_module.app.state.golden_repos_dir = previous_golden_dir

    if previous_config_service is not None:
        set_config_service(previous_config_service)
    else:
        reset_config_service()


@asynccontextmanager
async def _real_bootstrap(tmp_path):
    async with _GLOBAL_STATE_LOCK:
        previous_golden_dir, previous_config_service = _enter_real_state(tmp_path)
        try:
            yield
        finally:
            _exit_real_state(previous_golden_dir, previous_config_service)


def _build_defect1_repo(tmp_path: Path) -> Path:
    """Real git repo reproducing the exact issue #1876 Defect 1 shape:
    the same literal present in a .java, a .md, and a package-lock.json.
    """
    repo = tmp_path / "defect1-repo"
    (repo / "src" / "auth").mkdir(parents=True)
    (repo / "src" / "auth" / "Service.java").write_text(
        "public class FooAuthenticator {}\n"
    )
    (repo / "README.md").write_text("See FooAuthenticator for details.\n")
    (repo / "package-lock.json").write_text('{"name": "FooAuthenticator"}\n')
    subprocess.run(["git", "init", "--quiet"], cwd=str(repo), check=True, timeout=10)
    return repo


def _build_defect2_repo(tmp_path: Path) -> Path:
    """Real git repo reproducing the exact issue #1876 Defect 2 shape:
    one match with insufficient own trailing context, followed by another
    file whose own leading context could be misattached.
    """
    repo = tmp_path / "defect2-repo"
    repo.mkdir()
    (repo / "one.py").write_text("MATCH_TARGET_1876\nTAIL_ONE\n")
    (repo / "two.py").write_text(
        "HEADER_TWO_A\nHEADER_TWO_B\nMATCH_TARGET_1876\nTAIL_TWO\n"
    )
    subprocess.run(["git", "init", "--quiet"], cwd=str(repo), check=True, timeout=10)
    return repo


def _index(repo: Path) -> None:
    TrigramIndexManager(repo / ".code-indexer" / "trigram_index").build(repo)


@pytest.mark.asyncio
async def test_handler_include_patterns_honoured(tmp_path):
    """Bug #1876 Defect 1, real handler: regex_search with
    include_patterns=["*.md"] must return ONLY the .md file through the
    real handle_regex_search MCP handler.
    """
    repo = _build_defect1_repo(tmp_path)
    _index(repo)
    args = {
        "repository_alias": str(repo),
        "pattern": "FooAuthenticator",
        "include_patterns": ["*.md"],
    }

    async with _real_bootstrap(tmp_path):
        result = await handle_regex_search(args, _make_user())

    data = json.loads(result["content"][0]["text"])
    assert data["success"] is True, data
    files = {m["file_path"] for m in data["matches"]}
    assert files == {"README.md"}, (
        f"handler regex_search with include_patterns=['*.md'] returned "
        f"{files}, expected only README.md"
    )


_SYNC_HANDLER_THREAD_TIMEOUT_SECONDS = 30


def test_registered_sync_regex_handler_honours_include_patterns(tmp_path):
    """Exercise the production registry's synchronous dispatch target.

    Runs it on a worker thread, matching production's ``run_in_executor``
    dispatch: ``handle_regex_search_sync`` calls ``asyncio.run``, whose
    teardown clears the *calling thread's* event loop, which must not be
    the pytest MainThread.
    """
    repo = _build_defect1_repo(tmp_path)
    _index(repo)
    registry: Dict[str, Any] = {}
    _register(registry)
    assert registry["regex_search"] is handle_regex_search_sync
    args = {
        "repository_alias": str(repo),
        "pattern": "FooAuthenticator",
        "include_patterns": ["*.md"],
    }

    previous_golden_dir, previous_config_service = _enter_real_state(tmp_path)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(registry["regex_search"], args, _make_user())
            result = future.result(timeout=_SYNC_HANDLER_THREAD_TIMEOUT_SECONDS)
    finally:
        _exit_real_state(previous_golden_dir, previous_config_service)

    data = json.loads(result["content"][0]["text"])
    assert data["success"] is True, data
    assert {match["file_path"] for match in data["matches"]} == {"README.md"}


@pytest.mark.asyncio
async def test_handler_unindexed_directory_walk_still_honors_include_patterns(
    tmp_path,
):
    """Keep explicit coverage for the pre-existing directory-walk path."""
    repo = _build_defect1_repo(tmp_path)
    args = {
        "repository_alias": str(repo),
        "pattern": "FooAuthenticator",
        "include_patterns": ["*.md"],
    }

    async with _real_bootstrap(tmp_path):
        result = await handle_regex_search(args, _make_user())

    data = json.loads(result["content"][0]["text"])
    assert data["success"] is True, data
    assert {m["file_path"] for m in data["matches"]} == {"README.md"}


@pytest.mark.asyncio
async def test_handler_exclude_patterns_honoured(tmp_path):
    """Bug #1876 Defect 1, real handler: exclude_patterns=["*.java"]
    must remove the .java file.
    """
    repo = _build_defect1_repo(tmp_path)
    _index(repo)
    args = {
        "repository_alias": str(repo),
        "pattern": "FooAuthenticator",
        "exclude_patterns": ["*.java"],
    }

    async with _real_bootstrap(tmp_path):
        result = await handle_regex_search(args, _make_user())

    data = json.loads(result["content"][0]["text"])
    assert data["success"] is True, data
    files = {m["file_path"] for m in data["matches"]}
    assert "src/auth/Service.java" not in files
    assert files == {"README.md", "package-lock.json"}


@pytest.mark.asyncio
async def test_handler_context_lines_smoke_regression(tmp_path):
    """Bug #1876 Defect 2 handler smoke regression: context_lines must never
    attach context from a different file than the match.
    """
    repo = _build_defect2_repo(tmp_path)
    _index(repo)
    args = {
        "repository_alias": str(repo),
        "pattern": "MATCH_TARGET_1876",
        "context_lines": 5,
    }

    async with _real_bootstrap(tmp_path):
        result = await handle_regex_search(args, _make_user())

    data = json.loads(result["content"][0]["text"])
    assert data["success"] is True, data
    assert len(data["matches"]) == 2, data["matches"]

    by_file = {m["file_path"]: m for m in data["matches"]}
    one_match = by_file["one.py"]
    two_match = by_file["two.py"]

    assert one_match["context_before"] == []
    assert one_match["context_after"] == ["TAIL_ONE"], (
        f"one.py's context_after leaked foreign content: {one_match['context_after']!r}"
    )
    assert two_match["context_before"] == ["HEADER_TWO_A", "HEADER_TWO_B"], (
        f"two.py's context_before is missing/wrong: {two_match['context_before']!r}"
    )
    assert two_match["context_after"] == ["TAIL_TWO"]
