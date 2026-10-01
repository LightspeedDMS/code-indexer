"""Indexer resume-state trust and containment.

Every stored ``files_to_index`` candidate on the resume
path must be resolved (collapse ``..``, follow symlinks) and REJECTED
unless it lands strictly inside the resolved ``codebase_dir``, applying the
same eligibility filters (extensions, excludes) a fresh FileFinder walk
would apply. Rejected entries are dropped (a WARNING names the COUNT, never
the content/paths) rather than aborting the whole resume.

These tests drive the REAL ``SmartIndexer._do_resume_interrupted()`` (via a
planted ``ProgressiveMetadata`` in "in_progress" state, exactly as a
repository-authored ``.code-indexer/metadata-<provider>.json`` would look) and
assert the out-of-tree/ineligible candidate never reaches
``process_files_high_throughput`` -- the boundary immediately before
chunking/embedding/FTS. Only that boundary method is patched (to avoid
real network calls to the embedding provider and real chunk-store I/O);
everything upstream of it (ProgressiveMetadata, SmartIndexer's real resume
logic, FileFinder) is real.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Dict, List
from unittest.mock import MagicMock, patch

from code_indexer.config import Config
from code_indexer.services.smart_indexer import SmartIndexer


def _create_git_repo(path: Path) -> None:
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


def _make_indexer(
    repo: Path, tmp_path: Path, store: MagicMock, server_context: bool = False
) -> SmartIndexer:
    """Create a SmartIndexer wired to a real repo with mocked external services.

    ``server_context`` marks the config the way the server seam does, so
    out-of-root symlinks are confined; local CLI context follows them.
    """
    config = Config(codebase_dir=repo)
    if server_context:
        config.confine_to_codebase_root()
    mock_embedding = MagicMock()
    metadata_path = tmp_path / "metadata.json"
    return SmartIndexer(
        config=config,
        embedding_provider=mock_embedding,
        vector_store_client=store,
        metadata_path=metadata_path,
    )


def _mock_vector_store() -> MagicMock:
    store = MagicMock()
    store.resolve_collection_name.return_value = "test_collection"
    store.count_points.return_value = 0
    store.ensure_provider_aware_collection.return_value = "test_collection"
    store.begin_indexing.return_value = None
    store.end_indexing.return_value = {"vectors_indexed": 0}
    store.collection_exists.return_value = False
    store.delete_by_filter.return_value = True
    return store


def _seed_resumable_metadata(indexer: SmartIndexer, files_to_index: List[str]) -> None:
    """Plant metadata exactly as a repository-authored
    .code-indexer/metadata-<provider>.json would: an "in_progress" resume
    state with repository-controlled files_to_index and no files processed
    yet, so can_resume_interrupted_operation() is satisfied."""
    md = indexer.progressive_metadata.metadata
    md["status"] = "in_progress"
    md["files_to_index"] = list(files_to_index)
    md["total_files_to_index"] = len(files_to_index)
    md["current_file_index"] = 0
    md["completed_files"] = []
    md["failed_file_paths"] = []
    md["files_processed"] = 0
    md["chunks_indexed"] = 0


def _run_resume_and_capture(indexer: SmartIndexer) -> List[Path]:
    """Drive the REAL resume path, capturing exactly the file list that
    would be handed to chunking/embedding (process_files_high_throughput),
    without invoking real chunking/embedding/FTS I/O."""
    captured: Dict[str, List[Path]] = {}

    def _capture(files, **kwargs):
        captured["files"] = list(files)
        from code_indexer.indexing.processor import ProcessingStats

        return ProcessingStats()

    with patch.object(indexer, "process_files_high_throughput", side_effect=_capture):
        indexer._do_resume_interrupted(
            batch_size=50,
            progress_callback=None,
            git_status={},
            provider_name="voyage-ai",
            model_name="voyage-code-3",
        )

    return captured.get("files", [])


def _assert_eligible_if_it_were_in_tree(
    indexer: SmartIndexer, repo: Path, name: str, content: str
) -> None:
    """Precondition helper: proves that a file with this NAME/extension/
    content WOULD pass FileFinder.is_eligible() if it were reconstructed
    in-tree. Used before asserting a same-named out-of-tree candidate is
    dropped, so the drop can only be attributed to the containment check,
    never to eligibility filtering (extension/size/exclude/text-file)."""
    decoy = repo / f"_eligibility_decoy_{name}"
    decoy.write_text(content)
    try:
        assert indexer.file_finder.is_eligible(decoy) is True, (
            f"test precondition invalid: a same-named/extensioned in-tree "
            f"file must be eligible for this test to prove containment "
            f"(not eligibility) is what drops the out-of-tree target: {name}"
        )
    finally:
        decoy.unlink()


def _eligibility_only_safety_check(
    indexer: SmartIndexer, candidate: Path, _resolved_codebase: Path
) -> bool:
    """A deliberately WEAKENED stand-in for
    ``SmartIndexer._resume_candidate_is_safe`` that applies ONLY
    FileFinder's eligibility decision, with no resolve/containment check
    at all. Used exclusively as a test-harness monkeypatch (never as a
    production code change) to prove that the real safety check's
    rejection of a traversal candidate is attributable to containment,
    not to eligibility filtering.

    Deliberately does NOT call ``FileFinder.is_eligible()`` /
    ``_should_include_file()`` directly: that method itself starts with
    the SAME resolve/containment check this stand-in exists to omit,
    which would make it no longer eligibility-only. Replicates the rest
    of that method's body (size gate, base filtering, override filter)
    so the eligibility decision itself is unchanged."""
    file_finder = indexer.file_finder
    try:
        if candidate.stat().st_size > file_finder.config.indexing.max_file_size:
            return False
        base_result = file_finder._get_base_filtering_result(candidate)
        if file_finder.override_filter_service:
            relative_path = candidate.relative_to(file_finder.config.codebase_dir)
            return bool(
                file_finder.override_filter_service.should_include_file(
                    relative_path, base_result
                )
            )
        return bool(base_result)
    except (OSError, ValueError):
        return False


class TestResumePathTraversalContainment:
    """Stored resume candidates must resolve strictly
    inside codebase_dir, or be dropped."""

    def test_relative_traversal_dropped_legit_file_kept(self, tmp_path: Path) -> None:
        """files_to_index = ["legit.py", "../outside_marker.py"] --
        the traversal entry must never reach process_files_high_throughput,
        while the legitimate in-tree file still gets processed. The
        out-of-tree target uses an ELIGIBLE extension (.py) so this test
        discriminates on containment, not on eligibility filtering."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _create_git_repo(repo)

        (repo / "legit.py").write_text("# legit\n")

        outside_marker = tmp_path / "outside_marker.py"
        outside_marker_content = "# MARKER_RESUME_TRAVERSAL_OUTSIDE_TREE\n"
        outside_marker.write_text(outside_marker_content)

        indexer = _make_indexer(repo, tmp_path, _mock_vector_store())
        _assert_eligible_if_it_were_in_tree(
            indexer, repo, "outside_marker.py", outside_marker_content
        )
        _seed_resumable_metadata(indexer, ["legit.py", "../outside_marker.py"])

        processed_files = _run_resume_and_capture(indexer)
        resolved_processed = {f.resolve() for f in processed_files}

        assert outside_marker.resolve() not in resolved_processed, (
            "SECURITY: a '../' traversal entry in resume metadata reached "
            f"process_files_high_throughput. Processed files: {processed_files}"
        )
        assert (repo / "legit.py").resolve() in resolved_processed, (
            "The legitimate in-tree file must still be processed after "
            f"dropping the out-of-tree entry. Processed files: {processed_files}"
        )

    def test_absolute_path_outside_codebase_dir_dropped(self, tmp_path: Path) -> None:
        """A stored ABSOLUTE path outside codebase_dir must never reach
        process_files_high_throughput. The out-of-tree target uses an
        ELIGIBLE extension (.py) so this test discriminates on
        containment, not on eligibility filtering."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _create_git_repo(repo)

        outside_marker = tmp_path / "outside_absolute_marker.py"
        outside_marker_content = "# MARKER_RESUME_ABSOLUTE_OUTSIDE_TREE\n"
        outside_marker.write_text(outside_marker_content)

        indexer = _make_indexer(repo, tmp_path, _mock_vector_store())
        _assert_eligible_if_it_were_in_tree(
            indexer, repo, "outside_absolute_marker.py", outside_marker_content
        )
        _seed_resumable_metadata(indexer, [str(outside_marker)])

        processed_files = _run_resume_and_capture(indexer)
        resolved_processed = {f.resolve() for f in processed_files}

        assert outside_marker.resolve() not in resolved_processed, (
            "SECURITY: an absolute out-of-tree path in resume metadata "
            f"reached process_files_high_throughput. Processed files: {processed_files}"
        )

    def test_absolute_path_with_embedded_traversal_dropped(
        self, tmp_path: Path
    ) -> None:
        """An absolute stored path can still embed a '..' traversal
        component (e.g. ``<repo>/../outside.py``) -- must be dropped the
        same way the plain relative traversal case is."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _create_git_repo(repo)

        outside_marker = tmp_path / "outside_absolute_marker.py"
        outside_marker_content = "# MARKER_RESUME_ABSOLUTE_TRAVERSAL\n"
        outside_marker.write_text(outside_marker_content)

        indexer = _make_indexer(repo, tmp_path, _mock_vector_store())
        _assert_eligible_if_it_were_in_tree(
            indexer, repo, "outside_absolute_marker.py", outside_marker_content
        )
        absolute_traversal = f"{repo}/../outside_absolute_marker.py"
        _seed_resumable_metadata(indexer, [absolute_traversal])

        processed_files = _run_resume_and_capture(indexer)
        resolved_processed = {f.resolve() for f in processed_files}

        assert outside_marker.resolve() not in resolved_processed, (
            "SECURITY: an absolute path embedding a '..' traversal "
            "component reached process_files_high_throughput. Processed "
            f"files: {processed_files}"
        )

    def test_in_tree_symlink_escaping_codebase_dir_dropped(
        self, tmp_path: Path
    ) -> None:
        """Server context: a file that LOOKS in-tree (a relative path under
        codebase_dir) but is actually a symlink pointing outside must be
        rejected -- resolution must follow symlinks, not just collapse '..'."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _create_git_repo(repo)

        outside_secret = tmp_path / "real_secret.txt"
        outside_secret.write_text("SECRET_MARKER_RESUME_SYMLINK\n")

        escape_link = repo / "escape_link.py"
        escape_link.symlink_to(outside_secret)

        indexer = _make_indexer(
            repo, tmp_path, _mock_vector_store(), server_context=True
        )
        _seed_resumable_metadata(indexer, ["escape_link.py"])

        processed_files = _run_resume_and_capture(indexer)
        resolved_processed = {f.resolve() for f in processed_files}

        assert outside_secret.resolve() not in resolved_processed, (
            "SECURITY: an in-tree symlink escaping codebase_dir reached "
            f"process_files_high_throughput. Processed files: {processed_files}"
        )

    def test_excluded_extension_in_tree_file_dropped(self, tmp_path: Path) -> None:
        """A real in-tree file whose extension is NOT in config.file_extensions
        must be rejected the same way a fresh FileFinder walk would reject
        it -- resume must apply the SAME eligibility filters."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _create_git_repo(repo)

        ineligible_file = repo / "secret.exe"
        ineligible_file.write_bytes(b"MZ\x00\x00 not a text source file")

        indexer = _make_indexer(repo, tmp_path, _mock_vector_store())
        _seed_resumable_metadata(indexer, ["secret.exe"])

        processed_files = _run_resume_and_capture(indexer)
        resolved_processed = {f.resolve() for f in processed_files}

        assert ineligible_file.resolve() not in resolved_processed, (
            "An excluded-extension in-tree file must be dropped by resume "
            f"the same way a fresh walk would reject it. Processed files: {processed_files}"
        )

    def test_dropped_entries_log_warning_with_count_not_content(
        self, tmp_path: Path, caplog
    ) -> None:
        """Rejected entries must be logged at WARNING
        naming the COUNT of dropped entries, never the path/content. Uses
        an ELIGIBLE (.py) out-of-tree target so the drop is attributable
        to containment, not eligibility filtering."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _create_git_repo(repo)

        (repo / "legit.py").write_text("# legit\n")
        outside_marker = tmp_path / "outside_marker.py"
        outside_marker_content = "# MARKER_RESUME_LOG_OUTSIDE_TREE\n"
        outside_marker.write_text(outside_marker_content)

        indexer = _make_indexer(repo, tmp_path, _mock_vector_store())
        _assert_eligible_if_it_were_in_tree(
            indexer, repo, "outside_marker.py", outside_marker_content
        )
        _seed_resumable_metadata(indexer, ["legit.py", "../outside_marker.py"])

        with caplog.at_level(logging.WARNING):
            _run_resume_and_capture(indexer)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        expected_message = (
            "Resume metadata contained 1 file path candidate(s) that are "
            "outside the codebase root or ineligible for indexing; "
            "dropping them and continuing the resume with the remaining "
            "files."
        )
        actual_messages = [r.getMessage() for r in warnings]
        assert expected_message in actual_messages, (
            "Expected the exact WARNING message naming the count (1) of "
            f"dropped resume candidates. Got WARNING records: {actual_messages}"
        )
        for message in actual_messages:
            assert "outside_marker" not in message, (
                "The WARNING must name the count, not the path/content of "
                f"the dropped entry. Got: {message}"
            )


class TestResumeContainmentDiscriminationProof:
    """Permanent, executable proof that the containment tests above
    discriminate on CONTAINMENT specifically: with an eligibility-only
    stand-in for ``_resume_candidate_is_safe`` (test-harness monkeypatch
    only, never a production change), the SAME traversal entries that the
    real safety check drops must instead be ACCEPTED. If these tests ever
    started failing (i.e. the eligibility-only check also rejected the
    entry), it would mean the scenario no longer isolates containment."""

    def test_relative_traversal_would_be_accepted_by_eligibility_only_check(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _create_git_repo(repo)

        (repo / "legit.py").write_text("# legit\n")
        outside_marker = tmp_path / "outside_marker.py"
        outside_marker.write_text("# MARKER_RESUME_TRAVERSAL_OUTSIDE_TREE\n")

        indexer = _make_indexer(repo, tmp_path, _mock_vector_store())
        _seed_resumable_metadata(indexer, ["legit.py", "../outside_marker.py"])

        with patch.object(
            indexer,
            "_resume_candidate_is_safe",
            side_effect=lambda c, rc: _eligibility_only_safety_check(indexer, c, rc),
        ):
            processed_files = _run_resume_and_capture(indexer)
        resolved_processed = {f.resolve(strict=False) for f in processed_files}

        assert outside_marker.resolve() in resolved_processed, (
            "Discrimination proof failed: an eligibility-only safety "
            "check (no containment at all) was expected to let this "
            "'../' traversal entry through. If it did not, the real "
            "test's rejection of this SAME entry can no longer be "
            f"attributed to containment. Processed: {processed_files}"
        )

    def test_absolute_path_with_embedded_traversal_would_be_accepted_by_eligibility_only_check(
        self, tmp_path: Path
    ) -> None:
        """An absolute stored path can still embed a '..' traversal
        component (e.g. ``<repo>/../outside.py``) -- syntactically it is
        prefixed by codebase_dir, so ``relative_to()`` succeeds the same
        way it does for the plain relative case, making it a genuinely
        discriminating absolute-path scenario. (A wholly UNRELATED
        absolute path, with no shared prefix at all, is rejected by
        FileFinder.is_eligible()'s own internal relative_to() call
        regardless of containment -- that scenario does not discriminate
        and is not used here.)"""
        repo = tmp_path / "repo"
        repo.mkdir()
        _create_git_repo(repo)

        outside_marker = tmp_path / "outside_absolute_marker.py"
        outside_marker.write_text("# MARKER_RESUME_ABSOLUTE_OUTSIDE_TREE\n")

        indexer = _make_indexer(repo, tmp_path, _mock_vector_store())
        absolute_traversal = f"{repo}/../outside_absolute_marker.py"
        _seed_resumable_metadata(indexer, [absolute_traversal])

        with patch.object(
            indexer,
            "_resume_candidate_is_safe",
            side_effect=lambda c, rc: _eligibility_only_safety_check(indexer, c, rc),
        ):
            processed_files = _run_resume_and_capture(indexer)
        resolved_processed = {f.resolve(strict=False) for f in processed_files}

        assert outside_marker.resolve() in resolved_processed, (
            "Discrimination proof failed: an eligibility-only safety "
            "check (no containment at all) was expected to let this "
            "absolute-with-embedded-traversal entry through. If it did "
            "not, the real containment check's rejection of this SAME "
            f"entry can no longer be attributed to containment. Processed: {processed_files}"
        )
