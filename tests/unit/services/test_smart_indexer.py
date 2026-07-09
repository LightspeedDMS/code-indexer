"""Tests for SmartIndexer pre-flight checks."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.config import Config
from code_indexer.services.smart_indexer import SmartIndexer


def _create_git_repo(path: Path) -> str:
    """Create a minimal git repo with one initial commit."""
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


def _make_indexer(repo: Path, tmp_path: Path, store: MagicMock) -> SmartIndexer:
    """Create a SmartIndexer wired to real git repo with mocked external services."""
    config = Config(codebase_dir=str(repo))
    mock_embedding = MagicMock()
    metadata_path = tmp_path / "metadata.json"
    return SmartIndexer(
        config=config,
        embedding_provider=mock_embedding,
        vector_store_client=store,
        metadata_path=metadata_path,
    )


@pytest.fixture
def mock_vector_store() -> MagicMock:
    """External vector store dependency — all methods return safe defaults."""
    store = MagicMock()
    store.resolve_collection_name.return_value = "test_collection"
    store.count_points.return_value = 0
    store.ensure_provider_aware_collection.return_value = "test_collection"
    store.begin_indexing.return_value = None
    store.end_indexing.return_value = {"vectors_indexed": 0}
    store.collection_exists.return_value = False
    store.delete_by_filter.return_value = True
    return store


@pytest.fixture
def git_repo(tmp_path: Path):
    """A real, minimally seeded git repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _create_git_repo(repo)
    return repo


class TestHnswlibPreFlightCheck:
    """Tests for the hnswlib availability pre-flight check in smart_index()."""

    def test_smart_index_raises_runtime_error_when_hnswlib_unavailable(
        self, tmp_path, git_repo, mock_vector_store
    ):
        """smart_index() must raise RuntimeError before acquiring lock when hnswlib is missing."""
        indexer = _make_indexer(git_repo, tmp_path, mock_vector_store)

        with patch("code_indexer.services.smart_indexer.HNSWLIB_AVAILABLE", False):
            with pytest.raises(RuntimeError, match="hnswlib"):
                indexer.smart_index()


