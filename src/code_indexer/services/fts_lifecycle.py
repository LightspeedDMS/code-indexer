"""FTS index lifecycle of an indexing run (Bugs #1763, #2056).

* `open_fts_index_for_run` decides whether the existing index is reused or
  rebuilt from disk once (stale pre-#1761 schema, no current content marker,
  no documents, absolute-path documents), invalidates the content marker
  BEFORE any index is cleared or recreated, and opens the index.
* `bootstrap_fts_from_disk` fills a new or rebuilt index with every file's
  chunk-level documents (no embedding).
* `finish_fts_run` rebuilds from disk the files whose embedding or FTS
  write failed (FTS needs no embedding), commits, reports the committed
  document count, fails loudly on an empty index for a non-empty
  repository, and settles the content marker (`_settle_fts_content`).
* `rebuild_fts_index` is the explicit `--rebuild-fts-index` (CLI and
  daemon): under the repository indexing lock, from scratch.

Per-file failures (a file cannot be read or chunked, its FTS write fails)
follow semantic indexing's rule, in ONE place (`_settle_fts_content`): the
run fails only when no file could be indexed; otherwise it succeeds, the
missing files are reported (one WARNING, the run's output) and the index
is left without a current content marker, so the next run -- or refresh --
rebuilds it from disk. Infrastructure failures (setup, commit, an
unreadable committed index, the empty-index guard) raise `FtsIndexError`,
so `cidx index` exits non-zero and the server rejects the refresh. It
carries no chunk-store error, so the exit code is the generic one (never
the chunk-store corruption/environment codes that drive restore logic).
"""

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
)

from .fts_file_documents import (
    FileFtsDocuments,
    ensure_fts_index_not_empty,
    fts_content_version_is_current,
    fts_index_needs_repopulation,
    invalidate_fts_content_marker,
    mark_fts_content_current,
)

if TYPE_CHECKING:
    from ..config import Config
    from .tantivy_index_manager import TantivyIndexManager

logger = logging.getLogger(__name__)

#: (file, error) of a file a rebuild could not index.
FailedFile = Tuple[Path, str]


class FtsIndexError(RuntimeError):
    """FTS was requested but the index could not be set up, committed or
    fully rebuilt."""


class FtsRebuildIncompleteError(FtsIndexError):
    """A rebuild could not index every file; the index is not marked
    content-current, so the next run rebuilds it again."""


def fts_index_dir_for_repo(repo_root: Path) -> Path:
    """THE location of a repository's FTS index (CLI, daemon and server)."""
    return Path(repo_root) / ".code-indexer" / "tantivy_index"


def fts_index_dir(config: "Config") -> Path:
    return fts_index_dir_for_repo(Path(config.codebase_dir))


def fts_content_rebuild_due(repo_root: Path) -> bool:
    """True when the repository at `repo_root` has an FTS index whose
    content marker is not current: its next `cidx index --fts` rebuilds it
    once from disk, so a refresh must not skip it for lack of upstream
    changes. One stat and one small read; the index is not opened."""
    index_dir = fts_index_dir_for_repo(repo_root)
    return (index_dir / "meta.json").exists() and not fts_content_version_is_current(
        index_dir
    )


def committed_document_count(index_dir: Path) -> int:
    """The number of committed documents of the index at `index_dir`, read
    from disk. Unlike TantivyIndexManager.get_document_count() (0 on any
    error), a reader error raises: it must never pass for "no documents".

    Raises:
        FtsIndexError: the committed index cannot be read.
    """
    import tantivy

    try:
        index = tantivy.Index.open(str(index_dir))
        index.reload()
        # num_docs is a property in tantivy-py 0.25; the Searcher has no
        # close()/context manager and is released with this reference.
        return int(index.searcher().num_docs)
    except Exception as e:
        raise FtsIndexError(f"FTS index {index_dir} cannot be read: {e}") from e


def _rebuild_decision(
    fts_manager: "TantivyIndexManager", index_dir: Path
) -> Tuple[bool, bool]:
    """THE rebuild decision: (index exists, existing index needs a one-time
    rebuild from disk -- stale pre-#1761 schema, no current #2056 content
    marker, or absolute-path documents; a current marker on an empty index
    means a complete, empty repository). One rebuild covers
    every case; the marker check short-circuits, so a pre-#2056 index is
    not even opened before its rebuild."""
    exists = (index_dir / "meta.json").exists()
    needs_rebuild = exists and (
        fts_manager.schema_needs_rebuild()
        or not fts_content_version_is_current(index_dir)
        or fts_index_needs_repopulation(index_dir)
    )
    return exists, needs_rebuild


