"""Unit tests for Story #1001: Extension drift → force_reconcile orchestration.

Tests verify that _execute_refresh() correctly detects extension drift, checks for
matching files, and causes --reconcile to appear in the cidx index command when needed.

The SUT is the drift-related orchestration path in _execute_refresh() and the
force_reconcile logic in _index_source(). Internal scheduler methods unrelated
to drift (snapshot creation, alias swap, registry reconciliation, etc.) are stubbed
to isolate the SUT to the drift-related code paths only.

External service boundaries mocked:
- get_config_service() (controlled drift result via mock ConfigService)
- has_files_with_extensions() (controls whether matching files are found)
- run_with_popen_progress() (captures cidx commands; creates index dir as side effect)
- gather_repo_metrics() (returns zero counts to skip metrics computation)
- subprocess.run (stubs git operations and cidx fix-config calls)

Internal stubs (not part of drift path, isolated for test focus):
- _detect_existing_indexes, _reconcile_registry_with_filesystem, _create_snapshot,
  alias_manager.swap_alias, alias_manager.read_alias, is_write_locked, _reset_fetch_failures

Acceptance Criteria:
- AC1: drift + matching files → --reconcile in cidx command
- AC2: drift + no matching files → no --reconcile
- AC3: no drift → no --reconcile
- AC4: removal drift + matching files → --reconcile
- AC5: interrupted metadata without drift → --reconcile (crash recovery)
- AC6: interrupted metadata + drift → --reconcile (OR logic)
"""

import json
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.config import ConfigManager
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _RegistryStub:
    def __init__(self):
        self._repo_info = {
            "repo_url": "git@github.com:org/repo.git",
            "default_branch": "main",
            "enable_temporal": False,
        }

    def get_global_repo(self, alias_name: str) -> dict:
        return self._repo_info

    def update_refresh_timestamp(self, alias_name: str) -> None:
        return None


def _make_scheduler(tmp_path):
    """Build a RefreshScheduler with non-drift internals stubbed."""
    golden_repos_dir = tmp_path / "golden_repos"
    golden_repos_dir.mkdir(parents=True)
    config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
    sched = RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=QueryTracker(),
        cleanup_manager=CleanupManager(QueryTracker()),
        registry=_RegistryStub(),
    )
    sched._detect_existing_indexes = MagicMock(return_value={})
    sched._reconcile_registry_with_filesystem = MagicMock()
    sched._create_snapshot = MagicMock(return_value=str(tmp_path / "snapshot"))
    sched.alias_manager.swap_alias = MagicMock()
    sched.alias_manager.read_alias = MagicMock(
        return_value=str(golden_repos_dir / ".versioned" / "repo" / "v_1")
    )
    sched.is_write_locked = MagicMock(return_value=False)
    sched._reset_fetch_failures = MagicMock()
    return sched, golden_repos_dir


def _make_source_repo(base: Path) -> Path:
    """Create a minimal source repo directory."""
    src = base / "my-repo"
    src.mkdir(parents=True, exist_ok=True)
    (src / ".code-indexer").mkdir(exist_ok=True)
    (src / ".git").mkdir(exist_ok=True)
    return src


def _make_drift(added=None, removed=None):
    """Build an ExtensionDrift instance."""
    from code_indexer.server.services.config_service import ExtensionDrift

    return ExtensionDrift(
        added=set(added) if added else set(),
        removed=set(removed) if removed else set(),
    )