class TestFtsBootstrap:
    """Tests for the FTS bootstrap path in _do_incremental_index()."""

    # Offset added to current time when seeding last_index_timestamp.
    # Must be large enough that (timestamp - safety_buffer_seconds) still
    # exceeds the mtime of any file created during test setup, ensuring
    # find_modified_files() returns an empty list.
    _INDEX_TIMESTAMP_FUTURE_OFFSET_SECONDS = 120

    def _seed_completed_metadata(self, indexer: SmartIndexer, git_repo: Path) -> None:
        """Seed progressive metadata to look like a prior completed index.

        project_id for a no-remote git repo falls back to the directory name.
        The fixture creates the repo at tmp_path/'repo', so project_id='repo'.
        The commit watermark is seeded to the repo's current HEAD so incremental
        indexing finds no new commits and hits the 'nothing to do' early exit.
        """
        import time

        head_commit = subprocess.run(
            ["git", "-C", str(git_repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        current_branch = subprocess.run(
            ["git", "-C", str(git_repo), "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        meta = indexer.progressive_metadata.metadata
        meta["status"] = "completed"
        # Set slightly in the future so timestamp - safety_buffer is still
        # ahead of current file mtimes, making find_modified_files return [].
        meta["last_index_timestamp"] = (
            time.time() + self._INDEX_TIMESTAMP_FUTURE_OFFSET_SECONDS
        )
        meta["files_processed"] = 5
        meta["embedding_provider"] = "voyage"
        meta["embedding_model"] = "voyage-code-3"
        meta["git_available"] = True
        # No remote in test repo -> project_id = directory name = "repo"
        meta["project_id"] = "repo"
        meta["current_branch"] = current_branch
        meta["files_to_index"] = []
        meta["current_file_index"] = 0
        # Watermark tells incremental path this commit is already indexed.
        meta["branch_commit_watermarks"] = {current_branch: head_commit}
        indexer.progressive_metadata._save_metadata()

    def test_populate_fts_called_when_create_new_fts_and_nothing_to_index(
        self, tmp_path: Path, git_repo: Path, mock_vector_store: MagicMock
    ) -> None:
        """FTS bootstrap must populate documents when FTS index is new and nothing to embed."""
        mock_vector_store.count_points.return_value = 10
        indexer = _make_indexer(git_repo, tmp_path, mock_vector_store)
        # Embedding provider returns real strings so should_force_full_index stays False.
        indexer.embedding_provider.get_provider_name.return_value = "voyage"
        indexer.embedding_provider.get_current_model.return_value = "voyage-code-3"
        self._seed_completed_metadata(indexer, git_repo)

        # No tantivy_index/meta.json -> create_new_fts=True
        fts_index_dir = git_repo / ".code-indexer" / "tantivy_index"
        assert not (fts_index_dir / "meta.json").exists()

        mock_fts = MagicMock()

        with patch(
            "code_indexer.services.tantivy_index_manager.TantivyIndexManager",
            return_value=mock_fts,
        ):
            indexer.smart_index(enable_fts=True)

        # Observable outcome: add_document must have been called at least once
        # (bootstrapping from disk files), proving the FTS index was populated.
        assert mock_fts.add_document.call_count >= 1

    def test_populate_fts_not_called_when_fts_index_already_exists(
        self, tmp_path: Path, git_repo: Path, mock_vector_store: MagicMock
    ) -> None:
        """FTS bootstrap must NOT run when meta.json exists (create_new_fts=False)."""
        mock_vector_store.count_points.return_value = 10
        indexer = _make_indexer(git_repo, tmp_path, mock_vector_store)
        indexer.embedding_provider.get_provider_name.return_value = "voyage"
        indexer.embedding_provider.get_current_model.return_value = "voyage-code-3"
        self._seed_completed_metadata(indexer, git_repo)

        # Create meta.json so the FTS index appears to already exist.
        fts_index_dir = git_repo / ".code-indexer" / "tantivy_index"
        fts_index_dir.mkdir(parents=True, exist_ok=True)
        (fts_index_dir / "meta.json").write_text("{}")

        mock_fts = MagicMock()

        with patch(
            "code_indexer.services.tantivy_index_manager.TantivyIndexManager",
            return_value=mock_fts,
        ):
            indexer.smart_index(enable_fts=True)

        # FTS index already existed -> create_new_fts=False -> no bootstrap.
        assert mock_fts.add_document.call_count == 0

    def test_populate_fts_from_all_files_reads_files_and_adds_documents(
        self, tmp_path: Path, mock_vector_store: MagicMock
    ) -> None:
        """_populate_fts_from_all_files must call add_document once per file with required fields."""
        repo = tmp_path / "repo2"
        repo.mkdir()
        _create_git_repo(repo)
        (repo / "alpha.py").write_text("x = 1\n")
        (repo / "beta.py").write_text("y = 2\n")

        indexer = _make_indexer(repo, tmp_path, mock_vector_store)
        # Derive expected count from the same file_finder the method uses.
        expected_count = len(list(indexer.file_finder.find_files()))

        mock_fts = MagicMock()
        indexer._populate_fts_from_all_files(mock_fts)

        # Exact one call per discovered file, no more no less.
        assert mock_fts.add_document.call_count == expected_count

        # Both known files must appear in the reported paths.
        reported_paths = {
            call[0][0]["path"] for call in mock_fts.add_document.call_args_list
        }
        assert any("alpha.py" in p for p in reported_paths), (
            f"alpha.py missing from reported paths: {reported_paths}"
        )
        assert any("beta.py" in p for p in reported_paths), (
            f"beta.py missing from reported paths: {reported_paths}"
        )

        # Every call must carry all required fields with correct types.
        required_fields = {
            "path",
            "content",
            "content_raw",
            "identifiers",
            "line_start",
            "line_end",
            "language",
        }
        for call in mock_fts.add_document.call_args_list:
            doc = call[0][0]
            missing = required_fields - set(doc.keys())
            assert not missing, f"add_document call missing fields: {missing}"
            assert isinstance(doc["line_start"], int)
            assert isinstance(doc["line_end"], int)
            assert doc["line_start"] >= 1
            assert doc["line_end"] >= doc["line_start"]


class TestResumePathReanchoring:
    """Resume must re-anchor stored absolute paths onto the CURRENT walk root.

    Staging bug (langfuse global repo, cow-daemon backend, ~101 refresh
    failures): a prior interrupted run stored file paths via
    ``ProgressiveMetadata.set_files_to_index`` as ABSOLUTE strings carrying the
    NFS *mount* prefix (e.g. ``/mnt/cow-storage/golden-repos/<repo>/...``).
    On resume the current ``config.codebase_dir`` is the daemon-LOCAL prefix
    (e.g. ``/home/jsbattig/cow-storage/golden-repos/<repo>``). The old code
    used the stored absolute path verbatim, so
    ``file_path.relative_to(config.codebase_dir)`` raised
    ``ValueError: ... is not in the subpath of ...`` ->
    "Hash calculation failed" -> "Git-aware resume failed and fallbacks are
    disabled". The fix re-anchors stored paths onto the current walk root.
    """

    def test_reanchors_stale_mount_prefix_onto_current_codebase_dir(self) -> None:
        """A stored absolute path with a stale mount prefix must re-anchor.

        RED: the current code keeps the stale ``/mnt/...`` path, so
        ``relative_to(codebase_dir)`` raises ValueError. After the fix the
        returned path lives under the daemon-local codebase_dir.
        """
        repo_leaf = "langfuse_Claude_Code"
        codebase_dir = Path(f"/home/jsbattig/cow-storage/golden-repos/{repo_leaf}")
        stored_path = f"/mnt/cow-storage/golden-repos/{repo_leaf}/trace.json"

        reanchored = SmartIndexer._reanchor_resume_path(stored_path, codebase_dir)

        # MUST be under the current walk root (the staging failure mode).
        assert reanchored.relative_to(codebase_dir) == Path("trace.json")
        assert reanchored == codebase_dir / "trace.json"

    def test_reanchors_nested_file_with_correct_relative_subpath(self) -> None:
        """A nested file under the stale prefix re-anchors with full subpath."""
        repo_leaf = "langfuse_Claude_Code"
        codebase_dir = Path(f"/home/jsbattig/cow-storage/golden-repos/{repo_leaf}")
        stored_path = f"/mnt/cow-storage/golden-repos/{repo_leaf}/sub/dir/deep.json"

        reanchored = SmartIndexer._reanchor_resume_path(stored_path, codebase_dir)

        assert reanchored.relative_to(codebase_dir) == Path("sub/dir/deep.json")

    def test_relative_stored_path_joins_to_codebase_dir(self) -> None:
        """A stored RELATIVE path joins to codebase_dir (legacy behavior kept)."""
        codebase_dir = Path("/home/jsbattig/cow-storage/golden-repos/repo")

        reanchored = SmartIndexer._reanchor_resume_path("sub/x.py", codebase_dir)

        assert reanchored == codebase_dir / "sub" / "x.py"

    def test_absolute_path_already_under_codebase_dir_unchanged(self) -> None:
        """Normal/local case: an absolute path already under codebase_dir is
        returned byte-identical (no re-anchoring side effects)."""
        codebase_dir = Path("/srv/projects/myrepo")
        stored_path = "/srv/projects/myrepo/pkg/module.py"

        reanchored = SmartIndexer._reanchor_resume_path(stored_path, codebase_dir)

        assert reanchored == Path(stored_path)
        assert reanchored.relative_to(codebase_dir) == Path("pkg/module.py")

    def test_unrelatable_absolute_path_returns_stored_path_unchanged(self) -> None:
        """Anti-silent-failure (#13): if a stored absolute path shares NO leaf
        with the current codebase_dir, it is returned unchanged so the
        downstream ``.exists()`` filter (and, if it survives, a genuine
        relative_to error) still surfaces -- never fabricate a wrong mapping."""
        codebase_dir = Path("/home/jsbattig/cow-storage/golden-repos/repo")
        stored_path = "/completely/unrelated/elsewhere/file.py"

        reanchored = SmartIndexer._reanchor_resume_path(stored_path, codebase_dir)

        assert reanchored == Path(stored_path)

    def test_reanchors_first_existing_occurrence_when_leaf_appears_multiple_times(
        self, tmp_path: Path
    ) -> None:
        """Bug #1087: when the repo leaf name appears more than once in a stored
        path, the forward scan must return the FIRST occurrence that yields an
        existing path on disk -- not the LAST occurrence (which lands one level
        deeper and does not exist).

        Concrete scenario (fastapi repo):
          codebase_dir = <tmp>/golden-repos/fastapi   (leaf = "fastapi")
          stored path  = /mnt/cow-storage/golden-repos/fastapi/tests/fastapi/conftest.py
                                                      ^idx=2           ^idx=4

        Backward scan (old/wrong): picks idx=4 -> tail=("conftest.py",)
          -> codebase_dir/conftest.py   (DOES NOT EXIST)

        Forward scan (new/correct): picks idx=2 -> tail=("tests","fastapi","conftest.py")
          -> codebase_dir/tests/fastapi/conftest.py   (EXISTS)
        """
        # Build real on-disk structure that the forward scan can probe via .exists().
        codebase_dir = tmp_path / "golden-repos" / "fastapi"
        target_file = codebase_dir / "tests" / "fastapi" / "conftest.py"
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text("# conftest")

        # Stored path carries a stale /mnt prefix; leaf "fastapi" appears at
        # index 2 (golden-repos/fastapi) AND at index 4 (tests/fastapi).
        stored_path = "/mnt/cow-storage/golden-repos/fastapi/tests/fastapi/conftest.py"

        reanchored = SmartIndexer._reanchor_resume_path(stored_path, codebase_dir)

        # The correct re-anchored path must exist on disk.
        assert reanchored.exists(), f"Re-anchored path does not exist: {reanchored}"
        assert reanchored == codebase_dir / "tests" / "fastapi" / "conftest.py"

        # Prove the wrong (last-occurrence) path does NOT exist, confirming
        # this test would have caught the old backward-scan bug.
        wrong_path = codebase_dir / "conftest.py"
        assert not wrong_path.exists(), (
            f"Wrong path unexpectedly exists (test setup error): {wrong_path}"
        )


class TestResumeReanchorWiring:
    """End-to-end proof that _do_resume_interrupted re-anchors stored paths.

    Reproduces the cow-daemon mount-vs-daemon-local split using a real repo
    plus a symlinked "mount" alias directory. The prior run is simulated by
    seeding ProgressiveMetadata with absolute paths under the symlink alias;
    the current run's codebase_dir is the real (daemon-local) repo path.
    """

    def test_resume_reanchors_stale_stored_paths_under_codebase_dir(
        self, tmp_path: Path, mock_vector_store: MagicMock
    ) -> None:
        # Real repo (daemon-local equivalent) + a symlinked "mount" alias.
        repo = tmp_path / "real" / "langfuse_repo"
        repo.mkdir(parents=True)
        _create_git_repo(repo)
        (repo / "trace.json").write_text('{"a": 1}\n')

        mount = tmp_path / "mnt"
        mount.mkdir()
        mount_alias = mount / "langfuse_repo"
        mount_alias.symlink_to(repo, target_is_directory=True)

        # Current run walks the real (daemon-local) path.
        indexer = _make_indexer(repo, tmp_path, mock_vector_store)

        # Prior run stored the file under the stale MOUNT-alias prefix.
        stale_stored = str(mount_alias / "trace.json")
        indexer.progressive_metadata.metadata["files_to_index"] = [stale_stored]
        indexer.progressive_metadata.metadata["total_files_to_index"] = 1
        indexer.progressive_metadata.metadata["current_file_index"] = 0

        captured: dict = {}

        def _capture(files, **kwargs):
            captured["files"] = list(files)
            from code_indexer.indexing.processor import ProcessingStats

            return ProcessingStats()

        with patch.object(
            indexer, "process_files_high_throughput", side_effect=_capture
        ):
            indexer._do_resume_interrupted(
                batch_size=50,
                progress_callback=None,
                git_status={},
                provider_name="voyage-ai",
                model_name="voyage-code-3",
            )

        assert captured.get("files"), "resume passed no files to the processor"
        for file_path in captured["files"]:
            # Every file MUST be under the current walk root (no stale prefix).
            rel = file_path.relative_to(repo)
            assert rel == Path("trace.json")