def open_fts_index_for_run(
    config: "Config", *, force_full: bool
) -> Tuple["TantivyIndexManager", bool]:
    """Open the run's FTS index; return (manager, rebuilt). `rebuilt` is True
    when the index is created from scratch (new, forced, or a one-time
    rebuild): the caller then bootstraps it from disk unless the run
    re-indexes every file itself.

    Raises:
        FtsIndexError: the index could not be set up.
    """
    from .tantivy_index_manager import TantivyIndexManager

    index_dir = fts_index_dir(config)
    try:
        fts_manager = TantivyIndexManager(index_dir)
        exists, needs_rebuild = _rebuild_decision(fts_manager, index_dir)
        rebuilt = force_full or not exists or needs_rebuild
        # Before anything is written, reused index or not: a run dying from
        # here on leaves the index "not current", so the next run rebuilds
        # it (a resumed run replays its work list, never its deletions).
        # Only a completed run marks it again (_settle_fts_content).
        invalidate_fts_content_marker(index_dir)
        if needs_rebuild:
            logger.info(
                f"FTS index at {index_dir} has a stale schema, no current "
                f"content marker, no documents or absolute-path documents -- "
                f"clearing for a one-time rebuild from disk"
            )
        if rebuilt and index_dir.exists():
            # initialize_index(create_new=True) keeps an existing index's
            # documents (and a stale schema cannot be reopened at all), so
            # every rebuild -- forced ones included -- starts from empty.
            shutil.rmtree(index_dir)
        fts_manager.initialize_index(create_new=rebuilt)
    except Exception as e:
        raise FtsIndexError(f"FTS index setup failed at {index_dir}: {e}") from e
    return fts_manager, rebuilt


def _add_files(
    fts_manager: "TantivyIndexManager",
    documents: FileFtsDocuments,
    files: Iterable[Path],
    on_file: Callable[[Path, Optional[str]], None],
) -> Tuple[int, List[FailedFile]]:
    indexed = 0
    failed: List[FailedFile] = []
    for file_path in files:
        try:
            for document in documents.for_file(file_path):
                fts_manager.add_document(document)
        except Exception as e:
            logger.error(f"FTS rebuild could not index {file_path}: {e}")
            failed.append((file_path, str(e)))
            on_file(file_path, str(e))
            continue
        indexed += 1
        on_file(file_path, None)
    return indexed, failed


def bootstrap_fts_from_disk(
    fts_manager: "TantivyIndexManager",
    config: "Config",
    files: Iterable[Path],
    progress_callback: Optional[Callable[..., Any]] = None,
) -> List[FailedFile]:
    """Fill a new or rebuilt index with every file's chunk-level documents
    (reads and chunks only, no embedding). Returns the files it could not
    index, for `finish_fts_run`'s per-file rule."""
    if progress_callback:
        progress_callback(
            0,
            0,
            Path(""),
            info="FTS index is new - scanning all files to build full-text index...",
        )
    indexed, failed = _add_files(
        fts_manager, FileFtsDocuments(config), files, lambda f, e: None
    )
    if progress_callback:
        # Nothing is committed yet; the committed document count is
        # reported after the run's final FTS commit.
        progress_callback(
            0,
            0,
            Path(""),
            info=f"FTS bootstrap: queued {indexed} files for full-text indexing",
        )
    return failed


def _replace_from_disk(
    fts_manager: "TantivyIndexManager", config: "Config", files: Iterable[Path]
) -> List[FailedFile]:
    """Replace each file's FTS documents with its current chunks read from
    disk (no embedding). Returns the files that still could not be indexed;
    an index-level error (RuntimeError) propagates."""
    from .fts_file_documents import FtsReplaceError

    documents = FileFtsDocuments(config)
    failed: List[FailedFile] = []
    for file_path in files:
        try:
            documents.replace_in_index(fts_manager, file_path)
        except (ValueError, OSError, FtsReplaceError) as e:
            logger.error(f"FTS could not index {file_path}: {e}")
            failed.append((file_path, str(e)))
    return failed


