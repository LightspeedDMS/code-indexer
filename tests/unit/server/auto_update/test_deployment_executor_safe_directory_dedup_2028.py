"""Bug #2028: the auto-updater must add a git safe.directory entry only when
it is absent, and must collapse existing duplicates (left by earlier,
non-idempotent deploys) to exactly one entry -- touching nothing else.

Every test runs real git against a scratch global config
(``GIT_CONFIG_GLOBAL``), never the developer's ``~/.gitconfig``.  The only
test double is the network ``git clone`` of the hnswlib fallback.
"""

from __future__ import annotations

import logging
import os
import subprocess
from contextlib import ExitStack
from pathlib import Path
from typing import Any, List, Sequence
from unittest.mock import patch

import pytest

from code_indexer.server.auto_update import deployment_executor as de
from code_indexer.server.auto_update.deployment_executor import (
    DeploymentExecutor,
    ensure_single_safe_directory,
)

READ_FAILURE_EXIT = 128

_REAL_RUN = subprocess.run
IDENTITY = "[user]\n\tname = Example Developer\n\temail = dev@example.com\n"
IDENTITY_KEYS = 2
REPEATED_CALLS = 3
POLLUTED_DUPLICATES = 50
DEPLOY_DUPLICATES = 5
SELF_HEAL = "_ensure_safe_directory_entries_deduplicated"
# Every step execute() calls (besides the self-heal), patched to a no-op
# success so the test observes the self-heal's wiring alone.
EXECUTE_STEPS = (
    "git_pull",
    "git_submodule_update",
    "_build_hnswlib_with_fallback",
    "pip_install",
    "ensure_ripgrep",
    "ensure_nodejs",
    "ensure_scip_python",
    "_write_status_file",
    "_restart_auto_update_service",
    "_ensure_activated_repos_symlink_for_cow_daemon",
    "_ensure_auto_updater_uses_server_python",
    "_ensure_auto_update_service_has_cli_path",
    "_ensure_cidx_repo_root",
    "_ensure_claude_cli_installed",
    "_ensure_claude_cli_updated",
    "_ensure_cli_dependencies_synced",
    "_ensure_cli_hnswlib_capability",
    "_ensure_codex_cli_installed",
    "_ensure_cow_daemon_user_in_service_group",
    "_ensure_cow_storage_mount_options",
    "_ensure_daemon_storage_path",
    "_ensure_data_dir_env_var",
    "_ensure_git_safe_directory",
    "_ensure_git_safe_directory_wildcard",
    "_ensure_golden_repos_symlink_for_cow_daemon",
    "_ensure_launch_config",
    "_ensure_malloc_arena_max",
    "_ensure_memory_overcommit",
    "_ensure_nfs_research_symlinks",
    "_ensure_pace_maker_installed",
    "_ensure_rust_toolchain",
    "_ensure_sudoers_restart",
    "_ensure_swap_file",
    "_ensure_systemd_claude_path",
)


@pytest.fixture
def global_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "global-gitconfig"
    config.write_text(IDENTITY)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    return config


