"""
Unit tests for Story #229: Index-Source-First Refresh Pipeline (indexing types).

Tests verify tantivy preservation, temporal/SCIP indexing on source,
and error handling for the new _index_source() / _create_snapshot() split.

Acceptance criteria covered here:
- Tantivy FTS index NOT deleted from clone in _create_snapshot() (C3)
- Tantivy FTS index IS present in clone after CoW (inherited from source)
- Temporal indexing runs on source in _index_source() (C7)
- Temporal indexing NOT called for local:// repos
- SCIP indexing runs on source in _index_source() (C7)
- SCIP indexing NOT called again in _create_snapshot()
- Indexing failure on source raises RuntimeError (abort before CoW)
- Indexing timeout on source raises RuntimeError
"""

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.global_registry import GlobalRegistry
from code_indexer.config import ConfigManager


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def golden_repos_dir(tmp_path):
    grd = tmp_path / "golden_repos"
    grd.mkdir(parents=True)
    return grd


@pytest.fixture
def config_mgr(tmp_path):
    return ConfigManager(tmp_path / ".code-indexer" / "config.json")


@pytest.fixture
def query_tracker():
    return QueryTracker()


@pytest.fixture
def cleanup_manager(query_tracker):
    return CleanupManager(query_tracker)


@pytest.fixture
def registry(golden_repos_dir):
    return GlobalRegistry(str(golden_repos_dir))


@pytest.fixture
def scheduler(golden_repos_dir, config_mgr, query_tracker, cleanup_manager, registry):
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=query_tracker,
        cleanup_manager=cleanup_manager,
        registry=registry,
    )


@pytest.fixture
def source_repo(tmp_path):
    src = tmp_path / "source_repo"
    src.mkdir()
    (src / "README.md").write_text("# Test Repo")
    (src / "main.py").write_text("def main(): pass")
    (src / ".git").mkdir()
    return src


@pytest.fixture
def source_repo_with_tantivy(source_repo):
    """Source repo that already has a tantivy FTS index built on it."""
    tantivy_dir = source_repo / ".code-indexer" / "tantivy_index"
    tantivy_dir.mkdir(parents=True)
    (tantivy_dir / "meta.json").write_text('{"index_settings": {}}')
    (tantivy_dir / "0.segment").write_bytes(b"binary segment data")
    return source_repo


def _make_snapshot_mock_run():
    """
    Build a generic subprocess mock for _create_snapshot() tests:
    - cp --reflink=auto: uses shutil.copytree, then creates .code-indexer/index
      in the destination (simulating a source already indexed by _index_source();
      _create_snapshot() does NOT run cidx index itself)
    - all others: no-op success
    """
    def mock_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""

        if cmd[0] == "cp" and "--reflink=auto" in cmd:
            dst = cmd[-1]
            shutil.copytree(cmd[-2], dst)
            # Simulate source was already indexed before the CoW clone
            (Path(dst) / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)

        return result

    return mock_run


# ---------------------------------------------------------------------------
# Tests: tantivy FTS index preserved in clone (C3)
# ---------------------------------------------------------------------------


class TestTantivyIndexPreserved:
    """
    C3: Tantivy index deletion is REMOVED from _create_snapshot().

    The FTS index is built on the source in _index_source(), then inherited
    by the CoW clone. There is no need to delete and rebuild it.
    """

    def test_tantivy_index_not_deleted_in_create_snapshot(
        self, scheduler, registry, source_repo_with_tantivy
    ):
        """
        AC: _create_snapshot() must not call shutil.rmtree on the tantivy_index.
        """
        registry.register_global_repo(
            "tantivy-test",
            "tantivy-test-global",
            "git@github.com:org/repo.git",
            str(source_repo_with_tantivy),
        )

        rmtree_calls = []

        def mock_rmtree(path, **kwargs):
            rmtree_calls.append(str(path))

        with patch("subprocess.run", side_effect=_make_snapshot_mock_run()):
            with patch("shutil.rmtree", side_effect=mock_rmtree):
                scheduler._create_snapshot(
                    alias_name="tantivy-test-global",
                    source_path=str(source_repo_with_tantivy),
                )

        tantivy_deletes = [p for p in rmtree_calls if "tantivy_index" in p]
        assert tantivy_deletes == [], (
            f"C3: _create_snapshot() must NOT delete tantivy_index. "
            f"Found rmtree calls targeting tantivy: {tantivy_deletes}. "
            "The FTS index is built on source and inherited correctly via CoW clone."
        )

    def test_tantivy_index_present_in_clone_after_create_snapshot(
        self, scheduler, registry, source_repo_with_tantivy
    ):
        """
        AC: The tantivy_index in the clone reflects the source's indexed state.

        After _create_snapshot(), the versioned path must contain
        .code-indexer/tantivy_index/ with the inherited index files.
        """
        registry.register_global_repo(
            "tantivy-inherit-test",
            "tantivy-inherit-test-global",
            "git@github.com:org/repo.git",
            str(source_repo_with_tantivy),
        )

        with patch("subprocess.run", side_effect=_make_snapshot_mock_run()):
            result_path = scheduler._create_snapshot(
                alias_name="tantivy-inherit-test-global",
                source_path=str(source_repo_with_tantivy),
            )

        versioned_tantivy = Path(result_path) / ".code-indexer" / "tantivy_index"
        assert versioned_tantivy.exists(), (
            f"C3: tantivy_index must be present in clone at {versioned_tantivy}. "
            "The CoW clone should have inherited the index built on source."
        )


