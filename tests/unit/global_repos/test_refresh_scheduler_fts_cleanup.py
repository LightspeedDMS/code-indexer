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
        Story #229: tantivy_index is PRESERVED in the CoW clone, NOT deleted.

        The new workflow builds FTS on the source first (_index_source), then the
        CoW clone inherits the correct tantivy_index via reflink copy.  There is
        no ghost-vector problem because the FTS is built on the same content that
        will be cloned, so inherited entries are always accurate.

        This test (previously BUG-1) is now inverted: it verifies that
        tantivy_index IS present in the versioned snapshot after _create_new_index,
        confirming it was inherited from the source and not deleted.

        Approach:
        - Source repo has .code-indexer/tantivy_index/ (pre-indexed FTS)
        - _index_source runs cidx index --fts on source (mocked: no-op)
        - _create_snapshot CoW-clones source → versioned path (shutil.copytree)
        - Verify tantivy_index exists in the versioned path
        """
        # Register a dummy repo so registry.get_global_repo() returns something
        scheduler.registry.register_global_repo(
            "test-repo",
            "test-repo-global",
            "git@github.com:org/repo.git",
            str(source_repo_with_tantivy),
        )

        def mock_subprocess_run(cmd, **kwargs):
            """
            Mock subprocess.run:
            - cidx index --fts: no-op (FTS built on source — mocked away)
            - cp --reflink=auto: CoW clone via shutil.copytree; also creates index dir
            - git / cidx fix-config: no-op
            """
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""

            if cmd[0] == "cp":
                dst = cmd[-1]
                shutil.copytree(cmd[-2], dst)
                # Simulate source was already indexed: index dir must exist
                (Path(dst) / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)

            return mock_result

        with patch("subprocess.run", side_effect=mock_subprocess_run):
            result_path = scheduler._create_new_index(
                alias_name="test-repo-global",
                source_path=str(source_repo_with_tantivy),
            )

        # Story #229: tantivy_index must be PRESENT in the versioned snapshot
        versioned_tantivy = Path(result_path) / ".code-indexer" / "tantivy_index"
        assert versioned_tantivy.exists(), (
            "Story #229: tantivy_index must be preserved in the CoW clone. "
            "FTS is built on the source first (_index_source), then the clone "
            "inherits it directly — no deletion needed or wanted."
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
        Story #229: tantivy_index is PRESERVED through the entire _create_snapshot workflow.

        Previously (BUG-1 fix) tantivy was deleted before fix-config. With Story #229,
        FTS is built on the source by _index_source() first, so the CoW clone inherits
        a correct, fresh tantivy_index. No deletion is needed or performed.

        This test now verifies:
        - CoW clone happens
        - cidx fix-config runs with tantivy_index PRESENT (inherited correctly)
        - tantivy_index is not deleted at any point in _create_snapshot
        """
        call_sequence = []

        def mock_subprocess_run(cmd, **kwargs):
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""

            if cmd[0] == "cp":
                dst = cmd[-1]
                shutil.copytree(cmd[-2], dst)
                # Simulate source was already indexed: index dir must exist in clone
                (Path(dst) / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)
                call_sequence.append("cow_clone")
            elif cmd[:2] == ["cidx", "fix-config"]:
                # Record tantivy state at fix-config time — must be PRESENT
                cwd = Path(kwargs.get("cwd", "."))
                tantivy_path = cwd / ".code-indexer" / "tantivy_index"
                call_sequence.append(
                    f"fix-config:tantivy_exists={tantivy_path.exists()}"
                )

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

        # Verify fix-config happened with tantivy PRESENT (Story #229: no deletion)
        fix_config_entries = [
            e for e in call_sequence if e.startswith("fix-config:")
        ]
        assert len(fix_config_entries) >= 1, (
            "cidx fix-config was not called."
        )
        assert fix_config_entries[0] == "fix-config:tantivy_exists=True", (
            "Story #229: tantivy_index must be PRESENT when cidx fix-config runs. "
            f"Got: {fix_config_entries[0]}. "
            "FTS is built on source before the CoW clone and inherited correctly — "
            "no deletion should occur between CoW clone and fix-config."
        )
