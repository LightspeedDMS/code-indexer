"""Bug #2056: FTS index lifecycle -- rebuild decision, content marker,
commit/guard, and the explicit rebuild shared by the CLI and the daemon.

Real Tantivy indexes, real files, real indexing lock; no mocks.
"""

import os
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from code_indexer.config import Config
from code_indexer.services.fts_file_documents import (
    FTS_CONTENT_VERSION_FILE,
    fts_content_version_is_current,
    mark_fts_content_current,
)
from code_indexer.services.fts_lifecycle import (
    FtsIndexError,
    FtsRebuildIncompleteError,
    committed_document_count,
    finish_fts_run,
    fts_index_dir,
    open_fts_index_for_run,
    open_fts_index_for_watch,
    rebuild_fts_index,
)
from code_indexer.services.index_failure_exit_codes import (
    GENERIC_INDEX_FAILURE_EXIT_CODE,
    index_failure_exit_code,
)
from code_indexer.services.indexing_lock import (
    IndexingLockError,
    create_indexing_lock,
)
from code_indexer.services.tantivy_index_manager import TantivyIndexManager


def _doc(path: str, text: str = "TOKEN") -> dict:
    return {
        "path": path,
        "content": text,
        "content_raw": text,
        "identifiers": text.split(),
        "line_start": 1,
        "line_end": 1,
        "language": "py",
    }


def _current_index(config: Config, paths: List[str]) -> Path:
    """A committed, marked (content-current) index holding `paths`."""
    index_dir = fts_index_dir(config)
    fts = TantivyIndexManager(index_dir)
    fts.initialize_index(create_new=True)
    try:
        for path in paths:
            fts.add_document(_doc(path))
        fts.commit()
    finally:
        fts.close()
    mark_fts_content_current(index_dir)
    return index_dir


def _count(index_dir: Path) -> int:
    fts = TantivyIndexManager(index_dir)
    fts.initialize_index(create_new=False)
    try:
        return fts.get_document_count()
    finally:
        fts.close()


class TestOpenFtsIndexForRun2056:
    def test_current_index_is_reopened_not_rebuilt(self, tmp_path: Path) -> None:
        config = Config(codebase_dir=tmp_path)
        index_dir = _current_index(config, ["a.py"])

        fts, rebuilt = open_fts_index_for_run(config, force_full=False)
        fts.close()

        assert rebuilt is False
        assert fts_content_version_is_current(index_dir)
        assert _count(index_dir) == 1

    def test_forced_rebuild_invalidates_marker_before_replacing_index(
        self, tmp_path: Path
    ) -> None:
        config = Config(codebase_dir=tmp_path)
        index_dir = _current_index(config, ["a.py", "b.py"])

        fts, rebuilt = open_fts_index_for_run(config, force_full=True)
        assert rebuilt is True
        assert not fts_content_version_is_current(index_dir)
        # Partial output committed, then the run dies before finishing.
        fts.add_document(_doc("a.py"))
        fts.commit()
        fts.close()

        assert _count(index_dir) == 1
        assert not fts_content_version_is_current(index_dir)

    def test_setup_failure_raises_fts_index_error(self, tmp_path: Path) -> None:
        config = Config(codebase_dir=tmp_path)
        index_dir = fts_index_dir(config)
        index_dir.parent.mkdir(parents=True)
        index_dir.write_text("not a directory")

        with pytest.raises(FtsIndexError):
            open_fts_index_for_run(config, force_full=False)