def _run_execute_refresh_capture_popen(
    sched, source_repo, drift, scanner_result: bool
) -> List[List[str]]:
    """Run real _execute_refresh() and capture popen commands."""
    captured: List[List[str]] = []

    def mock_popen(**kwargs):
        captured.append(list(kwargs.get("command", [])))
        cwd = kwargs.get("cwd", "")
        (Path(cwd) / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)
        return 0

    with patch(
        "code_indexer.global_repos.refresh_scheduler.get_config_service"
    ) as mock_get_cs:
        mock_cs = MagicMock()
        mock_cs.sync_repo_extensions_if_drifted.return_value = drift
        mock_get_cs.return_value = mock_cs

        with patch(
            "code_indexer.global_repos.refresh_scheduler.has_files_with_extensions",
            return_value=scanner_result,
        ):
            with patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=mock_popen,
            ):
                with patch(
                    "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                    return_value=(0, 0),
                ):
                    with patch(
                        "code_indexer.global_repos.refresh_scheduler.subprocess.run",
                        return_value=MagicMock(returncode=0),
                    ):
                        sched._execute_refresh("my-repo-global")
    return captured


def _semantic_cmd(captured: List[List[str]]) -> List[str]:
    """Return the first cidx command containing --fts (semantic indexing)."""
    cmds = [c for c in captured if "--fts" in c]
    assert cmds, f"No semantic (--fts) command found in captured: {captured}"
    return cmds[0]


# ---------------------------------------------------------------------------
# Tests: _index_source() accepts force_reconcile (runtime contract)
# ---------------------------------------------------------------------------


class TestIndexSourceForceReconcileParameter:
    """_index_source() must accept and process a force_reconcile keyword argument."""

    def test_force_reconcile_false_does_not_raise(self, tmp_path):
        """Calling _index_source(force_reconcile=False) must not raise TypeError."""
        sched, golden_repos_dir = _make_scheduler(tmp_path)
        source_repo = _make_source_repo(golden_repos_dir)

        def mock_popen(**kwargs):
            cwd = kwargs.get("cwd", "")
            (Path(cwd) / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)
            return 0

        with patch(
            "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
            side_effect=mock_popen,
        ):
            with patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(0, 0),
            ):
                with patch(
                    "code_indexer.global_repos.refresh_scheduler.subprocess.run",
                    return_value=MagicMock(returncode=0),
                ):
                    sched._index_source(
                        alias_name="my-repo-global",
                        source_path=str(source_repo),
                        force_reconcile=False,
                    )
        # Reaching here without TypeError confirms force_reconcile is accepted


# ---------------------------------------------------------------------------
# Tests: AC1-AC4 parametrized — drift orchestration decisions
# ---------------------------------------------------------------------------


class TestDriftOrchestrationAC1toAC4:
    """AC1-AC4: drift + scanner result → correct --reconcile decision."""

    @pytest.mark.parametrize(
        "label, drift_kwargs, scanner_result, expect_reconcile",
        [
            ("AC1_addition_drift_with_files", {"added": ["jsonl"]}, True, True),
            ("AC2_addition_drift_no_files", {"added": ["jsonl"]}, False, False),
            ("AC3_no_drift", None, False, False),
            ("AC4_removal_drift_with_files", {"removed": ["log"]}, True, True),
        ],
    )
    def test_drift_reconcile_decision(
        self, tmp_path, label, drift_kwargs, scanner_result, expect_reconcile
    ):
        """--reconcile present/absent based on drift and matching files."""
        sched, golden_repos_dir = _make_scheduler(tmp_path)
        source_repo = _make_source_repo(golden_repos_dir)

        drift = _make_drift(**drift_kwargs) if drift_kwargs is not None else None
        captured = _run_execute_refresh_capture_popen(
            sched, source_repo, drift=drift, scanner_result=scanner_result
        )
        cmd = _semantic_cmd(captured)
        has_reconcile = "--reconcile" in cmd
        assert has_reconcile is expect_reconcile, (
            f"[{label}] Expected --reconcile={expect_reconcile} but cidx cmd was: {cmd}"
        )


# ---------------------------------------------------------------------------
# Tests: AC5 and AC6 — crash recovery (require metadata.json fixture)
# ---------------------------------------------------------------------------


