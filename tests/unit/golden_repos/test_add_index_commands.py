"""
Unit tests for Bug #468: add_index semantic rebuild completes instantly without rebuilding.

Root cause: add_index_to_golden_repo runs ["cidx", "index"] without --clear for semantic
index type, which is a no-op for already-indexed repos.

Fix: semantic must use ["cidx", "index", "--clear"] to force a full rebuild.

Also covers Story #478: Fix Temporal Index Rebuild and Expose Temporal Options in Web UI.

Acceptance criteria:
- semantic index_type issues ["cidx", "index", "--clear"]
- fts index_type issues ["cidx", "index", "--rebuild-fts-index"] (unchanged, verified)
- temporal index_type issues ["cidx", "index", "--index-commits", "--clear"] (AC1)
- temporal with no options does NOT include "--max-commits" (AC2)
- temporal with options applies max_commits, diff_context, since_date, all_branches (AC4)
- RefreshScheduler temporal command applies all_branches option (AC5)
"""

from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_success_run(repo_path):
    """Return a subprocess.run mock that always succeeds."""

    def mock_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = "ok"
        result.stderr = ""
        return result

    return mock_run


def _make_success_popen(collected_cmds):
    """
    Return a subprocess.Popen mock class that:
    - Records the command in collected_cmds
    - Returns a process with empty stdout, empty stderr, returncode=0

    Story #480: semantic and temporal now use Popen + --progress-json
    for real-time progress line reading.
    """

    class MockProcess:
        def __init__(self, cmd, **kwargs):
            collected_cmds.append(list(cmd))
            self.stdout = StringIO("")  # Empty: no JSON progress lines
            self.stderr = StringIO("")
            self.returncode = 0

        def wait(self):
            pass

    return MockProcess


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def manager(tmp_path):
    """Minimal GoldenRepoManager backed by a temp dir."""
    mgr = GoldenRepoManager(data_dir=str(tmp_path))
    return mgr


@pytest.fixture
def repo_path(tmp_path):
    """A fake repo directory that exists on disk."""
    p = tmp_path / "repos" / "test-repo"
    p.mkdir(parents=True)
    return p


