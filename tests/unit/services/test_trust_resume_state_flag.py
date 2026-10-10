"""Indexer resume-state trust and containment.

Story #2121 retired resume-from-list: no context ever replays a stored work
list, so a committer/tenant-authored ``.code-indexer/metadata-<provider>.json``
cannot steer which files are processed. An interrupted state (status
in_progress or failed) is recovered by the store-derived reconcile, a
disk-vs-database walk that never reads a stored list, and never by
``--clear``/``force_full`` (which would re-embed everything -- prohibitive
at ~900-repo production scale). ``trust_resume_state`` now only decides
whether the stored failure list is trusted (the server seal,
resume_state_seal.py).

These tests drive the REAL ``SmartIndexer.smart_index()`` entry point
against planted repository-authored state and assert the routing for both
flag values, for ``--reconcile`` and for ``--clear``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.config import Config
from code_indexer.services.smart_indexer import SmartIndexer


@pytest.fixture(autouse=True)
def _isolated_server_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Server-context runs create the resume-seal key in the server data
    directory; keep it inside the test's own temp dir."""
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path / "server-data"))


def _create_git_repo(path: Path) -> None:
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


def _make_indexer(repo: Path, tmp_path: Path, store: MagicMock) -> SmartIndexer:
    config = Config(codebase_dir=repo)
    mock_embedding = MagicMock()
    # smart_index() reads
    # these into ProgressiveMetadata and JSON-serializes them -- must be
    # real strings, not an unconfigured MagicMock.
    mock_embedding.get_provider_name.return_value = "voyage-ai"
    mock_embedding.get_current_model.return_value = "voyage-code-3"
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


def _seed_interrupted_state_with_stored_list(indexer: SmartIndexer) -> None:
    """A repository-authored interrupted state carrying a stored work list
    (the retired resume-from-list fields), as an older release wrote it."""
    md = indexer.progressive_metadata.metadata
    md["status"] = "in_progress"
    md["files_to_index"] = ["../outside_secret.txt", "initial.py"]
    md["total_files_to_index"] = 2
    md["current_file_index"] = 0
    md["completed_files"] = []
    md["failed_file_paths"] = []
    md["files_processed"] = 0
    md["chunks_indexed"] = 0


class TestTrustResumeStateFlag:
    """Story #2121: no stored work list is ever replayed. Whatever
    trust_resume_state says, an interrupted state is recovered by the
    store-derived reconcile (a disk-vs-database walk that never reads a
    stored list), and --clear keeps its contract."""

    @pytest.mark.parametrize("trust_resume_state", [True, False])
    def test_interrupted_state_is_reconciled_whatever_the_trust_flag(
        self, tmp_path: Path, trust_resume_state: bool
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _create_git_repo(repo)
        indexer = _make_indexer(repo, tmp_path, _mock_vector_store())
        _seed_interrupted_state_with_stored_list(indexer)

        with (
            patch.object(indexer, "_do_incremental_index") as mock_incremental,
            patch.object(indexer, "_do_reconcile_with_database") as mock_reconcile,
        ):
            indexer.smart_index(force_full=False, trust_resume_state=trust_resume_state)

        mock_incremental.assert_not_called()
        mock_reconcile.assert_called_once()

    def test_trust_resume_state_false_compatible_with_reconcile(
        self, tmp_path: Path
    ) -> None:
        """`--reconcile --ignore-resume-state` reconciles exactly once."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _create_git_repo(repo)
        indexer = _make_indexer(repo, tmp_path, _mock_vector_store())
        _seed_interrupted_state_with_stored_list(indexer)

        with patch.object(indexer, "_do_reconcile_with_database") as mock_reconcile:
            indexer.smart_index(
                force_full=False,
                reconcile_with_database=True,
                trust_resume_state=False,
            )

        mock_reconcile.assert_called_once()

    def test_clear_with_an_interrupted_state_still_rebuilds(
        self, tmp_path: Path
    ) -> None:
        """--clear (force_full) is never downgraded to the reconcile."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _create_git_repo(repo)
        indexer = _make_indexer(repo, tmp_path, _mock_vector_store())
        _seed_interrupted_state_with_stored_list(indexer)

        with (
            patch.object(indexer, "_do_full_index") as mock_full,
            patch.object(indexer, "_do_reconcile_with_database") as mock_reconcile,
        ):
            indexer.smart_index(force_full=True)

        mock_full.assert_called_once()
        mock_reconcile.assert_not_called()