class TestCrashRecoveryAC5AC6:
    """AC5-AC6: Crash recovery via interrupted metadata works with and without drift."""

    def test_ac5_interrupted_metadata_triggers_reconcile_no_drift(self, tmp_path):
        """AC5: metadata.json status=in_progress → --reconcile when drift=None."""
        sched, golden_repos_dir = _make_scheduler(tmp_path)
        source_repo = _make_source_repo(golden_repos_dir)
        (source_repo / ".code-indexer" / "metadata.json").write_text(
            json.dumps({"status": "in_progress"})
        )

        captured = _run_execute_refresh_capture_popen(
            sched, source_repo, drift=None, scanner_result=False
        )
        assert "--reconcile" in _semantic_cmd(captured), (
            "AC5: interrupted metadata must trigger --reconcile regardless of drift"
        )

    def test_ac6_crash_and_drift_both_trigger_reconcile(self, tmp_path):
        """AC6: interrupted metadata + drift + files → --reconcile (OR logic)."""
        sched, golden_repos_dir = _make_scheduler(tmp_path)
        source_repo = _make_source_repo(golden_repos_dir)
        (source_repo / ".code-indexer" / "metadata.json").write_text(
            json.dumps({"status": "failed"})
        )

        captured = _run_execute_refresh_capture_popen(
            sched, source_repo, drift=_make_drift(added=["jsonl"]), scanner_result=True
        )
        assert "--reconcile" in _semantic_cmd(captured), (
            "AC6: OR logic must trigger --reconcile when both conditions apply"
        )


# ---------------------------------------------------------------------------
# Helper for AC7-AC9 (early-return bypass tests)
# ---------------------------------------------------------------------------


def _run_execute_refresh_no_changes(
    sched,
    alias_name: str,
    drift,
    scanner_result: bool,
    extra_patches: list,
) -> List[List[str]]:
    """Run _execute_refresh() with the standard drift patch stack plus extra patches.

    extra_patches: list of context managers entered by the helper via ExitStack.
    Use this to supply a per-scenario no-changes mock (e.g. GitPullUpdater.has_changes).

    Returns the list of popen command lists captured during the run.
    """
    import contextlib

    captured: List[List[str]] = []

    def mock_popen(**kwargs):
        captured.append(list(kwargs.get("command", [])))
        cwd = kwargs.get("cwd", "")
        (Path(cwd) / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)
        return 0

    with contextlib.ExitStack() as stack:
        mock_get_cs = stack.enter_context(
            patch("code_indexer.global_repos.refresh_scheduler.get_config_service")
        )
        mock_cs = MagicMock()
        mock_cs.sync_repo_extensions_if_drifted.return_value = drift
        mock_get_cs.return_value = mock_cs

        stack.enter_context(
            patch(
                "code_indexer.global_repos.refresh_scheduler.has_files_with_extensions",
                return_value=scanner_result,
            )
        )
        stack.enter_context(
            patch(
                "code_indexer.services.progress_subprocess_runner.run_with_popen_progress",
                side_effect=mock_popen,
            )
        )
        stack.enter_context(
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(0, 0),
            )
        )
        stack.enter_context(
            patch(
                "code_indexer.global_repos.refresh_scheduler.subprocess.run",
                return_value=MagicMock(returncode=0),
            )
        )
        for ctx in extra_patches:
            stack.enter_context(ctx)

        sched._execute_refresh(alias_name)

    return captured


# ---------------------------------------------------------------------------
# Tests: AC7-AC9 — early-return bypass fix
# ---------------------------------------------------------------------------