# ---------------------------------------------------------------------------
# Tests: temporal indexing in _index_source() (C7)
# ---------------------------------------------------------------------------


class TestTemporalIndexingInIndexSource:
    """C7: Temporal indexing runs on source in _index_source(), not in _create_snapshot()."""

    def test_temporal_indexing_runs_on_source_when_enabled(
        self, scheduler, registry, source_repo
    ):
        """
        AC: cidx index --index-commits runs on the golden repo source path
        when enable_temporal=True and repo is NOT local://.
        """
        registry.register_global_repo(
            "temporal-test",
            "temporal-test-global",
            "git@github.com:org/repo.git",
            str(source_repo),
            enable_temporal=True,
            temporal_options={
                "max_commits": 500,
                "since_date": "2024-01-01",
                "diff_context": 3,
            },
        )

        temporal_calls = []

        def mock_run(cmd, **kwargs):
            cwd = kwargs.get("cwd", "")
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""

            if cmd[:2] == ["cidx", "index"] and "--index-commits" in cmd:
                temporal_calls.append((list(cmd), str(cwd)))
            elif cmd[:2] == ["cidx", "index"] and "--fts" in cmd:
                (Path(cwd) / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)

            return result

        with patch("subprocess.run", side_effect=mock_run):
            scheduler._index_source(
                alias_name="temporal-test-global",
                source_path=str(source_repo),
            )

        assert temporal_calls, (
            "cidx index --index-commits was not called by _index_source() "
            "even though enable_temporal=True. "
            "C7: temporal indexing must run on source in _index_source()."
        )

        cmd, cwd = temporal_calls[0]
        assert cwd == str(source_repo), (
            f"cidx index --index-commits cwd={cwd} must be source_path={source_repo}."
        )
        assert "--max-commits" in cmd, "temporal option --max-commits not passed"
        assert "500" in cmd, "temporal option max_commits value 500 not passed"
        assert "--since-date" in cmd, "temporal option --since-date not passed"
        assert "2024-01-01" in cmd, "temporal option since_date value not passed"
        assert "--diff-context" in cmd, "temporal option --diff-context not passed"
        assert "3" in cmd, "temporal option diff_context value not passed"

    def test_temporal_indexing_skipped_for_local_repo(
        self, scheduler, registry, source_repo
    ):
        """
        AC: cidx index --index-commits is NOT invoked for local:// repos.
        local:// repos have no git history; temporal indexing must be skipped.
        """
        registry.register_global_repo(
            "local-temporal-test",
            "local-temporal-test-global",
            "local:///some/path",
            str(source_repo),
            enable_temporal=True,
        )

        temporal_calls = []

        def mock_run(cmd, **kwargs):
            cwd = kwargs.get("cwd", "")
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""

            if cmd[:2] == ["cidx", "index"] and "--index-commits" in cmd:
                temporal_calls.append(list(cmd))
            elif cmd[:2] == ["cidx", "index"] and "--fts" in cmd:
                (Path(cwd) / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)

            return result

        with patch("subprocess.run", side_effect=mock_run):
            scheduler._index_source(
                alias_name="local-temporal-test-global",
                source_path=str(source_repo),
            )

        assert temporal_calls == [], (
            "cidx index --index-commits must NOT be called for local:// repos. "
            f"Got calls: {temporal_calls}"
        )


# ---------------------------------------------------------------------------
# Tests: SCIP indexing in _index_source() (C7)
# ---------------------------------------------------------------------------


