"""Issue #1999: on a git project, a file newly excluded from indexing (by an
exclusion filter or an extension change) must stop being searchable after a
reconcile, even when nothing else changed.

Real `SmartIndexer`, real `FilesystemVectorStore`, real temp git repository.
The only test double is the embedding provider (an external service): it
counts the texts it is asked to embed.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import pytest

from code_indexer.config import Config
from code_indexer.services.smart_indexer import SmartIndexer
from code_indexer.services.tantivy_index_manager import TantivyIndexManager
from tests.unit.services.index_visibility_test_support import (
    hidden_branches_by_path as _hidden_branches_by_path,
)
from tests.unit.services.test_reconcile_non_git_content_id_2013 import (
    VECTOR_DIM,
    _BASE_MTIME,
    _CountingEmbeddingProvider,
    _DeletionRecordingStore,
    _committed_git_repo,
    _git,
    _reset,
)

_EXCLUDED_DIR = "vendor_lib"
_EXCLUDED_DIR_FILE = f"{_EXCLUDED_DIR}/delta.py"
_EXTENSION_FILE = "tool.go"
_ALWAYS_INDEXED = ("alpha.py", "beta.py", "gamma.py")


def _repo_with_extra_files(tmp_path: Path) -> Path:
    repo = _committed_git_repo(tmp_path)
    (repo / _EXCLUDED_DIR).mkdir()
    extra = {
        _EXCLUDED_DIR_FILE: "def delta_marker_function():\n    return 'DELTA'\n",
        _EXTENSION_FILE: 'package main\n\nfunc ToolMarker() string { return "T" }\n',
    }
    for offset, (name, content) in enumerate(sorted(extra.items())):
        path = repo / name
        path.write_text(content)
        mtime = _BASE_MTIME + 100 + offset
        os.utime(path, (mtime, mtime))
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "extra files")
    return repo


def _commit_big_file(repo: Path) -> str:
    """Commit a multi-chunk file under the excluded dir; returns its path."""
    big = f"{_EXCLUDED_DIR}/big.py"
    (repo / big).write_text(
        "".join(
            f"def big_function_{i}():\n    return 'BIG_{i}_' + 'x' * 40\n\n"
            for i in range(300)
        )
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "big file")
    return big


def _indexer_for(
    config: Config, metadata_path: Path
) -> Tuple[SmartIndexer, _CountingEmbeddingProvider, _DeletionRecordingStore]:
    embedder = _CountingEmbeddingProvider()
    store = _DeletionRecordingStore(
        base_path=Path(config.codebase_dir) / ".code-indexer" / "index"
    )
    store.ensure_provider_aware_collection(config, embedder)
    indexer = SmartIndexer(
        config=config,
        embedding_provider=embedder,
        vector_store_client=store,
        metadata_path=metadata_path,
    )
    return indexer, embedder, store


def _index_everything(repo: Path, metadata_path: Path) -> None:
    indexer, embedder, store = _indexer_for(Config(codebase_dir=repo), metadata_path)
    indexer.smart_index(force_full=True, quiet=True)
    hidden = _hidden_branches_by_path(indexer, store)
    assert _EXCLUDED_DIR_FILE in hidden and _EXTENSION_FILE in hidden, hidden


def _fts_hit_paths(repo: Path, text: str) -> List[str]:
    """Path of every hit (duplicates kept) the real on-disk Tantivy index
    returns for `text`."""
    fts = TantivyIndexManager(repo / ".code-indexer" / "tantivy_index")
    fts.initialize_index(create_new=False)
    try:
        return sorted(hit["path"] for hit in fts.search(query_text=text, limit=50))
    finally:
        fts.close()


def _fts_paths(repo: Path, text: str) -> Set[str]:
    """Distinct paths the real on-disk Tantivy index returns for `text`."""
    return set(_fts_hit_paths(repo, text))


def _assert_only_hidden(
    indexer: SmartIndexer, store: _DeletionRecordingStore, excluded: str
) -> None:
    hidden = _hidden_branches_by_path(indexer, store)
    assert "master" in hidden[excluded], (
        f"{excluded} is no longer eligible and must be hidden on master: {hidden}"
    )
    for kept in _ALWAYS_INDEXED:
        assert "master" not in hidden[kept], (
            f"{kept} is still eligible and must stay visible: {hidden}"
        )


class TestNewlyExcludedGitFile:
    def test_newly_excluded_dir_file_is_hidden_when_nothing_else_changed(
        self, tmp_path: Path
    ) -> None:
        repo = _repo_with_extra_files(tmp_path)
        metadata_path = tmp_path / "meta.json"
        _index_everything(repo, metadata_path)

        config = Config(codebase_dir=repo)
        config.exclude_dirs = [*config.exclude_dirs, _EXCLUDED_DIR]
        indexer, embedder, store = _indexer_for(config, metadata_path)
        _reset(embedder, store)
        indexer.smart_index(reconcile_with_database=True, quiet=True)

        assert embedder.embedded_texts == [], embedder.embedded_texts
        _assert_only_hidden(indexer, store, _EXCLUDED_DIR_FILE)
        assert "master" not in _hidden_branches_by_path(indexer, store)[_EXTENSION_FILE]

    def test_newly_excluded_file_leaves_fts_index(self, tmp_path: Path) -> None:
        repo = _repo_with_extra_files(tmp_path)
        metadata_path = tmp_path / "meta.json"
        indexer, _, _ = _indexer_for(Config(codebase_dir=repo), metadata_path)
        indexer.smart_index(force_full=True, quiet=True, enable_fts=True)
        assert _fts_paths(repo, "DELTA") == {_EXCLUDED_DIR_FILE}

        config = Config(codebase_dir=repo)
        config.exclude_dirs = [*config.exclude_dirs, _EXCLUDED_DIR]
        for _ in range(2):
            indexer, embedder, store = _indexer_for(config, metadata_path)
            indexer.smart_index(
                reconcile_with_database=True, quiet=True, enable_fts=True
            )
            assert embedder.embedded_texts == []
            _assert_only_hidden(indexer, store, _EXCLUDED_DIR_FILE)
            assert _fts_paths(repo, "DELTA") == set(), (
                "a newly excluded file must leave full-text search results"
            )
            assert _fts_paths(repo, "ALPHA_MARKER") == {"alpha.py"}

    def test_repeated_reconcile_skips_deletions_already_hidden(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = _repo_with_extra_files(tmp_path)
        metadata_path = tmp_path / "meta.json"
        config = Config(codebase_dir=repo)
        indexer, _, _ = _indexer_for(config, metadata_path)
        indexer.smart_index(force_full=True, quiet=True, enable_fts=True)
        _git(repo, "rm", "-q", "beta.py", _EXTENSION_FILE)
        _git(repo, "commit", "-q", "-m", "delete two files")

        hides_per_run = []
        for _ in range(2):
            indexer, embedder, store = _indexer_for(config, metadata_path)
            caplog.clear()
            with caplog.at_level(logging.INFO):
                indexer.smart_index(
                    reconcile_with_database=True, quiet=True, enable_fts=True
                )
            assert embedder.embedded_texts == []
            hides_per_run.append(
                sorted(
                    r.getMessage().split(": ", 1)[1]
                    for r in caplog.records
                    if r.getMessage().startswith("Hidden file in branch")
                )
            )
            hidden = _hidden_branches_by_path(indexer, store)
            assert "master" in hidden["beta.py"] and "master" in hidden[_EXTENSION_FILE]
            assert _fts_paths(repo, "BETA_MARKER") == set()

        assert hides_per_run == [sorted(["beta.py", _EXTENSION_FILE]), []], (
            f"deletions already hidden on the branch must not be redone: {hides_per_run}"
        )

    def test_excluded_multi_chunk_file_keeps_every_point(self, tmp_path: Path) -> None:
        repo = _repo_with_extra_files(tmp_path)
        big = _commit_big_file(repo)
        metadata_path = tmp_path / "meta.json"
        indexer, _, store = _indexer_for(Config(codebase_dir=repo), metadata_path)
        indexer.smart_index(force_full=True, quiet=True)
        collection = store.resolve_collection_name(
            indexer.config, indexer.embedding_provider
        )

        def big_point_ids() -> Set[str]:
            return {
                p["id"]
                for p in indexer._scroll_all_content_points(collection)
                if p["payload"]["path"] == big
            }

        original = big_point_ids()
        assert len(original) >= 2, "the test needs a multi-chunk file"

        excluding = Config(codebase_dir=repo)
        excluding.exclude_dirs = [*excluding.exclude_dirs, _EXCLUDED_DIR]
        indexer, embedder, store = _indexer_for(excluding, metadata_path)
        indexer.smart_index(reconcile_with_database=True, quiet=True)
        assert embedder.embedded_texts == []
        assert big_point_ids() == original, "hiding a file must keep every chunk"

        indexer, embedder, store = _indexer_for(
            Config(codebase_dir=repo), metadata_path
        )
        indexer.smart_index(reconcile_with_database=True, quiet=True)
        assert embedder.embedded_texts == []
        assert big_point_ids() == original, "un-hiding a file must keep every chunk"
        hidden = _hidden_branches_by_path(indexer, store)
        assert "master" not in hidden[big], hidden

    def test_obsolete_working_dir_cleanup_keeps_every_chunk(
        self, tmp_path: Path
    ) -> None:
        repo = _repo_with_extra_files(tmp_path)
        big = _commit_big_file(repo)
        indexer, _, store = _indexer_for(Config(codebase_dir=repo), tmp_path / "m.json")
        indexer.smart_index(force_full=True, quiet=True)
        collection = store.resolve_collection_name(
            indexer.config, indexer.embedding_provider
        )

        def big_points() -> Dict[str, Dict[str, Any]]:
            return {
                p["id"]: p["payload"]
                for p in indexer._scroll_all_content_points(collection)
                if p["payload"]["path"] == big
            }

        original = big_points()
        assert len(original) >= 3, "the test needs a multi-chunk file"
        # Both working-dir and committed content visible for the same file
        # (the git-restore case the cleanup exists for).
        working_dir_ids = sorted(original)[:2]
        assert store._batch_update_payload_only(
            [
                {"id": pid, "payload": {"git_commit_hash": "working_dir_example"}}
                for pid in working_dir_ids
            ],
            collection,
        )

        indexer._cleanup_multiple_visible_content_points(collection, "master")

        after = big_points()
        assert set(after) == set(original), "the cleanup must keep every chunk"
        hidden_ids = sorted(
            pid for pid, p in after.items() if "master" in p.get("hidden_branches", [])
        )
        assert hidden_ids == working_dir_ids

    def test_single_file_hide_and_unhide_cover_more_than_one_page(
        self, tmp_path: Path
    ) -> None:
        repo = _repo_with_extra_files(tmp_path)
        indexer, _, store = _indexer_for(Config(codebase_dir=repo), tmp_path / "m.json")
        indexer.smart_index(force_full=True, quiet=True)
        collection = store.resolve_collection_name(
            indexer.config, indexer.embedding_provider
        )
        many = "vendor_lib/many.py"
        point_count = 1100  # more than one 1,000-point fetch
        store.upsert_points(
            collection,
            [
                {
                    "id": f"{i:032x}",
                    "vector": [float(i % 7) + 1.0] * VECTOR_DIM,
                    "payload": {
                        "type": "content",
                        "path": many,
                        "git_commit_hash": "0" * 40,
                        "line_start": i + 1,
                        "line_end": i + 1,
                    },
                }
                for i in range(point_count)
            ],
        )

        def hidden_states() -> List[bool]:
            return [
                "master" in p["payload"].get("hidden_branches", [])
                for p in indexer._scroll_all_content_points(collection)
                if p["payload"]["path"] == many
            ]

        assert len(hidden_states()) == point_count

        indexer._hide_file_in_branch_thread_safe(many, "master", collection)
        states = hidden_states()
        assert len(states) == point_count and all(states), (
            f"{states.count(False)} of {point_count} points left visible"
        )

        indexer._ensure_file_visible_in_branch_thread_safe(many, "master", collection)
        states = hidden_states()
        assert len(states) == point_count and not any(states), (
            f"{states.count(True)} of {point_count} points left hidden"
        )

    def test_mixed_visibility_excluded_file_is_fully_hidden(
        self, tmp_path: Path
    ) -> None:
        repo = _repo_with_extra_files(tmp_path)
        big = _commit_big_file(repo)
        metadata_path = tmp_path / "meta.json"
        indexer, _, store = _indexer_for(Config(codebase_dir=repo), metadata_path)
        indexer.smart_index(force_full=True, quiet=True)

        collection = store.resolve_collection_name(
            indexer.config, indexer.embedding_provider
        )

        def big_points() -> List[Dict[str, Any]]:
            return [
                p
                for p in indexer._scroll_all_content_points(collection)
                if p["payload"]["path"] == big
            ]

        assert len(big_points()) >= 2, "the test needs a multi-chunk file"

        def set_hidden(point_id: Any, hidden: List[str]) -> None:
            assert store._batch_update_payload_only(
                [{"id": point_id, "payload": {"hidden_branches": hidden}}], collection
            )

        # Hide every point, then leave exactly one visible such that the
        # first point the snapshot sees says "hidden" while the file is still
        # visible through another point. Rewriting a point can move it in
        # the storage scan order, so try each candidate (bounded).
        candidates = [p["id"] for p in big_points()]
        for point_id in candidates:
            set_hidden(point_id, ["master"])
        mixed = False
        for point_id in candidates:
            set_hidden(point_id, [])
            if "master" in big_points()[0]["payload"]["hidden_branches"]:
                mixed = True
                break
            set_hidden(point_id, ["master"])
        assert mixed, "could not build a first-point-hidden mixed state"

        config = Config(codebase_dir=repo)
        config.exclude_dirs = [*config.exclude_dirs, _EXCLUDED_DIR]
        indexer, embedder, store = _indexer_for(config, metadata_path)
        indexer.smart_index(reconcile_with_database=True, quiet=True)

        assert embedder.embedded_texts == []
        visible = [
            p["id"]
            for p in indexer._scroll_all_content_points(collection)
            if p["payload"]["path"] == big
            and "master" not in p["payload"].get("hidden_branches", [])
        ]
        assert visible == [], f"points of {big} still visible on master: {visible}"

    def test_reincluded_file_returns_to_fts_without_embedding(
        self, tmp_path: Path
    ) -> None:
        repo = _repo_with_extra_files(tmp_path)
        metadata_path = tmp_path / "meta.json"
        indexer, _, _ = _indexer_for(Config(codebase_dir=repo), metadata_path)
        indexer.smart_index(force_full=True, quiet=True, enable_fts=True)

        excluding = Config(codebase_dir=repo)
        excluding.exclude_dirs = [*excluding.exclude_dirs, _EXCLUDED_DIR]
        indexer, _, _ = _indexer_for(excluding, metadata_path)
        indexer.smart_index(reconcile_with_database=True, quiet=True, enable_fts=True)
        assert _fts_paths(repo, "DELTA") == set()

        indexer, embedder, store = _indexer_for(
            Config(codebase_dir=repo), metadata_path
        )
        indexer.smart_index(reconcile_with_database=True, quiet=True, enable_fts=True)

        assert embedder.embedded_texts == [], embedder.embedded_texts
        assert (
            "master" not in _hidden_branches_by_path(indexer, store)[_EXCLUDED_DIR_FILE]
        )
        assert _fts_hit_paths(repo, "DELTA") == [_EXCLUDED_DIR_FILE], (
            "a re-included file must be back in full-text search, exactly once"
        )

    def test_failed_fts_restore_is_retried_by_next_reconcile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo_with_extra_files(tmp_path)
        metadata_path = tmp_path / "meta.json"
        indexer, _, _ = _indexer_for(Config(codebase_dir=repo), metadata_path)
        indexer.smart_index(force_full=True, quiet=True, enable_fts=True)
        excluding = Config(codebase_dir=repo)
        excluding.exclude_dirs = [*excluding.exclude_dirs, _EXCLUDED_DIR]
        indexer, _, _ = _indexer_for(excluding, metadata_path)
        indexer.smart_index(reconcile_with_database=True, quiet=True, enable_fts=True)
        assert _fts_paths(repo, "DELTA") == set()

        # Fault injection at the external FTS library boundary: the add for
        # the re-included file fails once.
        real_add = TantivyIndexManager.add_document

        def failing_add(self: TantivyIndexManager, doc: Dict[str, Any]) -> None:
            if doc.get("path") == _EXCLUDED_DIR_FILE:
                raise OSError("injected FTS write failure")
            real_add(self, doc)

        monkeypatch.setattr(TantivyIndexManager, "add_document", failing_add)
        indexer, embedder, store = _indexer_for(
            Config(codebase_dir=repo), metadata_path
        )
        indexer.smart_index(reconcile_with_database=True, quiet=True, enable_fts=True)
        assert embedder.embedded_texts == []
        assert (
            "master" not in _hidden_branches_by_path(indexer, store)[_EXCLUDED_DIR_FILE]
        )
        assert _fts_paths(repo, "DELTA") == set()

        monkeypatch.undo()
        indexer, embedder, _ = _indexer_for(Config(codebase_dir=repo), metadata_path)
        indexer.smart_index(reconcile_with_database=True, quiet=True, enable_fts=True)
        assert embedder.embedded_texts == []
        assert _fts_hit_paths(repo, "DELTA") == [_EXCLUDED_DIR_FILE], (
            "the next reconcile must retry the failed FTS restore"
        )

    def test_fts_restore_propagates_uninitialized_writer(self, tmp_path: Path) -> None:
        """A writer that was never initialized is a wiring bug: it must
        surface, as on the per-file indexing path, not be logged away."""
        repo = _repo_with_extra_files(tmp_path)
        indexer, _, _ = _indexer_for(Config(codebase_dir=repo), tmp_path / "m.json")
        never_initialized = TantivyIndexManager(tmp_path / "fts_never_opened")
        with pytest.raises(RuntimeError, match="not initialized"):
            indexer._restore_file_in_fts(never_initialized, "alpha.py")

    def test_filter_excluding_every_file_hides_all_stored_files(
        self, tmp_path: Path
    ) -> None:
        repo = _repo_with_extra_files(tmp_path)
        metadata_path = tmp_path / "meta.json"
        _index_everything(repo, metadata_path)

        config = Config(codebase_dir=repo)
        config.file_extensions = ["nomatchext"]
        indexer, embedder, store = _indexer_for(config, metadata_path)
        assert list(indexer.file_finder.find_files()) == []
        _reset(embedder, store)
        indexer.smart_index(reconcile_with_database=True, quiet=True)

        assert embedder.embedded_texts == []
        hidden = _hidden_branches_by_path(indexer, store)
        not_hidden = sorted(p for p, b in hidden.items() if "master" not in b)
        assert not_hidden == [], (
            f"every stored file is excluded and must be hidden on master: {hidden}"
        )

    def test_removed_extension_file_is_hidden_when_nothing_else_changed(
        self, tmp_path: Path
    ) -> None:
        repo = _repo_with_extra_files(tmp_path)
        metadata_path = tmp_path / "meta.json"
        _index_everything(repo, metadata_path)

        config = Config(codebase_dir=repo)
        config.file_extensions = [e for e in config.file_extensions if e != "go"]
        indexer, embedder, store = _indexer_for(config, metadata_path)
        _reset(embedder, store)
        indexer.smart_index(reconcile_with_database=True, quiet=True)

        assert embedder.embedded_texts == [], embedder.embedded_texts
        _assert_only_hidden(indexer, store, _EXTENSION_FILE)
        assert (
            "master" not in _hidden_branches_by_path(indexer, store)[_EXCLUDED_DIR_FILE]
        )
