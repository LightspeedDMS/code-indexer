"""Issue #2013: `cidx index --reconcile` on a NON-git directory must not
delete and re-embed every file.

The reconcile pass compares the content id derived from the stored point
payload (`filesystem_mtime` = integer mtime, `filesystem_size`) with the
content id built from the file on disk. Both sides must use the SAME
format, otherwise every file looks modified and the non-git branch
deletes its points and re-embeds it on every run.

Real `SmartIndexer`, real `FilesystemVectorStore`, real temp directories
and real git (for the git-repository parity case). The only test double
is the embedding provider (an external service): it counts the texts it
is asked to embed. The vector store is the real store, subclassed only to
record deletion calls before delegating to the real implementation.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from code_indexer.config import Config
from code_indexer.services.embedding_provider import (
    BatchEmbeddingResult,
    EmbeddingProvider,
    EmbeddingResult,
)
from code_indexer.services.smart_indexer import SmartIndexer
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

VECTOR_DIM = 8

# Far above the handful of points these tests create; the helper asserts
# the scroll returned no continuation offset, so nothing is ever truncated.
_SCROLL_LIMIT = 1000

# Fractional mtimes on purpose: the stored payload keeps int(st_mtime), so a
# disk side built from the raw float st_mtime can never match it.
_BASE_MTIME = 1_700_000_000.75


class _CountingEmbeddingProvider(EmbeddingProvider):
    """Deterministic in-process embedder that records every embedded text."""

    def __init__(self) -> None:
        self.embedded_texts: List[str] = []
        # Run once, inside the first embed call (simulates work happening on
        # disk while the provider round-trip is in flight).
        self.before_first_embed: Optional[Callable[[], None]] = None
        self._lock = threading.Lock()

    def _record(self, texts: List[str]) -> None:
        with self._lock:
            hook, self.before_first_embed = self.before_first_embed, None
        if hook is not None:
            hook()
        with self._lock:
            self.embedded_texts.extend(texts)

    def _vec(self, text: str) -> List[float]:
        return [float((len(text) + i) % 7) + 1.0 for i in range(VECTOR_DIM)]

    def get_embedding(
        self,
        text: str,
        model: Optional[str] = None,
        embedding_purpose: Optional[str] = None,
    ) -> List[float]:
        self._record([text])
        return self._vec(text)

    def get_embeddings_batch(self, texts, model=None, **kwargs):
        batch = list(texts)
        self._record(batch)
        return [self._vec(t) for t in batch]

    def get_embedding_with_metadata(self, text, model=None):
        self._record([text])
        return EmbeddingResult(embedding=self._vec(text), model="test")

    def get_embeddings_batch_with_metadata(self, texts, model=None):
        batch = list(texts)
        self._record(batch)
        return BatchEmbeddingResult(
            embeddings=[self._vec(t) for t in batch], model="test"
        )

    def health_check(self, *, test_api: bool = False) -> bool:
        return True

    def get_model_info(self) -> Dict[str, Any]:
        return {"dimensions": VECTOR_DIM, "max_tokens": 8192}

    def _get_model_token_limit(self) -> int:
        return 120000

    def get_provider_name(self) -> str:
        return "counting-test-provider"

    def get_current_model(self) -> str:
        return "counting-test-model"

    def supports_batch_processing(self) -> bool:
        return True


class _DeletionRecordingStore(FilesystemVectorStore):
    """The real store; records deletion calls, then delegates to the real code."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.deletion_calls: List[str] = []

    def delete_by_filter(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        self.deletion_calls.append(f"delete_by_filter {args[1:]} {kwargs}")
        return super().delete_by_filter(*args, **kwargs)

    def delete_points(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        self.deletion_calls.append(f"delete_points {args[1:]} {kwargs}")
        return super().delete_points(*args, **kwargs)


_FILES = {
    "alpha.py": "def alpha_marker_function():\n    return 'ALPHA_MARKER'\n",
    "beta.py": "def beta_marker_function():\n    return 'BETA_MARKER'\n",
    "gamma.py": "def gamma_marker_function():\n    return 'GAMMA_MARKER'\n",
}


def _write_files(repo: Path) -> None:
    for offset, (name, content) in enumerate(sorted(_FILES.items())):
        path = repo / name
        path.write_text(content)
        mtime = _BASE_MTIME + offset
        os.utime(path, (mtime, mtime))


def _make_indexer(
    repo: Path, metadata_path: Path
) -> Tuple[SmartIndexer, _CountingEmbeddingProvider, _DeletionRecordingStore]:
    config = Config(codebase_dir=repo)
    embedder = _CountingEmbeddingProvider()
    store = _DeletionRecordingStore(base_path=repo / ".code-indexer" / "index")
    store.ensure_provider_aware_collection(config, embedder)
    indexer = SmartIndexer(
        config=config,
        embedding_provider=embedder,
        vector_store_client=store,
        metadata_path=metadata_path,
    )
    return indexer, embedder, store


def _reset(
    embedder: _CountingEmbeddingProvider, store: _DeletionRecordingStore
) -> None:
    embedder.embedded_texts.clear()
    store.deletion_calls.clear()


def _point_ids_by_path(
    indexer: SmartIndexer, store: _DeletionRecordingStore
) -> Dict[str, frozenset]:
    collection = store.resolve_collection_name(
        indexer.config, indexer.embedding_provider
    )
    points, next_offset = store.scroll_points(
        collection_name=collection,
        filter_conditions={"must": [{"key": "type", "match": {"value": "content"}}]},
        limit=_SCROLL_LIMIT,
        with_payload=True,
        with_vectors=False,
    )
    assert next_offset is None, "scroll did not return every content point"
    by_path: Dict[str, Set[str]] = {}
    for point in points:
        by_path.setdefault(point["payload"]["path"], set()).add(point["id"])
    return {path: frozenset(ids) for path, ids in by_path.items()}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _non_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "plain"
    repo.mkdir()
    # A plain dir must not sit inside a git work tree, or the test would
    # silently exercise the git path.
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert probe.returncode != 0, "tmp_path unexpectedly lives inside a git repo"
    _write_files(repo)
    return repo


class TestNonGitReconcileContentId:
    def test_reconcile_without_changes_embeds_and_deletes_nothing(
        self, tmp_path: Path
    ) -> None:
        repo = _non_git_repo(tmp_path)
        indexer, embedder, store = _make_indexer(repo, tmp_path / "meta.json")
        assert not indexer.is_git_aware()

        indexer.smart_index(force_full=True, quiet=True)
        assert embedder.embedded_texts, "initial full index must embed the files"

        _reset(embedder, store)
        indexer.smart_index(reconcile_with_database=True, quiet=True)

        assert embedder.embedded_texts == [], (
            "a reconcile with no file changes re-embedded content: "
            f"{embedder.embedded_texts}"
        )
        assert store.deletion_calls == [], (
            f"a reconcile with no file changes deleted points: {store.deletion_calls}"
        )

    def test_reconcile_reembeds_only_the_modified_file(self, tmp_path: Path) -> None:
        repo = _non_git_repo(tmp_path)
        indexer, embedder, store = _make_indexer(repo, tmp_path / "meta.json")
        indexer.smart_index(force_full=True, quiet=True)

        changed = repo / "beta.py"
        changed.write_text(
            "def beta_marker_function():\n    return 'BETA_MARKER_CHANGED_2013'\n"
        )
        mtime = _BASE_MTIME + 500
        os.utime(changed, (mtime, mtime))

        before = _point_ids_by_path(indexer, store)
        _reset(embedder, store)
        indexer.smart_index(reconcile_with_database=True, quiet=True)
        after = _point_ids_by_path(indexer, store)

        assert embedder.embedded_texts, "the modified file must be re-embedded"
        assert all("BETA_MARKER_CHANGED_2013" in t for t in embedder.embedded_texts), (
            f"only the modified file may be re-embedded, got: {embedder.embedded_texts}"
        )
        for unchanged in ("alpha.py", "gamma.py"):
            assert after[unchanged] == before[unchanged], (
                f"{unchanged} was not modified, its points must survive the "
                f"reconcile untouched: before={before} after={after}"
            )
        assert after["beta.py"] and after["beta.py"] != before["beta.py"], (
            f"beta.py's points must be replaced: before={before} after={after}"
        )


class TestNonGitReconcileRacyTimestamp:
    def test_same_second_same_size_edit_is_reindexed(self, tmp_path: Path) -> None:
        """Racy-timestamp rule: mtime+size equality is only trusted when the
        file's mtime second is strictly older than the second it was indexed
        in. A file whose mtime is at/after its indexing second may have been
        rewritten within that same second (same size, same int mtime), so its
        content must be checked."""
        repo = _non_git_repo(tmp_path)
        racy = repo / "racy.py"
        racy.write_text("def racy_marker():\n    return 'RACY_ORIGINAL'\n")
        # The file's mtime second is not older than the indexing second
        # (deterministic stand-in for "written in the same second it was
        # indexed in").
        racy_mtime = float(int(time.time()) + 60)
        os.utime(racy, (racy_mtime, racy_mtime))

        indexer, embedder, store = _make_indexer(repo, tmp_path / "meta.json")
        indexer.smart_index(force_full=True, quiet=True)

        # Unchanged racy file: content check confirms it, nothing re-embedded.
        _reset(embedder, store)
        indexer.smart_index(reconcile_with_database=True, quiet=True)
        assert embedder.embedded_texts == []
        assert store.deletion_calls == []

        new_content = "def racy_marker():\n    return 'RACY_EDITED__'\n"
        assert len(new_content) == len(racy.read_text())
        racy.write_text(new_content)
        os.utime(racy, (racy_mtime, racy_mtime))

        before = _point_ids_by_path(indexer, store)
        _reset(embedder, store)
        indexer.smart_index(reconcile_with_database=True, quiet=True)
        after = _point_ids_by_path(indexer, store)

        assert embedder.embedded_texts, (
            "a same-size edit with an unchanged int mtime, made in a second "
            "not older than the indexing second, was skipped"
        )
        assert all("RACY_EDITED__" in t for t in embedder.embedded_texts), (
            f"only racy.py may be re-embedded, got: {embedder.embedded_texts}"
        )
        for unchanged in ("alpha.py", "beta.py", "gamma.py"):
            assert after[unchanged] == before[unchanged]

    def test_edit_during_embedding_with_latency_is_reindexed(
        self, tmp_path: Path
    ) -> None:
        """The racy window is the second the file was READ, not the (later)
        second its points were written: a same-size edit landing while the
        embedding round-trip is in flight, in the same int-mtime second,
        must be caught by the next reconcile."""
        repo = tmp_path / "single"
        repo.mkdir()
        racy = repo / "racy.py"
        original = "def racy_marker():\n    return 'RACY_ORIGINAL'\n"
        edited = "def racy_marker():\n    return 'RACY_INFLIGHT'\n"
        assert len(original) == len(edited)
        racy.write_text(original)

        indexer, embedder, store = _make_indexer(repo, tmp_path / "meta.json")
        assert not indexer.is_git_aware()

        def rewrite_in_flight() -> None:
            racy.write_text(edited)
            os.utime(racy, (racy_mtime, racy_mtime))
            # Provider latency: the points are written more than the 2 s
            # clock-skew margin after the mtime second, so a write-time
            # indexed_timestamp would trust the file without a content check.
            time.sleep(3.5)

        # Single-file repo: the first embed call is racy.py's own. The mtime
        # is the current second, set immediately before indexing reads it.
        racy_mtime = float(int(time.time()))
        os.utime(racy, (racy_mtime, racy_mtime))
        embedder.before_first_embed = rewrite_in_flight
        indexer.smart_index(force_full=True, quiet=True)
        assert embedder.embedded_texts == [original]

        _reset(embedder, store)
        indexer.smart_index(reconcile_with_database=True, quiet=True)

        assert embedder.embedded_texts == [edited], (
            "a same-size edit in the second the file was read (points written "
            f"later) was not re-indexed: {embedder.embedded_texts}"
        )

    def test_filesystem_clock_skew_margin(self, tmp_path: Path) -> None:
        """The file mtime comes from the filesystem's clock (an NFS server),
        the read time from the indexing host's clock. A filesystem clock up
        to 2 s behind must not hide a same-second edit, so the content is
        checked unless the mtime second is more than 2 s before the read
        second."""
        repo = _non_git_repo(tmp_path)
        indexer, _, _ = _make_indexer(repo, tmp_path / "meta.json")
        target = repo / "alpha.py"
        stored_hash = indexer.file_identifier._get_file_content_hash(target)
        target.write_text(_FILES["alpha.py"].replace("ALPHA", "OMEGA"))

        read_ts = 1_800_000_000.4
        indexer._reconcile_working_dir_index = {"alpha.py": (read_ts, stored_hash)}

        within_margin = float(int(read_ts) - 2) + 0.9
        os.utime(target, (within_margin, within_margin))
        assert indexer._working_dir_file_racily_modified("alpha.py") is True, (
            "an mtime 2 s before the read second must still be content-checked"
        )

        beyond_margin = float(int(read_ts) - 3) + 0.9
        os.utime(target, (beyond_margin, beyond_margin))
        assert indexer._working_dir_file_racily_modified("alpha.py") is False

    def test_unreadable_racy_file_is_reported_failed_not_reindexed(
        self, tmp_path: Path
    ) -> None:
        """A content check that cannot read the file must report the file as
        a failed analysis -- never treat the read error as "content differs",
        which would delete the file's points before a doomed re-index."""
        repo = _non_git_repo(tmp_path)
        racy = repo / "racy.py"
        racy.write_text("def racy_marker():\n    return 'RACY_ORIGINAL'\n")
        racy_mtime = float(int(time.time()) + 60)
        os.utime(racy, (racy_mtime, racy_mtime))

        indexer, embedder, store = _make_indexer(repo, tmp_path / "meta.json")
        indexer.smart_index(force_full=True, quiet=True)

        racy.chmod(0)
        try:
            _reset(embedder, store)
            stats = indexer.smart_index(reconcile_with_database=True, quiet=True)
        finally:
            racy.chmod(0o644)

        assert embedder.embedded_texts == []
        assert store.deletion_calls == [], (
            f"an unreadable file's points were deleted: {store.deletion_calls}"
        )
        assert "racy.py" in stats.failed_paths, stats


def _committed_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "gitrepo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Example Tester")
    _write_files(repo)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


class TestGitReconcileUnchanged:
    def test_dirty_tracked_file_is_reindexed(self, tmp_path: Path) -> None:
        repo = _committed_git_repo(tmp_path)
        indexer, embedder, store = _make_indexer(repo, tmp_path / "meta.json")
        assert indexer.is_git_aware()
        indexer.smart_index(force_full=True, quiet=True)

        (repo / "alpha.py").write_text(
            "def alpha_marker_function():\n    return 'ALPHA_DIRTY_2013'\n"
        )

        before = _point_ids_by_path(indexer, store)
        _reset(embedder, store)
        indexer.smart_index(reconcile_with_database=True, quiet=True)
        after = _point_ids_by_path(indexer, store)

        assert embedder.embedded_texts, "the dirty tracked file must be re-indexed"
        assert all("ALPHA_DIRTY_2013" in t for t in embedder.embedded_texts), (
            f"only the dirty file may be re-embedded, got: {embedder.embedded_texts}"
        )
        for unchanged in ("beta.py", "gamma.py"):
            assert after[unchanged] == before[unchanged]
        assert after["alpha.py"] and after["alpha.py"] != before["alpha.py"]

    def test_clean_git_repo_reconcile_embeds_nothing(self, tmp_path: Path) -> None:
        repo = _committed_git_repo(tmp_path)
        indexer, embedder, store = _make_indexer(repo, tmp_path / "meta.json")
        assert indexer.is_git_aware()
        indexer.smart_index(force_full=True, quiet=True)
        assert embedder.embedded_texts

        _reset(embedder, store)
        indexer.smart_index(reconcile_with_database=True, quiet=True)

        assert embedder.embedded_texts == []
