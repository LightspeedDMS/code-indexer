"""Indexer resume state:
a reconcile run where file analysis fails completes with the failures
recorded -- never silently, and never by leaving the operation open.

``_do_reconcile_with_database()``'s per-file loop wraps content-id
analysis in a broad ``except Exception: continue`` -- a file whose
analysis throws is skipped, never added to ``files_to_index``. Marking
the run completed while hiding those files would be a silent partial index
(Bug #1218-class). Leaving the status open instead makes every following
server-context run fall back to a full reconcile again, forever. The run
is therefore marked completed with the failed files recorded in
``failed_files`` / ``failed_file_paths``.

These tests drive the REAL ``SmartIndexer.smart_index()`` entry point
against a real git repository and a real ``FilesystemVectorStore``,
forcing every file's content-id analysis to raise.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from code_indexer.config import Config
from code_indexer.services.embedding_provider import (
    BatchEmbeddingResult,
    EmbeddingProvider,
    EmbeddingResult,
)
from code_indexer.services.smart_indexer import SmartIndexer
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

_VECTOR_DIM = 16
_BYTE_MAX_VALUE = 255.0
_VECTOR_SCALE = 2.0
_VECTOR_OFFSET = 1.0
_FAKE_MAX_TOKENS = 8192


def _deterministic_embedding(text: str) -> List[float]:
    """Real (non-mocked), deterministic local embedding: no network call."""
    import hashlib

    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [
        (digest[i % len(digest)] / _BYTE_MAX_VALUE) * _VECTOR_SCALE - _VECTOR_OFFSET
        for i in range(_VECTOR_DIM)
    ]


class _DeterministicHashEmbeddingProvider(EmbeddingProvider):
    """Real, fully-working EmbeddingProvider (no mocking) -- local
    duplicate of the identical helper used by the other resume-state
    reconcile tests, kept local per this project's own precedent rather
    than a cross-test-module import."""

    def get_embedding(
        self,
        text: str,
        model: Optional[str] = None,
        embedding_purpose: Optional[str] = None,
    ) -> List[float]:
        return _deterministic_embedding(text)

    def get_embeddings_batch(
        self, texts: List[str], model: Optional[str] = None
    ) -> List[List[float]]:
        return [_deterministic_embedding(t) for t in texts]

    def get_embedding_with_metadata(
        self, text: str, model: Optional[str] = None
    ) -> EmbeddingResult:
        return EmbeddingResult(
            embedding=_deterministic_embedding(text), model=self.get_current_model()
        )

    def get_embeddings_batch_with_metadata(
        self, texts: List[str], model: Optional[str] = None
    ) -> BatchEmbeddingResult:
        return BatchEmbeddingResult(
            embeddings=[_deterministic_embedding(t) for t in texts],
            model=self.get_current_model(),
        )

    def health_check(self, *, test_api: bool = False) -> bool:
        return True

    def get_model_info(self) -> Dict[str, int]:
        return {"dimensions": _VECTOR_DIM, "max_tokens": _FAKE_MAX_TOKENS}

    def get_provider_name(self) -> str:
        return "deterministic-test-provider"

    def get_current_model(self) -> str:
        return "deterministic-test-model"

    def supports_batch_processing(self) -> bool:
        return True

    def _get_model_token_limit(self) -> int:
        return _FAKE_MAX_TOKENS


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _make_indexer(repo: Path, metadata_path: Path) -> SmartIndexer:
    config = Config(codebase_dir=repo)
    embedding_provider = _DeterministicHashEmbeddingProvider()
    vector_store = FilesystemVectorStore(base_path=repo / ".code-indexer" / "index")
    vector_store.ensure_provider_aware_collection(config, embedding_provider)
    return SmartIndexer(
        config=config,
        embedding_provider=embedding_provider,
        vector_store_client=vector_store,
        metadata_path=metadata_path,
    )


def _build_repo_with_five_files(repo: Path) -> List[Path]:
    """Real git repo with 5 committed files, each with unique content."""
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _run_git(repo, "config", "user.email", "test@test.com")
    _run_git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text(".code-indexer/\n")

    files = []
    for i, name in enumerate(["a", "b", "c", "d", "e"]):
        f = repo / f"{name}.py"
        f.write_text(f"# file {name} unique content marker {i}\n")
        files.append(f)

    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "initial: five files")
    return files


class TestReconcileAnalysisFailureRecordedCompletion:
    """Reconcile marks the operation completed with every analysis failure
    recorded, so the failures stay visible and the next run does not
    reconcile again."""

    def test_all_files_failing_analysis_completes_with_recorded_failures(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        files = _build_repo_with_five_files(repo)
        metadata_path = tmp_path / "metadata.json"
        indexer = _make_indexer(repo, metadata_path)

        indexer.progressive_metadata.metadata["status"] = "in_progress"
        indexer.progressive_metadata._save_metadata()

        with patch.object(
            indexer,
            "_get_effective_content_id_for_reconcile",
            side_effect=RuntimeError("simulated analysis failure"),
        ):
            stats = indexer.smart_index(force_full=False, reconcile_with_database=True)

        assert stats.files_processed == 0, (
            "No file should have been queued for indexing when every "
            f"file's analysis raised. Got files_processed={stats.files_processed}"
        )
        metadata = indexer.progressive_metadata.metadata
        assert metadata["status"] == "completed"
        assert metadata["failed_files"] == len(files), (
            "The analysis failures must be recorded, not hidden behind the "
            f"completion. Got failed_files={metadata['failed_files']!r}"
        )
        assert sorted(metadata["failed_file_paths"]) == sorted(str(f) for f in files)

    def test_server_context_analysis_failures_do_not_reconcile_every_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path / "server-data"))
        repo = tmp_path / "repo"
        _build_aged_repo(repo)
        metadata_path = tmp_path / "metadata.json"
        indexer = _make_indexer(repo, metadata_path)
        indexer.progressive_metadata.metadata["status"] = "in_progress"
        indexer.progressive_metadata._save_metadata()

        with patch.object(
            indexer,
            "_get_effective_content_id_for_reconcile",
            side_effect=RuntimeError("simulated analysis failure"),
        ):
            indexer.smart_index(force_full=False, trust_resume_state=False)

        infos: List[str] = []

        def collecting_callback(
            current: int, total: int, path: Path, info: Optional[str] = None, **_: Any
        ) -> None:
            if info:
                infos.append(info)

        _make_indexer(repo, metadata_path).smart_index(
            force_full=False,
            trust_resume_state=False,
            progress_callback=collecting_callback,
        )
        assert not any("Reconcile:" in i for i in infos), (
            f"The follow-up server run reconciled again: {infos}"
        )


_FAILING_FILE = "b.py"


def _fail_analysis_for(indexer: SmartIndexer, relative_path: str) -> Any:
    """Patch content-id analysis so it raises for ONE file only."""
    original = indexer._get_effective_content_id_for_reconcile

    def _analysis(rel: str) -> Any:
        if rel == relative_path:
            raise RuntimeError("simulated analysis failure")
        return original(rel)

    return patch.object(
        indexer, "_get_effective_content_id_for_reconcile", side_effect=_analysis
    )


def _has_indexed_points(indexer: SmartIndexer, repo: Path, relative_path: str) -> bool:
    store = indexer.vector_store_client
    collection = store.resolve_collection_name(
        indexer.config, indexer.embedding_provider
    )
    for candidate in (relative_path, str(repo / relative_path)):
        points, _ = store.scroll_points(
            collection_name=collection,
            filter_conditions={
                "must": [{"key": "path", "match": {"value": candidate}}]
            },
            limit=1,
            with_payload=False,
            with_vectors=False,
        )
        if points:
            return True
    return False


def _context_kwargs(server: bool) -> Dict[str, Any]:
    return {"trust_resume_state": False} if server else {}


_AGE_SECONDS = 3600


def _build_aged_repo(repo: Path) -> List[Path]:
    """The five-file repo with mtimes an hour in the past, so an incremental
    run's modification-time scan (which keeps a safety buffer) selects only
    files that genuinely changed."""
    import os
    import time

    files = _build_repo_with_five_files(repo)
    past = time.time() - _AGE_SECONDS
    for f in files:
        os.utime(f, (past, past))
    return files


class TestAnalysisFailuresReportedAndRetried:
    """Files whose analysis fails are reported in the returned stats, and a
    later run retries them until they are indexed."""

    @pytest.fixture(autouse=True)
    def _isolated_server_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path / "server-data"))

    def test_stats_report_failures_when_nothing_else_is_indexed(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        _build_aged_repo(repo)
        indexer = _make_indexer(repo, tmp_path / "metadata.json")

        with patch.object(
            indexer,
            "_get_effective_content_id_for_reconcile",
            side_effect=RuntimeError("simulated analysis failure"),
        ):
            stats = indexer.smart_index(force_full=False, reconcile_with_database=True)

        assert stats.files_processed == 0
        assert stats.failed_files == 5
        assert stats.failed_paths == frozenset({"a.py", "b.py", "c.py", "d.py", "e.py"})

    def test_stats_report_failures_alongside_indexed_files(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        _build_aged_repo(repo)
        indexer = _make_indexer(repo, tmp_path / "metadata.json")

        with _fail_analysis_for(indexer, _FAILING_FILE):
            stats = indexer.smart_index(force_full=False, reconcile_with_database=True)

        assert stats.files_processed == 4
        assert stats.failed_files == 1
        assert stats.failed_paths == frozenset({_FAILING_FILE})
        assert not _has_indexed_points(indexer, repo, _FAILING_FILE)

    @pytest.mark.parametrize("server", [False, True])
    def test_failed_file_is_retried_on_next_unchanged_run(
        self, tmp_path: Path, server: bool
    ) -> None:
        repo = tmp_path / "repo"
        _build_aged_repo(repo)
        metadata_path = tmp_path / "metadata.json"
        first = _make_indexer(repo, metadata_path)
        with _fail_analysis_for(first, _FAILING_FILE):
            first.smart_index(
                force_full=False,
                reconcile_with_database=True,
                **_context_kwargs(server),
            )
        assert first.progressive_metadata.metadata["failed_file_paths"] == [
            str(repo / _FAILING_FILE)
        ]

        second = _make_indexer(repo, metadata_path)
        stats = second.smart_index(force_full=False, **_context_kwargs(server))

        assert stats.files_processed == 1, (
            "The next run with no git or file changes must retry the file whose "
            f"analysis failed. Got files_processed={stats.files_processed}"
        )
        assert stats.failed_files == 0
        assert _has_indexed_points(second, repo, _FAILING_FILE)
        stored = second.progressive_metadata.metadata
        assert stored["failed_file_paths"] == []
        assert stored["status"] == "completed"

    def test_resumed_run_retries_recorded_failures(self, tmp_path: Path) -> None:
        import json

        repo = tmp_path / "repo"
        _build_aged_repo(repo)
        metadata_path = tmp_path / "metadata.json"
        first = _make_indexer(repo, metadata_path)
        with _fail_analysis_for(first, _FAILING_FILE):
            first.smart_index(force_full=False, reconcile_with_database=True)
        stored = json.loads(metadata_path.read_text())
        stored.update(
            {
                "status": "in_progress",
                "files_to_index": [str(repo / "a.py")],
                "total_files_to_index": 1,
                "current_file_index": 0,
                "completed_files": [],
            }
        )
        metadata_path.write_text(json.dumps(stored))

        second = _make_indexer(repo, metadata_path)
        stats = second.smart_index(force_full=False)

        assert stats.files_processed == 2
        assert _has_indexed_points(second, repo, _FAILING_FILE)
        assert json.loads(metadata_path.read_text())["failed_file_paths"] == []

    def test_server_ignores_recorded_failures_without_a_valid_seal(
        self, tmp_path: Path
    ) -> None:
        """Server context: the recorded failure list is used only when the
        server sealed it, and it does not carry into this run's sealed save."""
        import json

        repo = tmp_path / "repo"
        _build_aged_repo(repo)
        metadata_path = tmp_path / "metadata.json"
        _make_indexer(repo, metadata_path).smart_index(
            force_full=False, reconcile_with_database=True, trust_resume_state=False
        )
        stored = json.loads(metadata_path.read_text())
        stored["failed_file_paths"] = [str(repo / "a.py")]  # seal no longer matches
        metadata_path.write_text(json.dumps(stored))

        indexer = _make_indexer(repo, metadata_path)
        stats = indexer.smart_index(force_full=False, trust_resume_state=False)

        assert stats.files_processed == 0
        assert json.loads(metadata_path.read_text())["failed_file_paths"] == []

    def test_recorded_paths_missing_or_outside_the_root_drop_out(
        self, tmp_path: Path
    ) -> None:
        import json

        repo = tmp_path / "repo"
        _build_aged_repo(repo)
        (tmp_path / "outside.py").write_text("def outside():\n    return 0\n")
        metadata_path = tmp_path / "metadata.json"
        _make_indexer(repo, metadata_path).smart_index(
            force_full=False, reconcile_with_database=True
        )
        stored = json.loads(metadata_path.read_text())
        stored["failed_file_paths"] = [
            str(repo / "gone.py"),
            str(repo / ".." / "outside.py"),
        ]
        metadata_path.write_text(json.dumps(stored))

        indexer = _make_indexer(repo, metadata_path)
        stats = indexer.smart_index(force_full=False)

        assert stats.files_processed == 0
        assert json.loads(metadata_path.read_text())["failed_file_paths"] == []