@pytest.fixture
def registered_manager(manager, repo_path):
    """GoldenRepoManager with one repo registered and a mock background_job_manager."""
    from datetime import datetime, timezone
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepo

    repo = GoldenRepo(
        alias="test-repo",
        repo_url="git@github.com:org/test-repo.git",
        clone_path=str(repo_path),
        default_branch="main",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    manager.golden_repos["test-repo"] = repo

    # background_job_manager is an externally-set field (not initialized in __init__).
    # Inject a mock so add_index_to_golden_repo can call submit_job.
    manager.background_job_manager = MagicMock()
    return manager


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAddIndexSemanticCommand:
    """Bug #468: semantic add_index must issue cidx index --clear."""

    def _capture_subprocess_calls(self, registered_manager, repo_path, index_type):
        """
        Call add_index_to_golden_repo and capture every subprocess.run / Popen invocation.

        The method submits a background job via BackgroundJobManager.  We
        intercept submit_job to run the worker function synchronously so we can
        capture subprocess calls without threading complexity.

        Story #480: semantic and temporal now use subprocess.Popen with --progress-json.
        Both subprocess.run AND subprocess.Popen are patched so all commands are captured.
        """
        captured_cmds = []

        def fake_submit_job(operation_type, func, **kwargs):
            """Run the background worker synchronously and capture subprocess calls."""
            collected = []

            def recording_run(cmd, **kw):
                collected.append(list(cmd))
                r = MagicMock()
                r.returncode = 0
                r.stdout = "ok"
                r.stderr = ""
                return r

            # Also patch get_actual_repo_path so it returns our temp path.
            with (
                patch("subprocess.run", side_effect=recording_run),
                patch("subprocess.Popen", side_effect=_make_success_popen(collected)),
                patch.object(
                    registered_manager,
                    "get_actual_repo_path",
                    return_value=str(repo_path),
                ),
            ):
                func()
            captured_cmds.extend(collected)
            return "fake-job-id"

        with patch.object(
            registered_manager.background_job_manager,
            "submit_job",
            side_effect=fake_submit_job,
        ):
            registered_manager.add_index_to_golden_repo(
                alias="test-repo",
                index_type=index_type,
            )

        return captured_cmds

    def test_semantic_index_uses_clear_flag(self, registered_manager, repo_path):
        """
        Bug #468: cidx index for semantic rebuild must include --clear.

        Without --clear, cidx index is incremental and is a no-op for an
        already-indexed repository.  A rebuild must wipe and re-embed.
        """
        cmds = self._capture_subprocess_calls(registered_manager, repo_path, "semantic")

        # Find the cidx index command (not the cidx init command)
        index_cmds = [c for c in cmds if c[:2] == ["cidx", "index"]]
        assert index_cmds, (
            "No 'cidx index' command was issued for semantic index type. "
            f"All captured commands: {cmds}"
        )

        semantic_cmd = index_cmds[0]
        assert "--clear" in semantic_cmd, (
            f"Bug #468: 'cidx index' for semantic must include '--clear' to force "
            f"full rebuild.  Got command: {semantic_cmd}. "
            "Without --clear the command is a no-op for already-indexed repos."
        )

    def test_semantic_index_does_not_use_rebuild_fts_flag(
        self, registered_manager, repo_path
    ):
        """Semantic rebuild must NOT include --rebuild-fts-index (that is FTS only)."""
        cmds = self._capture_subprocess_calls(registered_manager, repo_path, "semantic")

        index_cmds = [c for c in cmds if c[:2] == ["cidx", "index"]]
        assert index_cmds, f"No 'cidx index' command captured. All commands: {cmds}"

        semantic_cmd = index_cmds[0]
        assert "--rebuild-fts-index" not in semantic_cmd, (
            f"Semantic rebuild must not include --rebuild-fts-index. "
            f"Got: {semantic_cmd}"
        )


class TestAddIndexFtsCommand:
    """Verify fts index_type is unchanged and uses --rebuild-fts-index (not --clear)."""

    def _capture_subprocess_calls(self, registered_manager, repo_path, index_type):
        """Same helper pattern as TestAddIndexSemanticCommand.

        Story #480: also patches subprocess.Popen for semantic/temporal Popen path.
        FTS itself uses subprocess.run but the shared infrastructure may use Popen.
        """
        captured_cmds = []

        def fake_submit_job(operation_type, func, **kwargs):
            collected = []

            def recording_run(cmd, **kw):
                collected.append(list(cmd))
                r = MagicMock()
                r.returncode = 0
                r.stdout = "ok"
                r.stderr = ""
                return r

            with (
                patch("subprocess.run", side_effect=recording_run),
                patch("subprocess.Popen", side_effect=_make_success_popen(collected)),
                patch.object(
                    registered_manager,
                    "get_actual_repo_path",
                    return_value=str(repo_path),
                ),
            ):
                func()
            captured_cmds.extend(collected)
            return "fake-job-id"

        with patch.object(
            registered_manager.background_job_manager,
            "submit_job",
            side_effect=fake_submit_job,
        ):
            registered_manager.add_index_to_golden_repo(
                alias="test-repo",
                index_type=index_type,
            )

        return captured_cmds

    def test_fts_index_uses_rebuild_fts_index_flag(self, registered_manager, repo_path):
        """
        FTS index_type must issue cidx index --rebuild-fts-index.

        This verifies that the existing fts behavior is correct and unchanged.
        """
        cmds = self._capture_subprocess_calls(registered_manager, repo_path, "fts")

        index_cmds = [c for c in cmds if c[:2] == ["cidx", "index"]]
        assert (
            index_cmds
        ), f"No 'cidx index' command issued for fts index type. Commands: {cmds}"

        fts_cmd = index_cmds[0]
        assert (
            "--rebuild-fts-index" in fts_cmd
        ), f"FTS index must use '--rebuild-fts-index'. Got: {fts_cmd}"

    def test_fts_index_does_not_use_clear_flag(self, registered_manager, repo_path):
        """FTS rebuild must NOT include --clear (that is for semantic only)."""
        cmds = self._capture_subprocess_calls(registered_manager, repo_path, "fts")

        index_cmds = [c for c in cmds if c[:2] == ["cidx", "index"]]
        assert index_cmds, f"No 'cidx index' command captured. Commands: {cmds}"

        fts_cmd = index_cmds[0]
        assert (
            "--clear" not in fts_cmd
        ), f"FTS rebuild must not include --clear. Got: {fts_cmd}"


# ---------------------------------------------------------------------------
# Story #478: Temporal index rebuild command fixes (AC1, AC2, AC4)
# ---------------------------------------------------------------------------


class TestAddIndexTemporalCommand:
    """
    Story #478: Temporal rebuild command must use --clear and respect options.

    AC1: admin-triggered temporal rebuild includes --clear for full rebuild.
    AC2: no magic max_commits=1000 default when temporal_options is None/empty.
    AC4: stored temporal_options (max_commits, diff_context, since_date, all_branches)
         are applied to the command.
    """

    def _capture_temporal_commands(
        self, registered_manager, repo_path, temporal_options=None
    ):
        """
        Call add_index_to_golden_repo with index_type='temporal' and capture
        the cidx index commands that are issued.
        """
        registered_manager.golden_repos["test-repo"].temporal_options = temporal_options

        captured_cmds = []

        def fake_submit_job(operation_type, func, **kwargs):
            collected = []

            def recording_run(cmd, **kw):
                collected.append(list(cmd))
                r = MagicMock()
                r.returncode = 0
                r.stdout = "ok"
                r.stderr = ""
                return r

            # Story #480: temporal now uses subprocess.Popen with --progress-json.
            # Patch both run and Popen so all commands are captured for verification.
            with (
                patch("subprocess.run", side_effect=recording_run),
                patch("subprocess.Popen", side_effect=_make_success_popen(collected)),
                patch.object(
                    registered_manager,
                    "get_actual_repo_path",
                    return_value=str(repo_path),
                ),
            ):
                func()
            captured_cmds.extend(collected)
            return "fake-job-id"

        with patch.object(
            registered_manager.background_job_manager,
            "submit_job",
            side_effect=fake_submit_job,
        ):
            registered_manager.add_index_to_golden_repo(
                alias="test-repo",
                index_type="temporal",
            )

        return [c for c in captured_cmds if c[:2] == ["cidx", "index"]]

    def test_temporal_index_uses_clear_flag(self, registered_manager, repo_path):
        """
        AC1: cidx index for temporal rebuild must include --clear.

        Without --clear, the temporal rebuild is incremental (no-op for already-indexed
        repos). Admin-triggered rebuild must always wipe and re-index all commits.
        """
        cmds = self._capture_temporal_commands(registered_manager, repo_path)

        assert cmds, "No 'cidx index' command issued for temporal index type."
        temporal_cmd = cmds[0]
        assert (
            "--clear" in temporal_cmd
        ), f"AC1: temporal rebuild must include '--clear'. Got: {temporal_cmd}"

    def test_temporal_index_uses_index_commits_flag(
        self, registered_manager, repo_path
    ):
        """AC1: cidx index for temporal must include --index-commits."""
        cmds = self._capture_temporal_commands(registered_manager, repo_path)

        assert cmds, "No 'cidx index' command issued for temporal index type."
        temporal_cmd = cmds[0]
        assert (
            "--index-commits" in temporal_cmd
        ), f"AC1: temporal rebuild must include '--index-commits'. Got: {temporal_cmd}"

    def test_temporal_no_max_commits_when_options_none(
        self, registered_manager, repo_path
    ):
        """
        AC2: when temporal_options is None, must NOT include --max-commits.

        Previously had a magic default of 1000 which silently capped all temporal
        indexes. When no limit is configured, the CLI should index all commits.
        """
        cmds = self._capture_temporal_commands(
            registered_manager, repo_path, temporal_options=None
        )

        assert cmds, "No 'cidx index' command issued for temporal index type."
        temporal_cmd = cmds[0]
        assert (
            "--max-commits" not in temporal_cmd
        ), f"AC2: no max_commits when temporal_options is None. Got: {temporal_cmd}"

    def test_temporal_no_max_commits_when_options_empty(
        self, registered_manager, repo_path
    ):
        """AC2: when temporal_options is an empty dict, must NOT include --max-commits."""
        cmds = self._capture_temporal_commands(
            registered_manager, repo_path, temporal_options={}
        )

        assert cmds, "No 'cidx index' command issued for temporal index type."
        temporal_cmd = cmds[0]
        assert (
            "--max-commits" not in temporal_cmd
        ), f"AC2: no max_commits for empty temporal_options. Got: {temporal_cmd}"

    def test_temporal_with_max_commits_option(self, registered_manager, repo_path):
        """AC4: when temporal_options has max_commits, the command includes --max-commits N."""
        cmds = self._capture_temporal_commands(
            registered_manager, repo_path, temporal_options={"max_commits": 500}
        )

        assert cmds, "No 'cidx index' command issued."
        temporal_cmd = cmds[0]
        assert (
            "--max-commits" in temporal_cmd
        ), f"AC4: max_commits=500 must produce '--max-commits'. Got: {temporal_cmd}"
        idx = temporal_cmd.index("--max-commits")
        assert (
            temporal_cmd[idx + 1] == "500"
        ), f"AC4: '--max-commits' must be followed by '500'. Got: {temporal_cmd}"

    def test_temporal_with_diff_context_option(self, registered_manager, repo_path):
        """AC4: when temporal_options has diff_context, the command includes --diff-context N."""
        cmds = self._capture_temporal_commands(
            registered_manager, repo_path, temporal_options={"diff_context": 10}
        )

        assert cmds, "No 'cidx index' command issued."
        temporal_cmd = cmds[0]
        assert (
            "--diff-context" in temporal_cmd
        ), f"AC4: diff_context=10 must produce '--diff-context'. Got: {temporal_cmd}"
        idx = temporal_cmd.index("--diff-context")
        assert temporal_cmd[idx + 1] == "10"

    def test_temporal_with_since_date_option(self, registered_manager, repo_path):
        """AC4: when temporal_options has since_date, the command includes --since YYYY-MM-DD."""
        cmds = self._capture_temporal_commands(
            registered_manager,
            repo_path,
            temporal_options={"since_date": "2024-01-01"},
        )

        assert cmds, "No 'cidx index' command issued."
        temporal_cmd = cmds[0]
        assert (
            "--since-date" in temporal_cmd
        ), f"AC4: since_date='2024-01-01' must produce '--since-date'. Got: {temporal_cmd}"
        idx = temporal_cmd.index("--since-date")
        assert temporal_cmd[idx + 1] == "2024-01-01"

    def test_temporal_with_all_branches_option(self, registered_manager, repo_path):
        """AC4: when temporal_options has all_branches=True, command includes --all-branches."""
        cmds = self._capture_temporal_commands(
            registered_manager,
            repo_path,
            temporal_options={"all_branches": True},
        )

        assert cmds, "No 'cidx index' command issued."
        temporal_cmd = cmds[0]
        assert (
            "--all-branches" in temporal_cmd
        ), f"AC4: all_branches=True must produce '--all-branches'. Got: {temporal_cmd}"

    def test_temporal_all_branches_false_omits_flag(
        self, registered_manager, repo_path
    ):
        """AC4: when all_branches=False, --all-branches must NOT appear."""
        cmds = self._capture_temporal_commands(
            registered_manager,
            repo_path,
            temporal_options={"all_branches": False},
        )

        assert cmds, "No 'cidx index' command issued."
        temporal_cmd = cmds[0]
        assert (
            "--all-branches" not in temporal_cmd
        ), f"AC4: all_branches=False must NOT produce '--all-branches'. Got: {temporal_cmd}"

    def test_temporal_full_options_command(self, registered_manager, repo_path):
        """
        AC4: all options together produce the correct full command.

        max_commits=500, diff_context=10, all_branches=True should produce:
          cidx index --index-commits --clear --max-commits 500 --diff-context 10 --all-branches
        """
        cmds = self._capture_temporal_commands(
            registered_manager,
            repo_path,
            temporal_options={
                "max_commits": 500,
                "diff_context": 10,
                "all_branches": True,
            },
        )

        assert cmds, "No 'cidx index' command issued."
        temporal_cmd = cmds[0]

        for flag in (
            "--index-commits",
            "--clear",
            "--max-commits",
            "--diff-context",
            "--all-branches",
        ):
            assert flag in temporal_cmd, f"Missing {flag}. Got: {temporal_cmd}"

        assert temporal_cmd[temporal_cmd.index("--max-commits") + 1] == "500"
        assert temporal_cmd[temporal_cmd.index("--diff-context") + 1] == "10"


# ---------------------------------------------------------------------------
# Story #478 AC3: TemporalIndexOptions model must include all_branches field
# ---------------------------------------------------------------------------


class TestTemporalIndexOptionsModel:
    """
    Story #478 AC3: TemporalIndexOptions Pydantic model must include all_branches.

    The Web UI temporal options form (AC3) requires an all_branches checkbox.
    The model must support this field so saved options can be validated and
    serialized correctly when rebuilding or scheduling temporal indexes.
    """

    def test_model_has_all_branches_field(self):
        """TemporalIndexOptions must have an all_branches field."""
        from code_indexer.server.models.api_models import TemporalIndexOptions

        opts = TemporalIndexOptions()
        assert hasattr(
            opts, "all_branches"
        ), "AC3: TemporalIndexOptions must have 'all_branches' field"

    def test_model_all_branches_defaults_to_false(self):
        """all_branches must default to False when not specified."""
        from code_indexer.server.models.api_models import TemporalIndexOptions

        opts = TemporalIndexOptions()
        assert (
            opts.all_branches is False
        ), f"AC3: all_branches must default to False. Got: {opts.all_branches}"

    def test_model_all_branches_can_be_set_true(self):
        """all_branches can be set to True."""
        from code_indexer.server.models.api_models import TemporalIndexOptions

        opts = TemporalIndexOptions(all_branches=True)
        assert opts.all_branches is True

    def test_model_max_commits_none_by_default(self):
        """max_commits must default to None (no cap by default)."""
        from code_indexer.server.models.api_models import TemporalIndexOptions

        opts = TemporalIndexOptions()
        assert opts.max_commits is None

    def test_model_diff_context_defaults_to_5(self):
        """diff_context must default to 5."""
        from code_indexer.server.models.api_models import TemporalIndexOptions

        opts = TemporalIndexOptions()
        assert opts.diff_context == 5


# ---------------------------------------------------------------------------
# Story #478: SQLite persistence of temporal_options per golden repo
# ---------------------------------------------------------------------------


class TestTemporalOptionsPersistence:
    """
    Story #478: temporal_options must be persisted and loaded via SQLite backend.

    Tests that GoldenRepoMetadataSqliteBackend.update_temporal_options exists and
    correctly writes/reads JSON-encoded temporal options for a golden repo.
    """

    def _make_backend(self, tmp_path):
        from code_indexer.server.storage.sqlite_backends import (
            GoldenRepoMetadataSqliteBackend,
        )

        backend = GoldenRepoMetadataSqliteBackend(str(tmp_path / "test.db"))
        backend.ensure_table_exists()
        return backend

    def _add_repo(self, backend, tmp_path, alias="my-repo"):
        backend.add_repo(
            alias=alias,
            repo_url=f"git@github.com:org/{alias}.git",
            default_branch="main",
            clone_path=str(tmp_path / alias),
            created_at="2024-01-01T00:00:00Z",
        )

    def test_update_temporal_options_method_exists(self, tmp_path):
        """GoldenRepoMetadataSqliteBackend must have update_temporal_options method."""
        backend = self._make_backend(tmp_path)
        assert hasattr(
            backend, "update_temporal_options"
        ), "GoldenRepoMetadataSqliteBackend must have 'update_temporal_options' method"

    def test_update_temporal_options_persists_and_retrieves(self, tmp_path):
        """Saved temporal_options can be read back from the database."""
        backend = self._make_backend(tmp_path)
        self._add_repo(backend, tmp_path)

        options = {"max_commits": 500, "diff_context": 10, "all_branches": True}
        result = backend.update_temporal_options("my-repo", options)
        assert result is True, "update_temporal_options must return True on success"

        row = backend.get_repo("my-repo")
        assert row is not None
        assert row["temporal_options"] == options

    def test_update_temporal_options_none_clears_options(self, tmp_path):
        """Setting temporal_options to None clears the stored value."""
        backend = self._make_backend(tmp_path)
        self._add_repo(backend, tmp_path)

        backend.update_temporal_options("my-repo", {"max_commits": 100})
        backend.update_temporal_options("my-repo", None)

        row = backend.get_repo("my-repo")
        assert row["temporal_options"] is None

    def test_update_temporal_options_returns_false_for_missing_alias(self, tmp_path):
        """update_temporal_options returns False when alias does not exist."""
        backend = self._make_backend(tmp_path)
        result = backend.update_temporal_options("nonexistent", {"max_commits": 100})
        assert result is False
