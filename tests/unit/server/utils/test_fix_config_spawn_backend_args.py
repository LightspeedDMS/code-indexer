"""Server-spawned ``cidx fix-config`` carries the repository's backend selection.

Every server spawn of ``cidx fix-config --force`` (refresh snapshot, master
restore, branch-change snapshot) is stamped exactly like the same repository's
``cidx index`` spawn, through ``server_backend_args`` + ``repo_layout_kwargs``:
delegated backends are stamped, the local default stamps nothing, and pod-local
``local://`` repositories stay unstamped. Without the stamp, fix-config on a
mount-less pod overwrote the git state the index run recorded.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import Mock

import pytest

from code_indexer.global_repos import refresh_scheduler as rs
from code_indexer.server.repositories import golden_repo_manager as grm
from code_indexer.server.utils import index_command_layout as layout
from code_indexer.server.utils.index_command_layout import (
    SERVER_DELEGATED_CONTENT_BACKEND_ARG as CONTENT,
    SERVER_DELEGATED_GIT_BACKEND_ARG as GIT,
    append_server_layout_args,
    server_backend_args,
)

FIX = ["cidx", "fix-config", "--force"]
GIT_URL = "https://example.com/acme/app.git"

# (repo_url, alias, expected args under a delegated server)
CASES = [
    (GIT_URL, "app-global", [CONTENT, GIT]),
    ("local://langfuse_traces", "langfuse_traces-global", []),
    ("local://cidx-meta", "cidx-meta-global", [CONTENT]),
    (None, "unregistered-global", [CONTENT, GIT]),
]


@pytest.fixture
def server_backends(monkeypatch: pytest.MonkeyPatch):
    def _set(name: str) -> None:
        monkeypatch.setattr(layout, "configured_content_backend_name", lambda: name)
        monkeypatch.setattr(layout, "configured_git_backend_name", lambda: name)

    return _set


# ── the helper ──────────────────────────────────────────────────────────────


def test_backend_args_follow_the_server_selection(server_backends) -> None:
    server_backends("delegated")
    assert server_backend_args() == [CONTENT, GIT]
    server_backends("local")
    assert server_backend_args() == []


def test_backend_args_for_pod_local_content_are_empty(server_backends) -> None:
    server_backends("delegated")
    assert server_backend_args(local_content=True) == []


def test_layout_args_are_unchanged_by_the_extraction(server_backends) -> None:
    server_backends("delegated")
    assert append_server_layout_args(["cidx", "index"]) == [
        "cidx",
        "index",
        layout.SERVER_NEW_COLLECTION_LAYOUT_ARG,
        layout.SERVER_IGNORE_RESUME_STATE_ARG,
        layout.SERVER_MANAGED_PROVIDER_SETTINGS_ARG,
        CONTENT,
        GIT,
    ]
    server_backends("local")
    assert append_server_layout_args(["cidx", "index"])[-1] == (
        layout.SERVER_MANAGED_PROVIDER_SETTINGS_ARG
    )


# ── RefreshScheduler ────────────────────────────────────────────────────────


def _scheduler(repo_url: Optional[str]) -> Any:
    scheduler = rs.RefreshScheduler.__new__(rs.RefreshScheduler)
    registry = Mock()
    registry.get_global_repo.return_value = (
        {"repo_url": repo_url} if repo_url is not None else None
    )
    scheduler.registry = registry
    return scheduler


@pytest.mark.parametrize("repo_url, alias, expected", CASES)
def test_snapshot_fix_config_is_stamped(
    server_backends, tmp_path: Path, repo_url, alias, expected
) -> None:
    server_backends("delegated")
    scheduler = _scheduler(repo_url)
    calls: List[List[str]] = []
    scheduler._run_subprocess = lambda cmd, **kw: calls.append(cmd)

    scheduler._fix_snapshot_config(alias, tmp_path, timeout=5)

    assert calls == [FIX + expected]


@pytest.mark.parametrize("repo_url, alias, expected", CASES)
def test_snapshot_fix_config_is_unstamped_on_a_local_server(
    server_backends, tmp_path: Path, repo_url, alias, expected
) -> None:
    server_backends("local")
    scheduler = _scheduler(repo_url)
    calls: List[List[str]] = []
    scheduler._run_subprocess = lambda cmd, **kw: calls.append(cmd)

    scheduler._fix_snapshot_config(alias, tmp_path, timeout=5)

    assert calls == [FIX]


@pytest.mark.parametrize("backend", ["delegated", "local"])
def test_master_restore_fix_config_is_stamped(
    server_backends, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend
) -> None:
    server_backends(backend)
    scheduler = _scheduler(GIT_URL)
    snapshot_manager = Mock()
    snapshot_manager.latest_snapshot.return_value = str(tmp_path / "v_1")
    scheduler._snapshot_manager = snapshot_manager
    calls: List[List[str]] = []
    monkeypatch.setattr(rs.subprocess, "run", lambda cmd, **kw: calls.append(cmd))

    assert scheduler._restore_master_from_versioned("app-global", tmp_path / "m")

    assert calls == [FIX + ([CONTENT, GIT] if backend == "delegated" else [])]


# ── GoldenRepoManager branch-change snapshot ────────────────────────────────


@pytest.mark.parametrize("repo_url, alias, expected", CASES)
def test_branch_change_snapshot_fix_config_is_stamped(
    server_backends,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo_url,
    alias,
    expected,
) -> None:
    server_backends("delegated")
    manager = grm.GoldenRepoManager.__new__(grm.GoldenRepoManager)
    snapshot_manager = Mock()
    snapshot_manager.create_snapshot.return_value = tmp_path / "v_1"
    manager._snapshot_manager = snapshot_manager
    golden = SimpleNamespace(repo_url=repo_url) if repo_url is not None else None
    monkeypatch.setattr(manager, "get_golden_repo", lambda a: golden)
    calls: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        grm.subprocess, "run", lambda cmd, **kw: calls.append({"cmd": cmd, **kw})
    )

    snapshot = manager._cb_cow_snapshot_impl(alias, str(tmp_path), 5, 5)

    assert snapshot == str(tmp_path / "v_1")
    assert [c["cmd"] for c in calls] == [FIX + expected]
    assert calls[0]["cwd"] == snapshot