class TestFinishFtsRun2056:
    def _open(self, config: Config, paths: List[str]) -> TantivyIndexManager:
        fts = TantivyIndexManager(fts_index_dir(config))
        fts.initialize_index(create_new=True)
        for path in paths:
            fts.add_document(_doc(path))
        return fts

    def test_successful_rebuild_is_marked(self, tmp_path: Path) -> None:
        config = Config(codebase_dir=tmp_path)
        fts = self._open(config, ["a.py"])

        warning = finish_fts_run(
            fts, config, rebuilt=True, run_raised=False, source_files=[]
        )
        fts.close()

        assert warning is None
        assert fts_content_version_is_current(fts_index_dir(config))

    def test_rebuild_with_failed_files_warns_once_and_stays_unmarked(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        config = Config(codebase_dir=tmp_path)
        fts = self._open(config, ["a.py"])

        with caplog.at_level("WARNING", logger="code_indexer.services.fts_lifecycle"):
            warning = finish_fts_run(
                fts,
                config,
                rebuilt=True,
                run_raised=False,
                failed_files=[(tmp_path / "b.py", "boom")],
                source_files=[],
            )
        fts.close()

        assert warning is not None and "1 file(s) missing from the FTS index" in warning
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert [r.getMessage() for r in warnings] == [warning]
        assert _count(fts_index_dir(config)) == 1, "what was read is committed"
        assert not fts_content_version_is_current(fts_index_dir(config))

    def test_failed_files_with_nothing_indexed_raise(self, tmp_path: Path) -> None:
        config = Config(codebase_dir=tmp_path)
        fts = self._open(config, [])

        with pytest.raises(FtsRebuildIncompleteError, match="indexed no file"):
            finish_fts_run(
                fts,
                config,
                rebuilt=True,
                run_raised=False,
                failed_files=[(tmp_path / "b.py", "boom")],
                source_files=[],
            )
        fts.close()

        assert not fts_content_version_is_current(fts_index_dir(config))

    def test_unnamed_failures_unmark_a_current_index(self, tmp_path: Path) -> None:
        config = Config(codebase_dir=tmp_path)
        index_dir = _current_index(config, ["a.py"])
        fts = TantivyIndexManager(index_dir)
        fts.initialize_index(create_new=False)

        warning = finish_fts_run(
            fts,
            config,
            rebuilt=False,
            run_raised=False,
            unknown_failures=1,
            source_files=[],
        )
        fts.close()

        assert warning is not None and "1 file(s) missing" in warning
        assert _count(index_dir) == 1
        assert not fts_content_version_is_current(index_dir)

    def test_retry_files_are_rebuilt_from_disk(self, tmp_path: Path) -> None:
        """A file whose embedding or FTS write failed gets its documents from
        disk at the finish; the index stays complete and current."""
        config = Config(codebase_dir=tmp_path)
        index_dir = _current_index(config, ["a.py"])
        retried = tmp_path / "retried.py"
        retried.write_text("def retried(): return 'RETRYTOKEN'\n")
        fts = TantivyIndexManager(index_dir)
        fts.initialize_index(create_new=False)

        warning = finish_fts_run(
            fts,
            config,
            rebuilt=False,
            run_raised=False,
            retry_files=[retried],
            source_files=[],
        )
        hits = fts.search("RETRYTOKEN", limit=5)
        fts.close()

        assert warning is None
        assert [hit["path"] for hit in hits] == ["retried.py"]
        assert fts_content_version_is_current(index_dir)

    def test_unreadable_retry_file_is_missing(self, tmp_path: Path) -> None:
        config = Config(codebase_dir=tmp_path)
        index_dir = _current_index(config, ["a.py"])
        retried = tmp_path / "retried.py"
        retried.write_text("def retried(): return 'RETRYTOKEN'\n")
        fts = TantivyIndexManager(index_dir)
        fts.initialize_index(create_new=False)
        os.chmod(retried, 0)
        try:
            warning = finish_fts_run(
                fts,
                config,
                rebuilt=False,
                run_raised=False,
                retry_files=[retried],
                source_files=[],
            )
        finally:
            os.chmod(retried, 0o644)
            fts.close()

        assert warning is not None and "retried.py" in warning
        assert not fts_content_version_is_current(index_dir)

    def test_file_failing_in_bootstrap_and_retry_counts_once(
        self, tmp_path: Path
    ) -> None:
        config = Config(codebase_dir=tmp_path)
        fts = self._open(config, ["a.py"])
        locked = tmp_path / "locked.py"
        locked.write_text("def locked(): return 'LOCKEDTOKEN'\n")
        os.chmod(locked, 0)
        try:
            warning = finish_fts_run(
                fts,
                config,
                rebuilt=True,
                run_raised=False,
                failed_files=[(locked, "bootstrap: permission denied")],
                retry_files=[locked],
                source_files=[],
            )
        finally:
            os.chmod(locked, 0o644)
            fts.close()

        assert warning is not None
        assert warning.startswith("1 file(s) missing from the FTS index"), warning

    def test_commit_failure_raises_unless_the_run_already_raised(
        self, tmp_path: Path
    ) -> None:
        config = Config(codebase_dir=tmp_path)
        fts = self._open(config, ["a.py"])
        fts.close()  # the writer is gone: commit() fails for real

        with pytest.raises(FtsIndexError):
            finish_fts_run(
                fts, config, rebuilt=False, run_raised=False, source_files=[]
            )
        # A run already propagating its own exception is never masked.
        finish_fts_run(fts, config, rebuilt=True, run_raised=True, source_files=[])
        assert not fts_content_version_is_current(fts_index_dir(config))

    def test_fts_errors_exit_with_the_generic_code(self) -> None:
        try:
            try:
                raise OSError(28, "No space left on device")
            except OSError as cause:
                raise FtsIndexError("FTS commit failed") from cause
        except FtsIndexError as error:
            assert index_failure_exit_code(error) == GENERIC_INDEX_FAILURE_EXIT_CODE
        assert (
            index_failure_exit_code(FtsRebuildIncompleteError("1 file failed"))
            == GENERIC_INDEX_FAILURE_EXIT_CODE
        )


class TestOpenFtsIndexForWatchLock2056:
    """`cidx watch` rebuilds its FTS index only under the repo indexing
    lock, like every other rebuild; with nothing to rebuild it needs none."""

    def test_rebuild_due_while_lock_held_refuses_and_touches_nothing(
        self, tmp_path: Path
    ) -> None:
        config = Config(codebase_dir=tmp_path)
        (tmp_path / "a.py").write_text("A = 1\n")
        index_dir = _current_index(config, ["kept.py"])
        (index_dir / FTS_CONTENT_VERSION_FILE).unlink()  # rebuild due
        lock = create_indexing_lock(tmp_path / ".code-indexer")
        lock.acquire(str(tmp_path))
        try:
            with pytest.raises(FtsIndexError, match="lock"):
                open_fts_index_for_watch(config)
        finally:
            lock.release()

        assert _count(index_dir) == 1, "a refused rebuild touches nothing"
        assert not fts_content_version_is_current(index_dir)

    def test_current_index_opens_while_lock_held(self, tmp_path: Path) -> None:
        config = Config(codebase_dir=tmp_path)
        index_dir = _current_index(config, ["kept.py"])
        lock = create_indexing_lock(tmp_path / ".code-indexer")
        lock.acquire(str(tmp_path))
        try:
            fts, rebuilt_files = open_fts_index_for_watch(config)
            fts.close()
        finally:
            lock.release()

        assert rebuilt_files is None
        assert _count(index_dir) == 1


class TestRebuildFtsIndex2056:
    def _repo(self, tmp_path: Path) -> Tuple[Config, Path, Path]:
        (tmp_path / ".code-indexer").mkdir()
        ok = tmp_path / "ok.py"
        ok.write_text("def ok(): return 'OKTOKEN'\n")
        locked = tmp_path / "locked.py"
        locked.write_text("def locked(): return 'LOCKEDTOKEN'\n")
        return Config(codebase_dir=tmp_path), ok, locked

    def test_clean_rebuild_is_marked_current(self, tmp_path: Path) -> None:
        config, ok, locked = self._repo(tmp_path)
        seen: List[Tuple[Path, Optional[str]]] = []

        result = rebuild_fts_index(
            config, [ok, locked], lambda f, e: seen.append((f, e))
        )

        assert result.indexed_files == 2 and result.failed_files == []
        assert [f for f, _e in seen] == [ok, locked]
        assert fts_content_version_is_current(fts_index_dir(config))

    def test_unreadable_file_leaves_index_unmarked(self, tmp_path: Path) -> None:
        config, ok, locked = self._repo(tmp_path)
        _current_index(config, ["stale.py"])
        os.chmod(locked, 0)
        try:
            result = rebuild_fts_index(config, [ok, locked], lambda f, e: None)
        finally:
            os.chmod(locked, 0o644)

        assert result.indexed_files == 1
        assert [f for f, _error in result.failed_files] == [locked]
        assert result.missing_message is not None
        assert "1 file(s) missing from the FTS index" in result.missing_message
        assert not fts_content_version_is_current(fts_index_dir(config))
        assert not (fts_index_dir(config) / f"{FTS_CONTENT_VERSION_FILE}.tmp").exists()

    def test_rebuild_with_every_file_failing_raises_and_is_not_marked(
        self, tmp_path: Path
    ) -> None:
        config, ok, locked = self._repo(tmp_path)
        os.chmod(locked, 0)
        try:
            with pytest.raises(FtsRebuildIncompleteError, match="indexed no file"):
                rebuild_fts_index(config, [locked], lambda f, e: None)
        finally:
            os.chmod(locked, 0o644)

        assert committed_document_count(fts_index_dir(config)) == 0
        assert not fts_content_version_is_current(fts_index_dir(config))

    def test_strict_count_raises_on_unreadable_index(self, tmp_path: Path) -> None:
        index_dir = tmp_path / "tantivy_index"
        index_dir.mkdir()
        (index_dir / "meta.json").write_text("{ not json")

        with pytest.raises(FtsIndexError):
            committed_document_count(index_dir)

    def test_refuses_while_the_repo_indexing_lock_is_held(self, tmp_path: Path) -> None:
        config, ok, locked = self._repo(tmp_path)
        index_dir = _current_index(config, ["kept.py"])
        lock = create_indexing_lock(tmp_path / ".code-indexer")
        lock.acquire(str(tmp_path))
        try:
            with pytest.raises(IndexingLockError):
                rebuild_fts_index(config, [ok, locked], lambda f, e: None)
        finally:
            lock.release()

        assert _count(index_dir) == 1, "a refused rebuild touches nothing"
        assert fts_content_version_is_current(index_dir)
