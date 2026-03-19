"""
Unit tests for Bug #468: add_index semantic rebuild completes instantly without rebuilding.

Root cause: add_index_to_golden_repo runs ["cidx", "index"] without --clear for semantic
index type, which is a no-op for already-indexed repos.

Fix: semantic must use ["cidx", "index", "--clear"] to force a full rebuild.

Acceptance criteria:
- semantic index_type issues ["cidx", "index", "--clear"]
- fts index_type issues ["cidx", "index", "--rebuild-fts-index"] (unchanged, verified)
"""

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
        Call add_index_to_golden_repo and capture every subprocess.run invocation.

        The method submits a background job via BackgroundJobManager.  We
        intercept submit_job to run the worker function synchronously so we can
        capture subprocess calls without threading complexity.
        """
        captured_cmds = []

        def fake_submit_job(operation_type, func, **kwargs):
            """Run the background worker synchronously and capture subprocess calls."""
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = _make_success_run(str(repo_path))
                # We need the mock to actually record calls.
                collected = []

                def recording_run(cmd, **kw):
                    collected.append(list(cmd))
                    r = MagicMock()
                    r.returncode = 0
                    r.stdout = "ok"
                    r.stderr = ""
                    return r

                mock_run.side_effect = recording_run
                # Also patch get_actual_repo_path so it returns our temp path.
                with patch.object(
                    registered_manager,
                    "get_actual_repo_path",
                    return_value=str(repo_path),
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
        """Same helper pattern as TestAddIndexSemanticCommand."""
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

            with patch("subprocess.run", side_effect=recording_run):
                with patch.object(
                    registered_manager,
                    "get_actual_repo_path",
                    return_value=str(repo_path),
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