class TestEarlyReturnBypassFix:
    """AC7-AC9: drift check must fire BEFORE the early-return for no changes.

    Before the fix the drift block was placed after both early-return exits,
    so drift was never checked for unchanged repos.  These tests verify the
    corrected ordering after the production fix is applied.
    """

    def test_ac7_git_no_changes_drift_triggers_reconcile(self, tmp_path):
        """AC7: git repo, has_changes=False, drift + files → indexing proceeds with --reconcile."""
        sched, golden_repos_dir = _make_scheduler(tmp_path)
        _make_source_repo(golden_repos_dir)

        extra = [
            patch(
                "code_indexer.global_repos.git_pull_updater.GitPullUpdater.has_changes",
                return_value=False,
            )
        ]
        captured = _run_execute_refresh_no_changes(
            sched,
            alias_name="my-repo-global",
            drift=_make_drift(added=["jsonl"]),
            scanner_result=True,
            extra_patches=extra,
        )

        assert len(captured) > 0, (
            "AC7: expected indexing to proceed when drift detected despite no git changes, "
            "but no popen commands were captured (early return fired incorrectly)"
        )
        cmd = _semantic_cmd(captured)
        assert "--reconcile" in cmd, (
            f"AC7: expected --reconcile in cidx command but got: {cmd}"
        )

    def test_ac8_git_no_changes_no_drift_returns_early(self, tmp_path):
        """AC8: git repo, has_changes=False, no drift → early return preserved (no indexing)."""
        sched, golden_repos_dir = _make_scheduler(tmp_path)
        _make_source_repo(golden_repos_dir)

        extra = [
            patch(
                "code_indexer.global_repos.git_pull_updater.GitPullUpdater.has_changes",
                return_value=False,
            )
        ]
        captured = _run_execute_refresh_no_changes(
            sched,
            alias_name="my-repo-global",
            drift=None,
            scanner_result=False,
            extra_patches=extra,
        )

        fts_cmds = [c for c in captured if "--fts" in c]
        assert len(fts_cmds) == 0, (
            f"AC8: expected early return with no indexing when no drift and no git changes, "
            f"but popen was called with: {fts_cmds}"
        )

    def test_ac9_local_no_changes_drift_triggers_reconcile(self, tmp_path):
        """AC9: local repo, no visible files, drift + files → indexing proceeds with --reconcile.

        _has_local_changes() is driven through real code:
        - A .versioned/my-repo/v_<future_ts>/ dir exists so the 'no versioned dir'
          fast-path (which returns True) is bypassed.
        - The source dir has no non-hidden files, so the real method returns False
          via the 'no visible files' guard at line 2234 of refresh_scheduler.py.

        This puts the local early-return path into play, which is the bug: after the
        fix, drift detection must fire before that return.

        sched.registry is replaced with a local-URL stub.  The registry is a pure
        data-provider injected at build time; swapping it is equivalent to
        re-constructing with a different registry argument.
        """
        import time

        sched, golden_repos_dir = _make_scheduler(tmp_path)

        # Create source dir with only .code-indexer/ (hidden) — no visible files.
        source_repo = golden_repos_dir / "my-repo"
        source_repo.mkdir(parents=True, exist_ok=True)
        (source_repo / ".code-indexer").mkdir(exist_ok=True)

        # Create a versioned snapshot dir with a far-future timestamp so
        # _has_local_changes sees an existing version and proceeds to the mtime
        # comparison, which then returns False (no visible files).
        future_ts = int(time.time()) + 10_000
        versioned_dir = golden_repos_dir / ".versioned" / "my-repo" / f"v_{future_ts}"
        versioned_dir.mkdir(parents=True, exist_ok=True)

        class _LocalRegistryStub:
            def get_global_repo(self, alias_name: str) -> dict:
                return {
                    "repo_url": f"local://{source_repo}",
                    "default_branch": "main",
                    "enable_temporal": False,
                }

            def update_refresh_timestamp(self, alias_name: str) -> None:
                return None

        # Swap the pure data-provider — equivalent to re-constructing with a different registry.
        sched.registry = _LocalRegistryStub()

        captured = _run_execute_refresh_no_changes(
            sched,
            alias_name="my-repo-global",
            drift=_make_drift(added=["jsonl"]),
            scanner_result=True,
            extra_patches=[],
        )

        assert len(captured) > 0, (
            "AC9: expected indexing to proceed when drift detected despite no local changes, "
            "but no popen commands were captured (early return fired incorrectly)"
        )
        cmd = _semantic_cmd(captured)
        assert "--reconcile" in cmd, (
            f"AC9: expected --reconcile in cidx command but got: {cmd}"
        )