def _settle_fts_content(
    index_dir: Path,
    *,
    committed: int,
    missing_files: int,
    first_error: str,
) -> Optional[str]:
    """THE per-file failure rule of every FTS write path (index, clear,
    rebuild, daemon, watch start), applied after a successful commit and
    empty guard. Same rule as semantic indexing: the run fails only when no
    file could be indexed. Otherwise files missing from FTS leave the index
    without a current content marker (the next run rebuilds it from disk)
    and are reported in the returned message, logged once as a WARNING. Any
    completed run with no file missing -- rebuild, incremental or no-op --
    marks the index content-current again (open_fts_index_for_run dropped
    the marker; a run that raised or was killed never gets here).

    Raises:
        FtsRebuildIncompleteError: files are missing and nothing is indexed.
    """
    if not missing_files:
        mark_fts_content_current(index_dir)
        return None
    invalidate_fts_content_marker(index_dir)
    message = (
        f"{missing_files} file(s) missing from the FTS index {index_dir} "
        f"(first: {first_error}); it is not marked content-current, so the "
        f"next 'cidx index --fts' rebuilds it from disk"
    )
    if committed == 0:
        raise FtsRebuildIncompleteError(f"FTS indexed no file: {message}")
    logger.warning(message)
    return message


def finish_fts_run(
    fts_manager: "TantivyIndexManager",
    config: "Config",
    *,
    run_raised: bool,
    source_files: Iterable[Path],
    failed_files: Sequence[FailedFile] = (),
    retry_files: Iterable[Path] = (),
    unknown_failures: int = 0,
    progress_callback: Optional[Callable[..., Any]] = None,
) -> Optional[str]:
    """Complete, commit and settle the run's FTS index.

    `failed_files`: files already found unindexable (a bootstrap's).
    `retry_files`: files whose documents the run may lack -- their
    embedding or FTS write failed; FTS needs no embedding, so they are
    rebuilt from disk here before the commit. `unknown_failures`: failed
    files the run could not name, so cannot be rebuilt. Files still missing
    then follow `_settle_fts_content`. Returns its message (None when FTS
    is complete), also sent to `progress_callback`.

    A run already propagating its own exception, or one that was cancelled
    (`run_raised`), only commits what it wrote; it never raises here (its
    own outcome must surface) and never marks the index -- its content is
    incomplete, so the next run rebuilds it. `source_files` is walked lazily, only when the
    committed index is empty.

    Raises:
        FtsIndexError: the commit failed or the index cannot be read.
        FtsIndexEmptyError: no document, although files would get some.
        FtsRebuildIncompleteError: files are missing and nothing is indexed.
    """
    index_dir = fts_index_dir(config)
    # Keyed by path: a file failing in the bootstrap AND the retry is one
    # missing file (its first error is kept); one the retry indexed is not
    # missing at all.
    missing_by_path = {Path(path): error for path, error in failed_files}
    if not run_raised:
        retried = [Path(path) for path in retry_files]
        retry_failures = dict(_replace_from_disk(fts_manager, config, retried))
        for path in retried:
            if path in retry_failures:
                missing_by_path.setdefault(path, retry_failures[path])
            else:
                missing_by_path.pop(path, None)
    missing = list(missing_by_path.items())
    try:
        fts_manager.commit()
    except Exception as e:
        if run_raised:
            logger.error(f"Failed to commit FTS index {index_dir}: {e}")
            return None
        raise FtsIndexError(f"FTS commit failed for {index_dir}: {e}") from e
    if run_raised:
        return None
    committed = committed_document_count(index_dir)
    message = f"FTS index holds {committed} committed documents"
    logger.info(message)
    if progress_callback:
        progress_callback(0, 0, Path(""), info=message)
    ensure_fts_index_not_empty(
        committed,
        FileFtsDocuments(config).files_with_documents(source_files),
        index_dir,
    )
    warning = _settle_fts_content(
        index_dir,
        committed=committed,
        missing_files=len(missing) + unknown_failures,
        first_error=(
            f"{missing[0][0]}: {missing[0][1]}" if missing else "unnamed file"
        ),
    )
    if warning and progress_callback:
        progress_callback(0, 0, Path(""), info=f"⚠️ {warning}")
    return warning


@dataclass
class FtsRebuildResult:
    indexed_files: int
    failed_files: List[FailedFile]
    #: _settle_fts_content's report of the files missing from FTS, if any.
    missing_message: Optional[str] = None


