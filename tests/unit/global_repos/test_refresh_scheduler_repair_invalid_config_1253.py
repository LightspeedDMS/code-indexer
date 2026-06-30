"""
Unit tests for Bug #1253: RefreshScheduler self-heals a local repo whose
.code-indexer/ directory exists but has no valid config.json.

Root cause: golden_repo_manager.register_local_repo() runs
`cidx init --no-override-file` exactly once per alias (Phase 3 registration
race / partial-failure path). `cidx init` creates the `.code-indexer/`
directory via ConfigManager.save_with_documentation()'s
`self.config_path.parent.mkdir(parents=True, exist_ok=True)` BEFORE writing
config.json. If anything fails between that mkdir and the config.json write
completing (or config.json is later truncated/corrupted), the
`subprocess.CalledProcessError` is caught in register_local_repo() and only
logged -- registration "continues" and the broken golden repo / global alias
is registered anyway. Because registration is idempotent per-alias (never
retried), the repo is now PERMANENTLY stuck with an existing-but-invalid
.code-indexer/ directory.

The pre-existing Bug #268 guard in `_execute_refresh()` only checks
`code_indexer_dir.exists()`. For a directory that exists but lacks a valid
config.json, the guard does NOT fire, so `cidx index` is invoked and fails
every single scheduled cycle with:
    "Command 'index' is not available in no configuration found - project
    needs initialization."
This matches the 231 recurring staging failures for
langfuse_Claude_Code_*-global repos.

Fix: extend the local-repo branch of `_execute_refresh()` to validate
config.json (mirrors CommandModeDetector._validate_local_config's "exists
and is valid JSON" check). When `.code-indexer/` exists but config.json is
missing or corrupt, self-heal by re-running `cidx init --no-override-file
--force` before proceeding with the normal indexing flow, instead of
failing the same way forever.

Acceptance Criteria:
AC1: .code-indexer/ exists but config.json missing -> repair subprocess is
     invoked; on success, refresh proceeds normally (no exception, no
     "Not yet initialized" skip).
AC2: .code-indexer/ exists but config.json is corrupt (invalid JSON) ->
     same repair-then-proceed behavior.
AC3: Repair subprocess failure -> _execute_refresh() returns
     success=False with an informative message, never raises.
AC4: .code-indexer/ exists with a VALID config.json -> repair subprocess is
     NEVER invoked (no regression for the healthy/common case).
AC5: .code-indexer/ missing entirely -> unchanged Bug #268 graceful skip
     behavior (no repair attempted; repo is genuinely brand new).
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def golden_repos_dir(tmp_path):
    d = tmp_path / "golden-repos"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def mock_registry():
    registry = MagicMock()
    registry.list_global_repos.return_value = []
    registry.get_global_repo.return_value = None
    registry.update_refresh_timestamp = MagicMock()
    registry.update_enable_temporal = MagicMock()
    registry.update_enable_scip = MagicMock()
    return registry


@pytest.fixture
def mock_config_source():
    cs = MagicMock()
    cs.get_global_refresh_interval.return_value = 3600
    return cs


@pytest.fixture
def scheduler(golden_repos_dir, mock_registry, mock_config_source):
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=mock_config_source,
        query_tracker=MagicMock(spec=QueryTracker),
        cleanup_manager=MagicMock(spec=CleanupManager),
        registry=mock_registry,
    )


def _make_repo_info(alias_name: str, repo_url: str = "local://langfuse-user"):
    return {
        "alias_name": alias_name,
        "repo_url": repo_url,
        "enable_temporal": False,
        "enable_scip": False,
    }


# ---------------------------------------------------------------------------
# AC1: missing config.json -> self-heal via cidx init, then proceed
# ---------------------------------------------------------------------------


class TestMissingConfigJsonIsSelfHealed:
    def test_repair_subprocess_invoked_and_refresh_proceeds(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        alias_name = "langfuse-user-global"
        source_dir = golden_repos_dir / "langfuse-user"
        source_dir.mkdir(parents=True)
        # .code-indexer/ exists (partial init left this behind) but config.json
        # was never written -- this is the broken state from Bug #1253.
        (source_dir / ".code-indexer").mkdir()

        repo_info = _make_repo_info(alias_name)
        mock_registry.get_global_repo.return_value = repo_info
        scheduler.alias_manager.read_alias = MagicMock(return_value=str(source_dir))

        repair_calls = []

        def fake_run(cmd, **kwargs):
            repair_calls.append((cmd, kwargs.get("cwd")))
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch(
            "code_indexer.global_repos.refresh_scheduler.subprocess.run",
            side_effect=fake_run,
        ):
            with patch.object(scheduler, "_has_local_changes", return_value=False):
                with patch.object(
                    scheduler, "_detect_existing_indexes", return_value={}
                ):
                    with patch.object(scheduler, "_reconcile_registry_with_filesystem"):
                        result = scheduler._execute_refresh(alias_name)

        assert len(repair_calls) == 1, (
            "AC1: cidx init must be invoked exactly once to repair the "
            "missing config.json."
        )
        cmd, cwd = repair_calls[0]
        assert cmd[:2] == ["cidx", "init"], f"Expected 'cidx init' command, got {cmd}"
        assert cwd == str(source_dir)

        assert result["success"] is True, (
            f"AC1: refresh must proceed (not skip/fail) after a successful "
            f"repair. Got: {result}"
        )
        # Must NOT be the Bug #268 "not yet initialized" skip message --
        # the repair should let it fall through to the normal flow.
        assert "not yet initialized" not in result.get("message", "").lower()


# ---------------------------------------------------------------------------
# AC2: corrupt config.json -> same self-heal behavior
# ---------------------------------------------------------------------------


class TestCorruptConfigJsonIsSelfHealed:
    def test_repair_subprocess_invoked_for_corrupt_json(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        alias_name = "langfuse-user-global"
        source_dir = golden_repos_dir / "langfuse-user"
        source_dir.mkdir(parents=True)
        code_indexer_dir = source_dir / ".code-indexer"
        code_indexer_dir.mkdir()
        # Corrupt / truncated config.json (e.g. a concurrent-write race)
        (code_indexer_dir / "config.json").write_text("{not valid json")

        repo_info = _make_repo_info(alias_name)
        mock_registry.get_global_repo.return_value = repo_info
        scheduler.alias_manager.read_alias = MagicMock(return_value=str(source_dir))

        repair_calls = []

        def fake_run(cmd, **kwargs):
            repair_calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch(
            "code_indexer.global_repos.refresh_scheduler.subprocess.run",
            side_effect=fake_run,
        ):
            with patch.object(scheduler, "_has_local_changes", return_value=False):
                with patch.object(
                    scheduler, "_detect_existing_indexes", return_value={}
                ):
                    with patch.object(scheduler, "_reconcile_registry_with_filesystem"):
                        result = scheduler._execute_refresh(alias_name)

        assert len(repair_calls) == 1, (
            "AC2: cidx init must be invoked exactly once to repair the "
            "corrupt config.json."
        )
        assert result["success"] is True


# ---------------------------------------------------------------------------
# AC3: repair failure -> graceful failure result, never raises
# ---------------------------------------------------------------------------


class TestRepairFailureIsGraceful:
    def test_repair_failure_returns_success_false_without_raising(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        alias_name = "langfuse-user-global"
        source_dir = golden_repos_dir / "langfuse-user"
        source_dir.mkdir(parents=True)
        (source_dir / ".code-indexer").mkdir()
        # No config.json -- repair will be attempted and fail.

        repo_info = _make_repo_info(alias_name)
        mock_registry.get_global_repo.return_value = repo_info
        scheduler.alias_manager.read_alias = MagicMock(return_value=str(source_dir))

        def fake_run(cmd, **kwargs):
            raise subprocess.CalledProcessError(
                returncode=1, cmd=cmd, output="", stderr="disk full"
            )

        with patch(
            "code_indexer.global_repos.refresh_scheduler.subprocess.run",
            side_effect=fake_run,
        ):
            with patch.object(scheduler, "_detect_existing_indexes", return_value={}):
                with patch.object(scheduler, "_reconcile_registry_with_filesystem"):
                    try:
                        result = scheduler._execute_refresh(alias_name)
                    except Exception as exc:  # pragma: no cover - failure path
                        pytest.fail(
                            f"AC3: _execute_refresh() must not raise when repair "
                            f"fails, got {type(exc).__name__}: {exc}"
                        )

        assert result["success"] is False, (
            f"AC3: a failed repair must surface as success=False, not be "
            f"silently swallowed as success. Got: {result}"
        )
        assert result["alias"] == alias_name


# ---------------------------------------------------------------------------
# AC4: valid config.json -> repair never invoked (no regression)
# ---------------------------------------------------------------------------


class TestValidConfigNeverTriggersRepair:
    def test_repair_not_invoked_for_valid_config(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        alias_name = "cidx-meta-global"
        source_dir = golden_repos_dir / "cidx-meta"
        source_dir.mkdir(parents=True)
        code_indexer_dir = source_dir / ".code-indexer"
        code_indexer_dir.mkdir()
        (code_indexer_dir / "config.json").write_text('{"codebase_dir": "."}')

        repo_info = _make_repo_info(alias_name, repo_url="local://cidx-meta")
        mock_registry.get_global_repo.return_value = repo_info
        scheduler.alias_manager.read_alias = MagicMock(return_value=str(source_dir))

        repair_calls = []

        def fake_run(cmd, **kwargs):
            repair_calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch(
            "code_indexer.global_repos.refresh_scheduler.subprocess.run",
            side_effect=fake_run,
        ):
            with patch.object(scheduler, "_has_local_changes", return_value=False):
                with patch.object(
                    scheduler, "_detect_existing_indexes", return_value={}
                ):
                    with patch.object(scheduler, "_reconcile_registry_with_filesystem"):
                        result = scheduler._execute_refresh(alias_name)

        assert repair_calls == [], (
            "AC4: cidx init repair must NOT be invoked when config.json is "
            "already valid -- this would be an unnecessary subprocess on "
            "every single healthy refresh cycle."
        )
        assert result["success"] is True


# ---------------------------------------------------------------------------
# AC5: .code-indexer/ missing entirely -> unchanged Bug #268 behavior
# ---------------------------------------------------------------------------


class TestMissingCodeIndexerDirStillSkipsGracefully:
    def test_no_repair_attempted_when_directory_absent(
        self, scheduler, golden_repos_dir, mock_registry
    ):
        alias_name = "langfuse-user-global"
        source_dir = golden_repos_dir / "langfuse-user"
        source_dir.mkdir(parents=True)
        # Deliberately no .code-indexer/ at all -- genuinely brand new repo.

        repo_info = _make_repo_info(alias_name)
        mock_registry.get_global_repo.return_value = repo_info
        scheduler.alias_manager.read_alias = MagicMock(return_value=str(source_dir))

        repair_calls = []

        def fake_run(cmd, **kwargs):
            repair_calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch(
            "code_indexer.global_repos.refresh_scheduler.subprocess.run",
            side_effect=fake_run,
        ):
            with patch.object(scheduler, "_detect_existing_indexes", return_value={}):
                with patch.object(scheduler, "_reconcile_registry_with_filesystem"):
                    result = scheduler._execute_refresh(alias_name)

        assert repair_calls == [], (
            "AC5: a genuinely uninitialized repo (no .code-indexer/ at all) "
            "must NOT trigger a repair attempt -- Bug #268's original "
            "'writer hasn't populated it yet' semantics are unchanged."
        )
        assert result["success"] is True
        assert "not yet initialized" in result.get("message", "").lower()
