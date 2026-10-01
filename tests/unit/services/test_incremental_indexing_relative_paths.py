"""TDD tests for Bug: incremental indexing passes relative paths to HighThroughputProcessor.

Root cause: _get_git_deltas_since_commit() returns relative paths from git diff
--name-status output. The _do_incremental_index() method wraps them in
Path() but never converts to absolute. These relative Paths then fail when
file_identifier.get_file_metadata() calls file_path.relative_to(self.project_dir).

The resume path (_do_resume_interrupted) has the same issue.

These tests FAIL before the fix and PASS after.
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.config import Config
from code_indexer.services.smart_indexer import SmartIndexer
from code_indexer.indexing.processor import ProcessingStats


def _create_git_repo(path: Path) -> str:
    """Create a git repo and return the initial commit hash."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    (path / "initial.py").write_text("# initial\n")
    subprocess.run(
        ["git", "-C", str(path), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _add_commit(repo: Path, filename: str, content: str) -> str:
    """Add a file and commit, return new commit hash."""
    filepath = repo / filename
    filepath.parent.mkdir(parents=True, exist_ok=True)  # Create parent dirs
    filepath.write_text(content)
    subprocess.run(
        ["git", "-C", str(repo), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", f"add {filename}"],
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _make_smart_indexer(codebase_dir: Path, metadata_path: Path) -> SmartIndexer:
    """Create a SmartIndexer with mocked dependencies."""
    config = Config(codebase_dir=str(codebase_dir))
    mock_embedding_provider = MagicMock()
    mock_vector_store = MagicMock()
    mock_vector_store.resolve_collection_name.return_value = "test_collection"
    mock_vector_store.count_points.return_value = 0
    mock_vector_store.ensure_provider_aware_collection.return_value = "test_collection"
    mock_vector_store.begin_indexing.return_value = None
    mock_vector_store.end_indexing.return_value = {"vectors_indexed": 0}
    mock_vector_store.collection_exists.return_value = False
    return SmartIndexer(
        config=config,
        embedding_provider=mock_embedding_provider,
        vector_store_client=mock_vector_store,
        metadata_path=metadata_path,
    )


class TestGitDeltaReturnsRelativePaths:
    """Verify that _get_git_deltas_since_commit returns relative paths (expected git behavior)."""

    def test_git_delta_added_paths_are_relative(self, tmp_path):
        """_get_git_deltas_since_commit returns relative paths, not absolute."""
        repo = tmp_path / "repo"
        repo.mkdir()
        initial_commit = _create_git_repo(repo)

        second_commit = _add_commit(repo, "src/foo.py", "def foo(): pass\n")

        metadata_path = tmp_path / "metadata.json"
        indexer = _make_smart_indexer(repo, metadata_path)

        delta = indexer._get_git_deltas_since_commit(initial_commit, second_commit)

        assert len(delta.added) == 1
        added_path = delta.added[0]
        assert not Path(added_path).is_absolute(), (
            f"Expected relative path from git diff, got absolute: {added_path}"
        )
        assert added_path == "src/foo.py"


def _run_do_incremental_index(
    indexer, repo, initial_commit, second_commit, working_dir_files=None
):
    """Module-level helper to call _do_incremental_index with all required
    mocks. Shared by TestIncrementalIndexingConvertsRelativeToAbsolute and
    TestIncrementalIndexingDedupesAcrossTracks -- kept as a plain function
    (not a shared base class) so pytest never re-collects/re-runs one
    class's tests under the other."""
    captured_files = []

    def capture_process_files(files, *args, **kwargs):
        captured_files.extend(files)
        return ProcessingStats()

    if working_dir_files is None:
        working_dir_files = []

    git_status = {
        "git_available": True,
        "current_branch": "master",
        "current_commit": second_commit,
        "is_dirty": False,
    }

    # Mock progressive_metadata to return a resume_timestamp so we go into
    # incremental path (not full-index path), and return last_indexed_commit
    # so the git delta path is triggered.
    indexer.progressive_metadata.metadata["status"] = "completed"
    with patch.object(
        indexer.progressive_metadata,
        "can_resume_interrupted_operation",
        return_value=False,
    ):
        with patch.object(
            indexer.progressive_metadata,
            "get_resume_timestamp",
            return_value=1.0,  # non-zero => incremental path
        ):
            with patch.object(
                indexer.progressive_metadata,
                "get_last_indexed_commit",
                return_value=initial_commit,
            ):
                with patch.object(indexer.progressive_metadata, "start_indexing"):
                    with patch.object(
                        indexer.progressive_metadata, "set_files_to_index"
                    ):
                        with patch.object(
                            indexer.progressive_metadata, "update_progress"
                        ):
                            with patch.object(
                                indexer.progressive_metadata,
                                "update_commit_watermark",
                            ):
                                with patch.object(
                                    indexer.progressive_metadata,
                                    "complete_indexing",
                                ):
                                    with patch.object(
                                        indexer,
                                        "_delete_files_from_backend",
                                        return_value=0,
                                    ):
                                        with patch.object(
                                            indexer.file_finder,
                                            "find_modified_files",
                                            return_value=working_dir_files,
                                        ):
                                            with patch.object(
                                                indexer.file_finder,
                                                "find_files",
                                                return_value=[],
                                            ):
                                                with patch.object(
                                                    indexer.git_topology_service,
                                                    "get_current_branch",
                                                    return_value="master",
                                                ):
                                                    with patch.object(
                                                        indexer.git_topology_service,
                                                        "is_git_available",
                                                        return_value=False,
                                                    ):
                                                        with patch.object(
                                                            indexer.progress_log,
                                                            "start_session",
                                                            return_value="session-id",
                                                        ):
                                                            with patch.object(
                                                                indexer.progress_log,
                                                                "complete_session",
                                                            ):
                                                                with patch.object(
                                                                    indexer,
                                                                    "process_files_high_throughput",
                                                                    side_effect=capture_process_files,
                                                                ):
                                                                    indexer._do_incremental_index(
                                                                        batch_size=10,
                                                                        progress_callback=None,
                                                                        git_status=git_status,
                                                                        provider_name="voyage",
                                                                        model_name="voyage-3",
                                                                        safety_buffer_seconds=0,
                                                                        quiet=True,
                                                                    )
    return captured_files


class TestIncrementalIndexingConvertsRelativeToAbsolute:
    """Tests that the incremental indexing path converts relative git paths to absolute."""

    def test_files_passed_to_process_files_are_absolute(self, tmp_path):
        """When git diff returns relative paths, process_files_high_throughput
        must receive absolute paths rooted at config.codebase_dir."""
        repo = tmp_path / "repo"
        repo.mkdir()
        initial_commit = _create_git_repo(repo)
        second_commit = _add_commit(repo, "src/bar.py", "def bar(): pass\n")

        metadata_path = tmp_path / "metadata.json"
        indexer = _make_smart_indexer(repo, metadata_path)

        captured_files = _run_do_incremental_index(
            indexer, repo, initial_commit, second_commit
        )

        assert len(captured_files) > 0, (
            "No files were passed to process_files_high_throughput"
        )

        for f in captured_files:
            assert Path(f).is_absolute(), (
                f"Expected absolute path passed to process_files_high_throughput, got: {f}"
            )
            assert str(f).startswith(str(repo)), (
                f"Expected path rooted at {repo}, got: {f}"
            )

    def test_relative_path_becomes_absolute_with_codebase_dir_prefix(self, tmp_path):
        """A relative path like 'src/module.py' becomes '{codebase_dir}/src/module.py'."""
        repo = tmp_path / "repo"
        repo.mkdir()
        initial_commit = _create_git_repo(repo)
        second_commit = _add_commit(repo, "src/module.py", "x = 1\n")

        metadata_path = tmp_path / "metadata.json"
        indexer = _make_smart_indexer(repo, metadata_path)

        captured_files = _run_do_incremental_index(
            indexer, repo, initial_commit, second_commit
        )

        assert len(captured_files) > 0, (
            "No files were passed to process_files_high_throughput"
        )
        expected_absolute = repo / "src" / "module.py"
        file_paths = [Path(f) for f in captured_files]
        assert expected_absolute in file_paths, (
            f"Expected {expected_absolute} in files, got: {file_paths}"
        )


class TestIncrementalIndexingDedupesAcrossTracks:
    """Regression tests for Issue #1574: a physical file discovered by BOTH
    change-detection tracks (git-delta relative path AND mtime-scan absolute
    path) must be de-duplicated on the absolutized path and therefore
    appear exactly once in files_to_index -- never twice.

    Before the fix, _do_incremental_index() deduplicates the raw path
    STRINGS via set() BEFORE absolutizing them, so 'src/dup.py' (relative,
    from the git delta) and '/repo/src/dup.py' (absolute, from the mtime
    scan) survive as two distinct set members even though they resolve to
    the identical physical file -- causing that file to be hashed, chunked,
    embedded, and upserted twice in the same run.

    Uses the module-level _run_do_incremental_index() helper (shared with
    TestIncrementalIndexingConvertsRelativeToAbsolute) with its
    `working_dir_files` seam to drive Track 2. Deliberately NOT a subclass
    of TestIncrementalIndexingConvertsRelativeToAbsolute -- inheriting
    between two concrete Test* classes causes pytest to re-collect and
    re-run the parent's tests under the subclass too.
    """

    def test_file_found_by_both_tracks_indexed_exactly_once(self, tmp_path):
        """A file present in both the git-delta track (relative path) and the
        mtime-scan track (absolute path) must appear exactly once in the
        files passed to process_files_high_throughput -- proving the merge
        dedupes on resolved absolute path, not on raw path string."""
        repo = tmp_path / "repo"
        repo.mkdir()
        initial_commit = _create_git_repo(repo)
        # This commit makes 'src/dup.py' show up in git_delta.added, i.e. the
        # git-delta track (Track 1), as a REPO-RELATIVE path string.
        second_commit = _add_commit(repo, "src/dup.py", "def dup(): pass\n")

        metadata_path = tmp_path / "metadata.json"
        indexer = _make_smart_indexer(repo, metadata_path)

        # Same physical file, reported by the mtime-scan track (Track 2) as
        # an ABSOLUTE path -- exactly how FileFinder.find_modified_files
        # (via os.walk on an absolute codebase_dir) actually reports files.
        absolute_dup_path = repo / "src" / "dup.py"

        captured_files = _run_do_incremental_index(
            indexer,
            repo,
            initial_commit,
            second_commit,
            working_dir_files=[absolute_dup_path],
        )

        # NOTE: intentionally NOT normalizing with Path(f).resolve() here --
        # the production code de-duplicates on the ABSOLUTIZED path only
        # (prefixing with codebase_dir), never on the symlink-resolved
        # path. Asserting against .resolve()'d paths would pin a STRONGER
        # equivalence than the code actually provides and could mask a
        # future regression. Assert directly against the raw captured
        # paths instead.
        assert len(captured_files) == 1, (
            "File present in BOTH change-detection tracks (relative git "
            "delta + absolute mtime scan) must be de-duplicated to exactly "
            f"one entry, but appeared {len(captured_files)} times in "
            f"files_to_index: {captured_files}"
        )


class TestResumePathConvertsRelativeToAbsolute:
    """Tests that the resume path (_do_resume_interrupted) converts relative paths to absolute."""

    def _call_do_resume_interrupted(self, indexer, relative_paths):
        """Helper to call _do_resume_interrupted with all required mocks."""
        captured_files = []

        def capture_process_files(files, *args, **kwargs):
            captured_files.extend(files)
            return ProcessingStats()

        git_status = {
            "git_available": True,
            "current_branch": "master",
            "current_commit": "abc123",
            "is_dirty": False,
        }

        with patch.object(
            indexer.progressive_metadata,
            "get_remaining_files",
            return_value=relative_paths,
        ):
            with patch.object(indexer.progressive_metadata, "complete_indexing"):
                with patch.object(indexer.progressive_metadata, "update_progress"):
                    with patch.object(
                        indexer.progressive_metadata,
                        "get_stats",
                        return_value={
                            "files_processed": 0,
                            "total_files_to_index": 1,
                            "chunks_indexed": 0,
                        },
                    ):
                        with patch.object(indexer.progress_log, "complete_session"):
                            with patch.object(
                                indexer.progress_log, "mark_session_cancelled"
                            ):
                                with patch.object(
                                    indexer,
                                    "process_files_high_throughput",
                                    side_effect=capture_process_files,
                                ):
                                    indexer._do_resume_interrupted(
                                        batch_size=10,
                                        progress_callback=None,
                                        git_status=git_status,
                                        provider_name="voyage",
                                        model_name="voyage-3",
                                        quiet=True,
                                    )
        return captured_files

    def test_resume_files_passed_to_process_are_absolute(self, tmp_path):
        """When progressive metadata contains relative file paths (stored from a
        failed incremental run), _do_resume_interrupted must convert them to
        absolute before passing to process_files_high_throughput."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _create_git_repo(repo)

        # Create the actual file so exists() check passes
        (repo / "src").mkdir()
        (repo / "src" / "resume_me.py").write_text("def resume(): pass\n")

        metadata_path = tmp_path / "metadata.json"
        indexer = _make_smart_indexer(repo, metadata_path)

        captured_files = self._call_do_resume_interrupted(indexer, ["src/resume_me.py"])

        assert len(captured_files) > 0, (
            "No files were passed to process_files_high_throughput during resume"
        )

        for f in captured_files:
            assert Path(f).is_absolute(), (
                f"Expected absolute path during resume, got relative: {f}"
            )
            assert str(f).startswith(str(repo)), (
                f"Expected path rooted at {repo}, got: {f}"
            )

    def test_resume_relative_path_resolves_to_codebase_dir(self, tmp_path):
        """Relative path 'a/b.py' in metadata resolves to '{codebase_dir}/a/b.py'."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _create_git_repo(repo)

        (repo / "a").mkdir()
        (repo / "a" / "b.py").write_text("pass\n")

        metadata_path = tmp_path / "metadata.json"
        indexer = _make_smart_indexer(repo, metadata_path)

        captured_files = self._call_do_resume_interrupted(indexer, ["a/b.py"])

        assert len(captured_files) > 0
        expected = repo / "a" / "b.py"
        file_paths = [Path(f) for f in captured_files]
        assert expected in file_paths, (
            f"Expected {expected} in files, got: {file_paths}"
        )