def open_fts_index_for_watch(
    config: "Config",
) -> Tuple["TantivyIndexManager", Optional[FtsRebuildResult]]:
    """`cidx watch`: open its FTS index through the SAME decision as
    `cidx index --fts` (open_fts_index_for_run: stale schema, no current
    content marker, no documents, absolute paths). When the index is
    (re)built, fill it with the same bootstrap and settle it like any run
    (finish_fts_run). Returns (manager with an open writer, the rebuild's
    result, or None when the existing index was reused).

    A rebuild runs only under the repository indexing lock (the one every
    rebuild and `cidx index` hold); a current index is reopened without it.

    Raises:
        FtsIndexError: setup or commit failed, or a rebuild is due while
            another indexing run holds the lock (nothing was touched).
        FtsIndexEmptyError: no document, although files would get some.
        FtsRebuildIncompleteError: files are missing and nothing is indexed.
    """
    from ..indexing.file_finder import FileFinder
    from .indexing_lock import IndexingLockError, create_indexing_lock
    from .tantivy_index_manager import TantivyIndexManager

    index_dir = fts_index_dir(config)
    fts_manager = TantivyIndexManager(index_dir)
    exists, needs_rebuild = _rebuild_decision(fts_manager, index_dir)
    if exists and not needs_rebuild:
        try:
            fts_manager.initialize_index(create_new=False)
        except Exception as e:
            raise FtsIndexError(f"FTS index {index_dir} cannot be opened: {e}") from e
        return fts_manager, None

    lock = create_indexing_lock(index_dir.parent)
    try:
        lock.acquire(str(config.codebase_dir))
    except IndexingLockError as e:
        raise FtsIndexError(
            f"FTS index at {index_dir} needs a rebuild, but another indexing "
            f"run holds the repository lock ({e}); not rebuilding -- start "
            f"`cidx watch` again once it has finished"
        ) from e
    try:
        fts_manager, rebuilt = open_fts_index_for_run(config, force_full=False)
        if not rebuilt:
            return fts_manager, None
        files = list(FileFinder(config).find_files())
        failed = bootstrap_fts_from_disk(fts_manager, config, files)
        warning = finish_fts_run(
            fts_manager,
            config,
            run_raised=False,
            failed_files=failed,
            source_files=files,
        )
        return fts_manager, FtsRebuildResult(
            indexed_files=len(files) - len(failed),
            failed_files=failed,
            missing_message=warning,
        )
    finally:
        lock.release()


def rebuild_fts_index(
    config: "Config",
    files: Iterable[Path],
    on_file: Callable[[Path, Optional[str]], None],
) -> FtsRebuildResult:
    """`--rebuild-fts-index`: rebuild the FTS index from scratch, with
    exactly normal indexing's chunk-level documents and no embedding, under
    the repository indexing lock normal indexing holds. `on_file(file,
    error)` is called once per file (error None on success). Files that
    could not be indexed follow `_settle_fts_content` (reported in the
    result's `missing_message`, index left unmarked).

    Raises:
        IndexingLockError: another indexing run holds the repository lock;
            nothing was touched.
        FtsIndexError: the committed index cannot be read.
        FtsIndexEmptyError: no document, although files would get some.
        FtsRebuildIncompleteError: files are missing and nothing is indexed.
    """
    from .indexing_lock import create_indexing_lock
    from .tantivy_index_manager import TantivyIndexManager

    index_dir = fts_index_dir(config)
    lock = create_indexing_lock(index_dir.parent)
    lock.acquire(str(config.codebase_dir))
    try:
        invalidate_fts_content_marker(index_dir)
        if index_dir.exists():
            shutil.rmtree(index_dir)
        fts_manager = TantivyIndexManager(index_dir)
        fts_manager.initialize_index(create_new=True)
        try:
            indexed, failed = _add_files(
                fts_manager, FileFtsDocuments(config), files, on_file
            )
            fts_manager.commit()
        finally:
            fts_manager.close()
        # Same empty guard and per-file rule as `cidx index --fts`.
        committed = committed_document_count(index_dir)
        ensure_fts_index_not_empty(
            committed,
            FileFtsDocuments(config).files_with_documents(files),
            index_dir,
        )
        warning = _settle_fts_content(
            index_dir,
            committed=committed,
            missing_files=len(failed),
            first_error=f"{failed[0][0]}: {failed[0][1]}" if failed else "",
        )
        return FtsRebuildResult(
            indexed_files=indexed, failed_files=failed, missing_message=warning
        )
    finally:
        lock.release()