class TestScipIndexingInIndexSource:
    """C7: SCIP indexing runs on source in _index_source(), not in _create_snapshot()."""

    def test_scip_runs_on_source_when_enabled(
        self, scheduler, registry, source_repo
    ):
        """
        AC: cidx scip generate runs on source path when enable_scip=True.
        """
        registry.register_global_repo(
            "scip-test",
            "scip-test-global",
            "git@github.com:org/repo.git",
            str(source_repo),
            enable_scip=True,
        )

        scip_calls = []

        def mock_run(cmd, **kwargs):
            cwd = kwargs.get("cwd", "")
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""

            if cmd[:2] == ["cidx", "scip"] and "generate" in cmd:
                scip_calls.append((list(cmd), str(cwd)))
            elif cmd[:2] == ["cidx", "index"] and "--fts" in cmd:
                (Path(cwd) / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)

            return result

        with patch("subprocess.run", side_effect=mock_run):
            scheduler._index_source(
                alias_name="scip-test-global",
                source_path=str(source_repo),
            )

        assert scip_calls, (
            "cidx scip generate was not called by _index_source() even though enable_scip=True. "
            "C7: SCIP indexing must run on source in _index_source()."
        )
        cmd, cwd = scip_calls[0]
        assert cwd == str(source_repo), (
            f"cidx scip generate cwd={cwd} must be source_path={source_repo}."
        )

    def test_scip_not_called_in_create_snapshot(
        self, scheduler, registry, source_repo
    ):
        """
        AC: cidx scip generate does NOT run in _create_snapshot().
        The clone inherits the SCIP index from the source via CoW.
        """
        registry.register_global_repo(
            "scip-snapshot-test",
            "scip-snapshot-test-global",
            "git@github.com:org/repo.git",
            str(source_repo),
            enable_scip=True,
        )

        scip_calls = []

        def mock_run(cmd, **kwargs):
            cwd = kwargs.get("cwd", "")
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""

            if cmd[0] == "cp" and "--reflink=auto" in cmd:
                dst = cmd[-1]
                shutil.copytree(cmd[-2], dst)
                # Simulate source was already indexed before the CoW clone
                (Path(dst) / ".code-indexer" / "index").mkdir(parents=True, exist_ok=True)
            elif cmd[:2] == ["cidx", "scip"] and "generate" in cmd:
                scip_calls.append((list(cmd), str(cwd)))

            return result

        with patch("subprocess.run", side_effect=mock_run):
            scheduler._create_snapshot(
                alias_name="scip-snapshot-test-global",
                source_path=str(source_repo),
            )

        assert scip_calls == [], (
            "cidx scip generate must NOT be called from _create_snapshot(). "
            f"Got calls: {scip_calls}"
        )


# ---------------------------------------------------------------------------
# Tests: error handling — indexing failure aborts before CoW clone
# ---------------------------------------------------------------------------


class TestIndexingFailureAbortsPipeline:
    """
    AC: Indexing failure on source raises RuntimeError and no CoW clone occurs.
    """

    def test_index_source_failure_raises_runtime_error(
        self, scheduler, registry, source_repo
    ):
        """
        AC: When cidx index fails, _index_source() raises RuntimeError.
        """
        registry.register_global_repo(
            "fail-test",
            "fail-test-global",
            "git@github.com:org/repo.git",
            str(source_repo),
        )

        def mock_run_fail(cmd, **kwargs):
            if cmd[:2] == ["cidx", "index"] and "--fts" in cmd:
                raise subprocess.CalledProcessError(
                    returncode=1, cmd=cmd, stderr="indexing failed"
                )
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        with pytest.raises(RuntimeError, match="(?i)index"):
            with patch("subprocess.run", side_effect=mock_run_fail):
                scheduler._index_source(
                    alias_name="fail-test-global",
                    source_path=str(source_repo),
                )

    def test_index_source_failure_does_not_trigger_cow_clone(
        self, scheduler, registry, source_repo
    ):
        """
        AC: When _index_source() fails, the CoW clone (cp --reflink=auto) is
        never invoked — because _create_snapshot() is never called.

        This test simulates the _execute_refresh() call-site pattern where
        an exception from _index_source() propagates and prevents the
        subsequent call to _create_snapshot().
        """
        registry.register_global_repo(
            "abort-test",
            "abort-test-global",
            "git@github.com:org/repo.git",
            str(source_repo),
        )

        cow_called = []

        def mock_run_fail(cmd, **kwargs):
            if cmd[:2] == ["cidx", "index"] and "--fts" in cmd:
                raise subprocess.CalledProcessError(
                    returncode=1, cmd=cmd, stderr="indexing failed"
                )
            if cmd[0] == "cp" and "--reflink=auto" in cmd:
                cow_called.append(True)
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        with patch("subprocess.run", side_effect=mock_run_fail):
            try:
                scheduler._index_source(
                    alias_name="abort-test-global",
                    source_path=str(source_repo),
                )
                pytest.fail("_index_source() must raise RuntimeError on indexing failure")
            except RuntimeError:
                pass  # expected — caller would not call _create_snapshot()

        assert cow_called == [], (
            "CoW clone must NOT be called when _index_source() fails. "
            "The caller aborts before calling _create_snapshot()."
        )

    def test_index_source_timeout_raises_runtime_error(
        self, scheduler, registry, source_repo
    ):
        """
        AC: When cidx index times out, _index_source() raises RuntimeError.
        """
        registry.register_global_repo(
            "timeout-test",
            "timeout-test-global",
            "git@github.com:org/repo.git",
            str(source_repo),
        )

        def mock_run_timeout(cmd, **kwargs):
            if cmd[:2] == ["cidx", "index"] and "--fts" in cmd:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=3600)
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        with pytest.raises(RuntimeError):
            with patch("subprocess.run", side_effect=mock_run_timeout):
                scheduler._index_source(
                    alias_name="timeout-test-global",
                    source_path=str(source_repo),
                )
