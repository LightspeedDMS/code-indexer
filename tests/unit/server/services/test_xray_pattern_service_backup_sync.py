"""Tests for XrayPatternService._git_commit canonical backup pattern — Bug #1036.

Verifies that _git_commit:
1. Uses canonical cidx-meta-backup author/committer env vars
2. Triggers CidxMetaBackupSync.sync() after commit when backup is enabled
3. Surfaces sync failures via WARNING log (deferred-failure pattern)
4. Degrades cleanly when backup is disabled (commit happens, sync NOT called)
5. Uses self._cidx_meta as the git working directory (no ad-hoc path construction)

Uses a real git repo (no mocks of core git operations) — only mocks the
config service singleton and CidxMetaBackupSync.sync (external I/O).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared git helpers (same pattern as test_sync.py)
# ---------------------------------------------------------------------------

MINIMAL_EVALUATOR = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }"

MINIMAL_PATTERN_YAML = f"""\
name: my-pattern
description: "Test pattern"
language: java
evaluator_code: |
  {MINIMAL_EVALUATOR}
"""


def _git(args: list[str], cwd: Path, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )
    return result.stdout.strip()


def _init_git_repo(repo_path: Path) -> None:
    """Initialize a git repo with an initial commit so HEAD exists."""
    _git(["init"], repo_path)
    _git(["config", "user.email", "test@test.invalid"], repo_path)
    _git(["config", "user.name", "Test User"], repo_path)
    readme = repo_path / "README.md"
    readme.write_text("cidx-meta\n")
    _git(["add", "README.md"], repo_path)
    _git(["commit", "-m", "initial"], repo_path)


def _make_cidx_meta_git_repo(tmp_path: Path) -> Path:
    """Create a real git-initialized cidx-meta directory."""
    cidx_meta = tmp_path / "data" / "golden-repos" / "cidx-meta"
    cidx_meta.mkdir(parents=True, exist_ok=True)
    _init_git_repo(cidx_meta)
    return cidx_meta


def _make_config(enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        cidx_meta_backup_config=SimpleNamespace(enabled=enabled, remote_url=""),
    )


def _create_test_pattern_file(cidx_meta: Path) -> Path:
    """Create a test pattern file in cidx-meta and return its path."""
    pattern_dir = cidx_meta / "xray-patterns" / "__any__"
    pattern_dir.mkdir(parents=True, exist_ok=True)
    pattern_file = pattern_dir / "test-pattern.yaml"
    pattern_file.write_text(MINIMAL_PATTERN_YAML)
    return pattern_file


def _import_service():
    from code_indexer.server.services.xray_pattern_service import XrayPatternService

    return XrayPatternService


# ---------------------------------------------------------------------------
# Test 1: canonical author env vars
# ---------------------------------------------------------------------------


class TestGitCommitCanonicalAuthorEnvVars:
    """Bug #1036: _git_commit must use canonical cidx-meta-backup author."""

    def test_git_commit_uses_canonical_author_env_vars(self, tmp_path: Path) -> None:
        """git log must show cidx-meta-backup as author after _git_commit."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta_git_repo(tmp_path)
        service = XrayPatternService(cidx_meta)
        pattern_file = _create_test_pattern_file(cidx_meta)

        mock_config = _make_config(enabled=False)
        with patch(
            "code_indexer.server.services.xray_pattern_service.get_config_service"
        ) as mock_get_cfg:
            mock_get_cfg.return_value.get_config.return_value = mock_config
            service._git_commit(files=[pattern_file], message="test commit")

        author_line = _git(["log", "-1", "--format=%an %ae"], cidx_meta)
        assert author_line == "cidx-meta-backup cidx-meta-backup@example.invalid", (
            f"Expected canonical author, got: {author_line!r}"
        )


# ---------------------------------------------------------------------------
# Test 2: CidxMetaBackupSync.sync() triggered after commit
# ---------------------------------------------------------------------------


class TestGitCommitTriggersSyncAfterCommit:
    """Bug #1036: _git_commit must call CidxMetaBackupSync.sync() when enabled."""

    def test_git_commit_triggers_CidxMetaBackupSync_after_commit(
        self, tmp_path: Path
    ) -> None:
        """CidxMetaBackupSync.sync must be called exactly once after commit."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta_git_repo(tmp_path)
        service = XrayPatternService(cidx_meta)
        pattern_file = _create_test_pattern_file(cidx_meta)

        mock_config = _make_config(enabled=True)
        mock_sync_instance = MagicMock()
        mock_sync_instance.sync.return_value = SimpleNamespace(
            skipped=False, sync_failure=None
        )

        with patch(
            "code_indexer.server.services.xray_pattern_service.get_config_service"
        ) as mock_get_cfg:
            mock_get_cfg.return_value.get_config.return_value = mock_config
            with patch(
                "code_indexer.server.services.xray_pattern_service.CidxMetaBackupSync"
            ) as MockSync:
                MockSync.return_value = mock_sync_instance
                service._git_commit(files=[pattern_file], message="test commit")

        mock_sync_instance.sync.assert_called_once()


# ---------------------------------------------------------------------------
# Test 3: sync failure surfaced via WARNING log
# ---------------------------------------------------------------------------


class TestGitCommitSurfacesSyncFailure:
    """Bug #1036: sync failures must appear as WARNING log, not be silently swallowed."""

    def test_git_commit_surfaces_sync_failure_via_warning_log(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When sync returns sync_failure, a WARNING must be logged."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta_git_repo(tmp_path)
        service = XrayPatternService(cidx_meta)
        pattern_file = _create_test_pattern_file(cidx_meta)

        mock_config = _make_config(enabled=True)
        mock_sync_instance = MagicMock()
        mock_sync_instance.sync.return_value = SimpleNamespace(
            skipped=False, sync_failure="push failed: connection refused"
        )

        with caplog.at_level(logging.WARNING, logger="code_indexer"):
            with patch(
                "code_indexer.server.services.xray_pattern_service.get_config_service"
            ) as mock_get_cfg:
                mock_get_cfg.return_value.get_config.return_value = mock_config
                with patch(
                    "code_indexer.server.services.xray_pattern_service.CidxMetaBackupSync"
                ) as MockSync:
                    MockSync.return_value = mock_sync_instance
                    service._git_commit(files=[pattern_file], message="test commit")

        warning_messages = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any("push failed" in str(m) for m in warning_messages), (
            f"Expected WARNING mentioning sync failure, got: {warning_messages}"
        )


# ---------------------------------------------------------------------------
# Test 4: backup disabled — commit happens, sync NOT called
# ---------------------------------------------------------------------------


class TestGitCommitDegradesWhenBackupDisabled:
    """Bug #1036: when backup disabled, commit happens but sync is skipped."""

    def test_git_commit_completes_in_isolation_when_backup_disabled(
        self, tmp_path: Path
    ) -> None:
        """When cidx_meta_backup_config.enabled=False: commit OK, sync NOT called."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta_git_repo(tmp_path)
        service = XrayPatternService(cidx_meta)
        pattern_file = _create_test_pattern_file(cidx_meta)

        mock_config = _make_config(enabled=False)

        with patch(
            "code_indexer.server.services.xray_pattern_service.get_config_service"
        ) as mock_get_cfg:
            mock_get_cfg.return_value.get_config.return_value = mock_config
            with patch(
                "code_indexer.server.services.xray_pattern_service.CidxMetaBackupSync"
            ) as MockSync:
                service._git_commit(files=[pattern_file], message="test commit")
                MockSync.assert_not_called()

        # Verify the commit actually happened
        log_output = _git(["log", "--oneline"], cidx_meta)
        assert "test commit" in log_output


# ---------------------------------------------------------------------------
# Test 5: _git_commit uses self._cidx_meta as working directory
# ---------------------------------------------------------------------------


class TestGitCommitUsesSelfCidxMetaPath:
    """Bug #1036: _git_commit must use self._cidx_meta (no ad-hoc path construction)."""

    def test_git_commit_uses_cidx_meta_as_cwd(self, tmp_path: Path) -> None:
        """CidxMetaBackupSync is constructed with str(self._cidx_meta) as first arg."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta_git_repo(tmp_path)
        service = XrayPatternService(cidx_meta)
        pattern_file = _create_test_pattern_file(cidx_meta)

        mock_config = _make_config(enabled=True)
        mock_sync_instance = MagicMock()
        mock_sync_instance.sync.return_value = SimpleNamespace(
            skipped=False, sync_failure=None
        )

        captured_calls: list = []  # list of (args, kwargs) tuples

        def capture_sync_init(*args, **kwargs):
            captured_calls.append((args, kwargs))
            return mock_sync_instance

        with patch(
            "code_indexer.server.services.xray_pattern_service.get_config_service"
        ) as mock_get_cfg:
            mock_get_cfg.return_value.get_config.return_value = mock_config
            with patch(
                "code_indexer.server.services.xray_pattern_service.CidxMetaBackupSync",
                side_effect=capture_sync_init,
            ):
                service._git_commit(files=[pattern_file], message="test commit")

        assert len(captured_calls) >= 1, "CidxMetaBackupSync was not instantiated"
        init_args, init_kwargs = captured_calls[0]
        # Production passes cidx_meta_path as a keyword argument
        actual_path = init_kwargs.get("cidx_meta_path") or (
            init_args[0] if init_args else None
        )
        assert str(cidx_meta) == actual_path, (
            f"Expected CidxMetaBackupSync cidx_meta_path {cidx_meta!r}, got {actual_path!r}"
        )