_FAIL_ONCE_MARKER = "embedding_fails_once_marker"
_NEW_FILE = "new_module.py"


class _FailOnceEmbeddingProvider(_DeterministicHashEmbeddingProvider):
    """Real deterministic provider whose FIRST embedding call for text
    carrying the marker raises, as a transient embedder-call failure."""

    def __init__(self) -> None:
        super().__init__()
        self.failed_once = False

    def _maybe_fail(self, texts: List[str]) -> None:
        if not self.failed_once and any(_FAIL_ONCE_MARKER in t for t in texts):
            self.failed_once = True
            raise RuntimeError("simulated transient embedding failure")

    def get_embeddings_batch(
        self, texts: List[str], model: Optional[str] = None
    ) -> List[List[float]]:
        self._maybe_fail(texts)
        return super().get_embeddings_batch(texts, model)

    def get_embeddings_batch_with_metadata(
        self, texts: List[str], model: Optional[str] = None
    ) -> BatchEmbeddingResult:
        self._maybe_fail(texts)
        return super().get_embeddings_batch_with_metadata(texts, model)


def _make_indexer_with(
    repo: Path, metadata_path: Path, provider: EmbeddingProvider
) -> SmartIndexer:
    config = Config(codebase_dir=repo)
    vector_store = FilesystemVectorStore(base_path=repo / ".code-indexer" / "index")
    vector_store.ensure_provider_aware_collection(config, provider)
    return SmartIndexer(
        config=config,
        embedding_provider=provider,
        vector_store_client=vector_store,
        metadata_path=metadata_path,
    )


