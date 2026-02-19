"""
Unit tests for RefreshScheduler FTS ghost vector cleanup (BUG-1).

Problem: When RefreshScheduler creates a versioned snapshot via CoW clone,
the tantivy FTS index is copied along with the rest of `.code-indexer/`.
The subsequent `cidx index --fts` opens the existing index incrementally
and appends new entries but never removes stale entries for deleted files.
This causes ghost vectors in FTS/regex search results.

Fix: Delete `.code-indexer/tantivy_index/` from the versioned snapshot
immediately after the CoW clone, before `cidx fix-config` and `cidx index`.
This forces a full FTS rebuild from scratch with no inherited stale entries.
"""

import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.global_registry import GlobalRegistry
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.config import ConfigManager


class TestRefreshSchedulerFtsCleanup:
    """Test suite for BUG-1: tantivy_index cleanup in versioned snapshots."""

    @pytest.fixture
    def golden_repos_dir(self, tmp_path):
        """Create a golden repos directory structure."""
        golden_repos_dir = tmp_path / "golden_repos"
        golden_repos_dir.mkdir(parents=True)
        return golden_repos_dir

    @pytest.fixture
    def config_mgr(self, tmp_path):
        """Create a ConfigManager instance."""
        return ConfigManager(tmp_path / ".code-indexer" / "config.json")

    @pytest.fixture
    def query_tracker(self):
        """Create a QueryTracker instance."""
        return QueryTracker()

    @pytest.fixture
    def cleanup_manager(self, query_tracker):
        """Create a CleanupManager instance."""
        return CleanupManager(query_tracker)

    @pytest.fixture
    def registry(self, golden_repos_dir):
        """Create a GlobalRegistry instance."""
        return GlobalRegistry(str(golden_repos_dir))

    @pytest.fixture
    def alias_manager(self, golden_repos_dir):
        """Create an AliasManager instance."""
        return AliasManager(str(golden_repos_dir / "aliases"))

    @pytest.fixture
    def scheduler(
        self, golden_repos_dir, config_mgr, query_tracker, cleanup_manager, registry
    ):
        """Create a RefreshScheduler instance."""
        return RefreshScheduler(
            golden_repos_dir=str(golden_repos_dir),
            config_source=config_mgr,
            query_tracker=query_tracker,
            cleanup_manager=cleanup_manager,
            registry=registry,
        )

    @pytest.fixture
    def source_repo_with_tantivy(self, tmp_path):
        """
        Create a simulated source repository that has a tantivy_index.

        This simulates a golden repo whose .code-indexer/tantivy_index/ was
        indexed on a previous run.
        """
        source_dir = tmp_path / "source_repo"
        source_dir.mkdir()
        (source_dir / "README.md").write_text("# Test Repo")
        (source_dir / "main.py").write_text("def main(): pass")

        # Simulate existing tantivy index
        tantivy_dir = source_dir / ".code-indexer" / "tantivy_index"
        tantivy_dir.mkdir(parents=True)
        (tantivy_dir / "meta.json").write_text('{"index_settings": {}}')
        (tantivy_dir / "0.segment").write_bytes(b"binary segment data")

        return source_dir

    def test_tantivy_index_deleted_from_versioned_snapshot(
        self,
        golden_repos_dir,
        scheduler,
        source_repo_with_tantivy,
    ):
        """
        BUG-1: tantivy_index/ must be deleted after CoW clone, before cidx index.

        The CoW clone copies the entire source directory including
        .code-indexer/tantivy_index/. This directory must be deleted before
        cidx index --fts runs so that a fresh FTS index is created with no
        inherited ghost entries.

        Approach:
        - Create source repo with .code-indexer/tantivy_index/ containing dummy files
        - Mock subprocess.run so CoW clone actually copies via Python (shutil) to
          simulate the clone, while cidx commands are mocked to do nothing
        - Verify the tantivy_index directory is absent when cidx index --fts runs
        """
        # Track the state of tantivy_index at the moment cidx index --fts is called
        tantivy_state_at_cidx_index = []

        def mock_subprocess_run(cmd, **kwargs):
            """
            Mock subprocess.run:
            - cp --reflink=auto -a: actually copy with shutil to simulate CoW clone
            - git update-index: no-op
            - git restore: no-op
            - cidx fix-config: no-op, creates .code-indexer/index dir
            - cidx index: record tantivy state at this moment, then no-op
            - cidx scip: no-op
            """
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""

            if cmd[0] == "cp":
                # Simulate CoW clone: actually copy the directory
                src = cmd[-2]
                dst = cmd[-1]
                shutil.copytree(src, dst)
            elif cmd[:2] == ["cidx", "index"] and "--fts" in cmd:
                # Record whether tantivy_index exists at this exact moment
                cwd = Path(kwargs.get("cwd", "."))
                tantivy_path = cwd / ".code-indexer" / "tantivy_index"
                tantivy_state_at_cidx_index.append(tantivy_path.exists())
                # Also create the index dir so Step 6 validation passes
                index_dir = cwd / ".code-indexer" / "index"
                index_dir.mkdir(parents=True, exist_ok=True)
            elif cmd[:2] == ["cidx", "fix-config"]:
                # No-op for fix-config
                pass

            return mock_result

        # Register a dummy repo so registry.get_global_repo() returns something
        scheduler.registry.register_global_repo(
            "test-repo",
            "test-repo-global",
            "git@github.com:org/repo.git",
            str(source_repo_with_tantivy),
        )

        with patch("subprocess.run", side_effect=mock_subprocess_run):
            result_path = scheduler._create_new_index(
                alias_name="test-repo-global",
                source_path=str(source_repo_with_tantivy),
            )

        # cidx index --fts must have been called at least once
        assert len(tantivy_state_at_cidx_index) >= 1, (
            "cidx index --fts was never called. "
            "_create_new_index() must run 'cidx index --fts'."
        )

        # At the moment cidx index --fts was called, tantivy_index must NOT exist
        assert tantivy_state_at_cidx_index[0] is False, (
            "BUG-1: tantivy_index/ still exists when 'cidx index --fts' runs. "
            "The inherited FTS index from the CoW clone must be deleted before "
            "cidx index --fts to prevent ghost vectors in search results. "
            "Add shutil.rmtree() for .code-indexer/tantivy_index/ between "
            "the CoW clone and cidx fix-config steps in _create_new_index()."
        )

    def test_tantivy_cleanup_only_when_dir_exists(
        self,
        golden_repos_dir,
        scheduler,
        tmp_path,
    ):
        """
        BUG-1 safety: tantivy cleanup must not fail when tantivy_index is absent.

        Some source repos may not have a tantivy_index (e.g., freshly cloned
        repos that were never FTS-indexed). The cleanup step must be a no-op
        in that case and must not raise any exception.
        """
        # Source repo WITHOUT tantivy_index
        source_dir = tmp_path / "source_no_tantivy"
        source_dir.mkdir()
        (source_dir / "README.md").write_text("# Test Repo")

        tantivy_state_at_cidx_index = []

        def mock_subprocess_run(cmd, **kwargs):
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""

            if cmd[0] == "cp":
                src = cmd[-2]
                dst = cmd[-1]
                shutil.copytree(src, dst)
            elif cmd[:2] == ["cidx", "index"] and "--fts" in cmd:
                cwd = Path(kwargs.get("cwd", "."))
                tantivy_path = cwd / ".code-indexer" / "tantivy_index"
                tantivy_state_at_cidx_index.append(tantivy_path.exists())
                index_dir = cwd / ".code-indexer" / "index"
                index_dir.mkdir(parents=True, exist_ok=True)

            return mock_result

        # Register a dummy repo
        scheduler.registry.register_global_repo(
            "no-tantivy-repo",
            "no-tantivy-repo-global",
            "git@github.com:org/repo.git",
            str(source_dir),
        )

        # Must not raise any exception when tantivy_index does not exist
        with patch("subprocess.run", side_effect=mock_subprocess_run):
            result_path = scheduler._create_new_index(
                alias_name="no-tantivy-repo-global",
                source_path=str(source_dir),
            )

        # The process must complete successfully
        assert result_path is not None, (
            "BUG-1 safety: _create_new_index() must succeed even when "
            "tantivy_index does not exist in the source repo."
        )

        # cidx index --fts must still have been called
        assert len(tantivy_state_at_cidx_index) >= 1, (
            "cidx index --fts was not called when tantivy_index was absent."
        )

        # tantivy_index must still not exist at cidx index time (it was never there)
        assert tantivy_state_at_cidx_index[0] is False, (
            "tantivy_index should not exist in a fresh snapshot that never had one."
        )

    def test_tantivy_cleanup_happens_before_cidx_fix_config(
        self,
        golden_repos_dir,
        scheduler,
        source_repo_with_tantivy,
    ):
        """
        BUG-1 ordering: tantivy cleanup must happen before cidx fix-config.

        The fix must be placed between the CoW clone (Step 2) and
        cidx fix-config --force (Step 4), not after fix-config.
        This test verifies the deletion order by tracking call sequence.
        """
        call_sequence = []

        def mock_subprocess_run(cmd, **kwargs):
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""

            if cmd[0] == "cp":
                src = cmd[-2]
                dst = cmd[-1]
                shutil.copytree(src, dst)
                call_sequence.append("cow_clone")
            elif cmd[:2] == ["cidx", "fix-config"]:
                # Record tantivy state at fix-config time
                cwd = Path(kwargs.get("cwd", "."))
                tantivy_path = cwd / ".code-indexer" / "tantivy_index"
                call_sequence.append(
                    f"fix-config:tantivy_exists={tantivy_path.exists()}"
                )
            elif cmd[:2] == ["cidx", "index"] and "--fts" in cmd:
                cwd = Path(kwargs.get("cwd", "."))
                tantivy_path = cwd / ".code-indexer" / "tantivy_index"
                call_sequence.append(
                    f"cidx-index-fts:tantivy_exists={tantivy_path.exists()}"
                )
                index_dir = cwd / ".code-indexer" / "index"
                index_dir.mkdir(parents=True, exist_ok=True)

            return mock_result

        # Register a dummy repo
        scheduler.registry.register_global_repo(
            "ordering-test-repo",
            "ordering-test-repo-global",
            "git@github.com:org/repo.git",
            str(source_repo_with_tantivy),
        )

        with patch("subprocess.run", side_effect=mock_subprocess_run):
            scheduler._create_new_index(
                alias_name="ordering-test-repo-global",
                source_path=str(source_repo_with_tantivy),
            )

        # Verify cow_clone happened
        assert "cow_clone" in call_sequence, (
            "CoW clone step was not recorded in call sequence."
        )

        # Verify fix-config happened with tantivy already deleted
        fix_config_entries = [
            e for e in call_sequence if e.startswith("fix-config:")
        ]
        assert len(fix_config_entries) >= 1, (
            "cidx fix-config was not called."
        )
        assert fix_config_entries[0] == "fix-config:tantivy_exists=False", (
            "BUG-1 ordering: tantivy_index must be deleted BEFORE cidx fix-config runs. "
            f"Got: {fix_config_entries[0]}. "
            "The deletion must happen immediately after CoW clone (Step 2b), "
            "not after fix-config."
        )

        # Verify cidx index --fts happened with tantivy deleted
        cidx_entries = [
            e for e in call_sequence if e.startswith("cidx-index-fts:")
        ]
        assert len(cidx_entries) >= 1, (
            "cidx index --fts was not called."
        )
        assert cidx_entries[0] == "cidx-index-fts:tantivy_exists=False", (
            "BUG-1: tantivy_index must not exist when cidx index --fts runs. "
            f"Got: {cidx_entries[0]}."
        )