def _git_config(config: Path, *args: str) -> List[str]:
    """Read *config*; exit 1 (key absent) is an empty answer, any other
    failure is a test error."""
    result = _REAL_RUN(
        ["git", "config", "--file", str(config), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise AssertionError(f"git config {args} failed: {result.stderr}")
    return result.stdout.splitlines()


def _safe_dirs(config: Path) -> List[str]:
    return _git_config(config, "--get-all", "safe.directory")


def _add(config: Path, value: str, times: int = 1) -> None:
    for _ in range(times):
        _REAL_RUN(
            ["git", "config", "--file", str(config), "--add", "safe.directory", value],
            check=True,
        )


# Any: mirrors subprocess.run's overloaded signature, which it stands in for.
def _offline_clone(cmd: Sequence[str], *args: Any, **kwargs: Any) -> Any:
    """Only the network clone is faked; every git config call is real."""
    if list(cmd[:2]) == ["git", "clone"]:
        return subprocess.CompletedProcess(list(cmd), 0, "", "")
    return _REAL_RUN(cmd, *args, **kwargs)


# Any: mirrors subprocess.run's overloaded signature, which it stands in for.
def _listing_fails(cmd: Sequence[str], *args: Any, **kwargs: Any) -> Any:
    """Only the safe.directory listing fails (writes stay real): a read
    failure must never be mistaken for 'absent'."""
    if "--get-all" in cmd:
        return subprocess.CompletedProcess(
            list(cmd), READ_FAILURE_EXIT, "", "injected read failure"
        )
    return _REAL_RUN(cmd, *args, **kwargs)


def test_listing_exit_1_means_absent_and_adds(
    tmp_path: Path, global_config: Path
) -> None:
    path = str(tmp_path / "repo")
    assert ensure_single_safe_directory(path) is None
    assert _safe_dirs(global_config) == [path]


@pytest.mark.parametrize("add_if_absent", [True, False])
def test_read_failure_is_an_error_never_absent(
    tmp_path: Path, global_config: Path, add_if_absent: bool
) -> None:
    path = str(tmp_path / "repo")
    with patch.object(de.subprocess, "run", side_effect=_listing_fails):
        error = ensure_single_safe_directory(path, add_if_absent=add_if_absent)
    assert error is not None and "injected read failure" in error
    assert _safe_dirs(global_config) == []  # nothing added


def test_self_heal_logs_226_when_the_config_cannot_be_read(
    tmp_path: Path, global_config: Path, caplog: pytest.LogCaptureFixture
) -> None:
    global_config.write_text("[[[malformed\n")
    executor = DeploymentExecutor(repo_path=tmp_path / "repo")
    with caplog.at_level(logging.WARNING):
        assert getattr(executor, SELF_HEAL)() is True
    assert "DEPLOY-GENERAL-226" in caplog.text


def test_submodule_safe_directory_is_added_once(
    tmp_path: Path, global_config: Path
) -> None:
    executor = DeploymentExecutor(repo_path=tmp_path / "repo")
    submodule = tmp_path / "repo" / "third_party" / "hnswlib"
    submodule.mkdir(parents=True)

    for _ in range(REPEATED_CALLS):
        assert executor._ensure_submodule_safe_directory() is True

    assert _safe_dirs(global_config).count(str(submodule)) == 1


def test_hnswlib_fallback_safe_directory_is_added_once(
    tmp_path: Path, global_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fallback = tmp_path / "cidx-hnswlib"
    monkeypatch.setattr(de, "HNSWLIB_FALLBACK_PATH", fallback)
    executor = DeploymentExecutor(repo_path=tmp_path / "repo")

    with patch.object(de.subprocess, "run", side_effect=_offline_clone):
        for _ in range(REPEATED_CALLS):
            assert executor._clone_hnswlib_standalone() is True

    assert _safe_dirs(global_config).count(str(fallback)) == 1


def test_polluted_config_collapses_to_one_entry_and_keeps_the_rest(
    tmp_path: Path, global_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The managed path holds a regex metacharacter ('.'); an unrelated
    entry that an unescaped pattern would also match must survive."""
    fallback = tmp_path / "cidx.hnswlib"
    # look-alike, prefix/suffix decoys (pin the ^...$ anchors), and unrelated
    unrelated = (
        str(tmp_path / "cidxXhnswlib"),
        str(fallback) + "-old",
        str(fallback) + "/",
        "/prefix" + str(fallback),
        "/srv/example-repo",
    )
    monkeypatch.setattr(de, "HNSWLIB_FALLBACK_PATH", fallback)
    _add(global_config, unrelated[0])
    _add(global_config, str(fallback), times=POLLUTED_DUPLICATES)
    for value in unrelated[1:]:
        _add(global_config, value)
    identity_before = _git_config(global_config, "--get-regexp", "^user\\.")
    assert len(identity_before) == IDENTITY_KEYS

    executor = DeploymentExecutor(repo_path=tmp_path / "repo")
    assert getattr(executor, SELF_HEAL)() is True

    entries = _safe_dirs(global_config)
    assert entries.count(str(fallback)) == 1
    assert all(entries.count(value) == 1 for value in unrelated)
    assert len(entries) == 1 + len(unrelated)
    assert _git_config(global_config, "--get-regexp", "^user\\.") == identity_before


def test_self_heal_is_a_noop_with_zero_or_one_entry(
    tmp_path: Path, global_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fallback = tmp_path / "cidx-hnswlib"
    monkeypatch.setattr(de, "HNSWLIB_FALLBACK_PATH", fallback)
    executor = DeploymentExecutor(repo_path=tmp_path / "repo")

    assert getattr(executor, SELF_HEAL)() is True
    assert _safe_dirs(global_config) == []  # absent stays absent

    _add(global_config, str(fallback))
    before = global_config.read_bytes()
    assert getattr(executor, SELF_HEAL)() is True
    assert global_config.read_bytes() == before


def test_execute_runs_the_self_heal_so_a_deploy_converges(
    tmp_path: Path, global_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every other execute() step is patched by name (the established
    wiring-test pattern); the self-heal itself runs for real."""
    fallback = tmp_path / "cidx-hnswlib"
    monkeypatch.setattr(de, "HNSWLIB_FALLBACK_PATH", fallback)
    _add(global_config, str(fallback), times=DEPLOY_DUPLICATES)
    executor = DeploymentExecutor(repo_path=tmp_path / "repo")

    tmpdir_before = os.environ.get("TMPDIR")
    with ExitStack() as stack:
        # execute() sets os.environ["TMPDIR"]: restored on exit, never leaked
        stack.enter_context(patch.dict(os.environ))
        for name in EXECUTE_STEPS:
            stack.enter_context(patch.object(executor, name, return_value=True))
        stack.enter_context(
            patch.object(executor, "_deploy_tmpdir", return_value=str(tmp_path))
        )
        # equal hashes: no auto-updater self-restart branch
        stack.enter_context(
            patch.object(
                executor, "_calculate_auto_update_hash", return_value="same-hash"
            )
        )
        assert executor.execute() is True

    assert _safe_dirs(global_config).count(str(fallback)) == 1
    assert os.environ.get("TMPDIR") == tmpdir_before