def _commit_aged_new_file(repo: Path) -> Path:
    """Commit a new file and age its mtime, so only the git delta (not the
    modification-time scan) selects it."""
    import os
    import time

    new_file = repo / _NEW_FILE
    new_file.write_text(f"def fresh():\n    return '{_FAIL_ONCE_MARKER}'\n")
    _run_git(repo, "add", _NEW_FILE)
    _run_git(repo, "commit", "-m", "add new module")
    past = time.time() - _AGE_SECONDS
    os.utime(new_file, (past, past))
    return new_file


class TestNewFileFailuresRecorded:
    """A file that fails on its FIRST pass (not a retry) is recorded and
    retried by the next run, even after the commit watermark advanced."""

    @pytest.fixture(autouse=True)
    def _isolated_server_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path / "server-data"))

    @pytest.mark.parametrize("server", [False, True])
    def test_new_file_failing_incremental_pass_is_recorded_and_retried(
        self, tmp_path: Path, server: bool
    ) -> None:
        repo = tmp_path / "repo"
        _build_aged_repo(repo)
        metadata_path = tmp_path / "metadata.json"
        _make_indexer(repo, metadata_path).smart_index(
            force_full=True, **_context_kwargs(server)
        )
        new_file = _commit_aged_new_file(repo)

        failing = _make_indexer_with(repo, metadata_path, _FailOnceEmbeddingProvider())
        stats = failing.smart_index(force_full=False, **_context_kwargs(server))

        assert stats.failed_files == 1
        assert _NEW_FILE in stats.failed_paths
        assert failing.progressive_metadata.metadata["failed_file_paths"] == [
            str(new_file)
        ], "A file that failed on its first incremental pass must be recorded."

        retry = _make_indexer(repo, metadata_path)
        retry_stats = retry.smart_index(force_full=False, **_context_kwargs(server))

        assert retry_stats.files_processed == 1
        assert retry_stats.failed_files == 0
        assert _has_indexed_points(retry, repo, _NEW_FILE)
        assert retry.progressive_metadata.metadata["failed_file_paths"] == []

    def test_new_file_failing_resumed_pass_is_recorded_and_retried(
        self, tmp_path: Path
    ) -> None:
        import json

        repo = tmp_path / "repo"
        _build_aged_repo(repo)
        metadata_path = tmp_path / "metadata.json"
        _make_indexer(repo, metadata_path).smart_index(force_full=True)
        new_file = _commit_aged_new_file(repo)
        stored = json.loads(metadata_path.read_text())
        stored.update(
            {
                "status": "in_progress",
                "files_to_index": [str(new_file)],
                "total_files_to_index": 1,
                "current_file_index": 0,
                "completed_files": [],
                "failed_file_paths": [],
            }
        )
        metadata_path.write_text(json.dumps(stored))

        failing = _make_indexer_with(repo, metadata_path, _FailOnceEmbeddingProvider())
        stats = failing.smart_index(force_full=False)

        assert stats.failed_files == 1
        assert failing.progressive_metadata.metadata["failed_file_paths"] == [
            str(new_file)
        ]

        retry = _make_indexer(repo, metadata_path)
        retry.smart_index(force_full=False)

        assert _has_indexed_points(retry, repo, _NEW_FILE)
        assert retry.progressive_metadata.metadata["failed_file_paths"] == []
