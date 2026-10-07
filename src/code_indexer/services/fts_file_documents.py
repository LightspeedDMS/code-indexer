"""FTS documents of a file, and FTS index content checks (Bug #2056).

ONE builder turns chunker output into FTS documents: `chunk_fts_documents`.
Normal indexing (FileChunkingManager) uses it for every processed file, and
every rebuild path -- the FTS bootstrap and content-version rebuild of
`cidx index --fts` (SmartIndexer), the reconcile FTS restore, and
`cidx index --rebuild-fts-index` (CLI and daemon) -- uses it through
`FileFtsDocuments`, which runs the SAME chunker normal indexing runs, with
no embedding call. Every path therefore writes exactly the documents normal
indexing writes: one per chunk, same path, line range, content and
identifiers. A file the chunker yields no chunk for (blank) gets no
document; one it cannot read raises, and callers skip it, as normal
indexing fails that file without writing FTS documents.

FTS paths are ALWAYS stored relative to the repository root: path filters
match the relative path, and per-file supersession deletes by it (#1761).
"""

import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, Iterator, List, Tuple

from ..indexing.fixed_size_chunker import FixedSizeChunker

if TYPE_CHECKING:
    from ..config import Config
    from .tantivy_index_manager import TantivyIndexManager

logger = logging.getLogger(__name__)

#: Content version of an FTS index, recorded in FTS_CONTENT_VERSION_FILE
#: inside the index directory. An index whose marker is missing or older is
#: rebuilt from disk ONCE on its next `cidx index --fts` (the #1763 rebuild
#: path), then marked. Bump it only when existing indexes may hold wrong
#: content. 2 = Bug #2056: indexes built before it may be missing the
#: documents of cache-reused chunks, which nothing else can detect.
FTS_CONTENT_VERSION = 2
FTS_CONTENT_VERSION_FILE = "cidx_fts_content_version"


def chunk_fts_documents(
    chunks: Iterable[Dict[str, Any]], file_path: Path, codebase_dir: Path
) -> List[Dict[str, Any]]:
    """The FTS documents of `file_path`: one per chunk of the chunker's
    output, in chunk order.

    Raises:
        ValueError: `file_path` is not inside `codebase_dir`.
    """
    relative_path = str(file_path.relative_to(codebase_dir))
    language = file_path.suffix.lstrip(".") or "txt"
    documents = []
    for chunk in chunks:
        text = chunk.get("text", "")
        documents.append(
            {
                "path": relative_path,
                "content": text,
                "content_raw": text,
                # Identifiers: simple whitespace split of the chunk text.
                "identifiers": text.split(),
                "line_start": chunk.get("line_start", 0),
                "line_end": chunk.get("line_end", 0),
                "language": language,
            }
        )
    return documents


class FtsReplaceError(Exception):
    """A file's FTS documents could not be replaced (the delete failed);
    nothing was added."""


class FtsWriteFailures:
    """Run-scoped, thread-safe record of the files whose FTS documents could
    not be replaced (worker threads record; the run's finish reads). The
    finish retries them from disk (fts_lifecycle.finish_fts_run); a file
    still failing then is missing from FTS and keeps the index unmarked."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._files: List[Tuple[Path, str]] = []

    def record(self, file_path: Path, error: str) -> None:
        with self._lock:
            self._files.append((Path(file_path), error))

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._files)

    def paths(self) -> List[Path]:
        with self._lock:
            return [file_path for file_path, _error in self._files]


def replace_file_fts_documents(
    fts_manager: "TantivyIndexManager",
    file_path: Path,
    codebase_dir: Path,
    chunks: Iterable[Dict[str, Any]],
) -> None:
    """THE one implementation of "replace a file's FTS documents", used by
    normal indexing (FileChunkingManager) and `cidx watch`: queue a delete of
    EVERY document of the file (deferred, uncommitted -- Tantivy appends on
    add_document, so supersession is delete-by-path, Bug #1761), then add
    one document per current chunk (Bug #2056: every chunk, whatever its
    vector path). No chunks (blank or deleted file) only deletes. The
    caller commits.

    Raises:
        RuntimeError: the writer is not initialized (a wiring bug, never
            swallowed -- it would silently duplicate every file).
        FtsReplaceError: the delete failed (nothing was added: stale but
            unique beats duplicated), or some documents could not be added
            (every one was attempted). Callers decide: log it, or retry.
    """
    relative_path = str(file_path.relative_to(codebase_dir))
    try:
        fts_manager.delete_document_deferred(relative_path)
    except RuntimeError:
        raise
    except Exception as e:
        raise FtsReplaceError(
            f"FTS pre-delete failed for {file_path}; its FTS documents were "
            f"not re-indexed this pass (avoids duplicate rows): {e}"
        ) from e
    documents = chunk_fts_documents(chunks, file_path, codebase_dir)
    # Messages only: a kept exception's traceback references this frame,
    # whose locals reference the exception -- a cycle that keeps the index
    # manager (and its writer lock) alive until the cyclic GC runs.
    add_errors: List[str] = []
    for document in documents:
        try:
            fts_manager.add_document(document)
        except Exception as e:
            add_errors.append(f"{type(e).__name__}: {e}")
    if add_errors:
        raise FtsReplaceError(
            f"{len(add_errors)} of {len(documents)} FTS documents of "
            f"{file_path} could not be added: {add_errors[0]}"
        )


class FileFtsDocuments:
    """Chunk-level FTS documents of whole files, built with the chunker
    normal indexing uses (`FixedSizeChunker(config)`, as DocumentProcessor
    builds it, called with `repo_root=codebase_dir` as FileChunkingManager
    calls it). Reads and chunks files only: never embeds."""

    def __init__(self, config: "Config") -> None:
        self._codebase_dir = Path(config.codebase_dir)
        self._chunker = FixedSizeChunker(config)

    def for_file(self, file_path: Path) -> List[Dict[str, Any]]:
        """`file_path`'s FTS documents; [] for a file with no chunk.

        Raises:
            ValueError: the file cannot be read or chunked, or lies outside
                the codebase (normal indexing fails such a file).
        """
        chunks = self._chunker.chunk_file(file_path, repo_root=self._codebase_dir)
        return chunk_fts_documents(chunks, file_path, self._codebase_dir)

    def replace_in_index(
        self, fts_manager: "TantivyIndexManager", file_path: Path
    ) -> None:
        """Replace `file_path`'s FTS documents with its current chunks (see
        replace_file_fts_documents); a file that no longer exists has none.

        Raises:
            ValueError: the file exists but cannot be read or chunked.
        """
        # The chunker's own return type: one dict per chunk.
        chunks: List[Dict[str, Any]] = []
        if file_path.exists():
            chunks = self._chunker.chunk_file(file_path, repo_root=self._codebase_dir)
        replace_file_fts_documents(fts_manager, file_path, self._codebase_dir, chunks)

    def files_with_documents(self, files: Iterable[Path]) -> Iterator[Path]:
        """Lazily yield the files that get at least one FTS document."""
        for file_path in files:
            try:
                if self.for_file(file_path):
                    yield file_path
            except ValueError:
                continue


def fts_content_version_is_current(index_dir: Path) -> bool:
    """True when the index at `index_dir` carries a content marker at
    FTS_CONTENT_VERSION or newer. A missing or garbled marker is not
    current: the index is rebuilt once and the marker rewritten."""
    try:
        text = (index_dir / FTS_CONTENT_VERSION_FILE).read_text().strip()
    except FileNotFoundError:
        return False
    return text.isdigit() and int(text) >= FTS_CONTENT_VERSION


def mark_fts_content_current(index_dir: Path) -> None:
    """Record FTS_CONTENT_VERSION for the index at `index_dir`. Callers
    write it only after the rebuilt index was committed, so a run that dies
    earlier leaves no marker and the next run rebuilds again. Atomic
    (temp file + os.replace): a crash never leaves a half-written marker.
    Tantivy only garbage-collects files it manages, so the marker survives
    commits and merges."""
    marker = index_dir / FTS_CONTENT_VERSION_FILE
    temp = marker.with_name(f"{marker.name}.tmp")
    temp.write_text(f"{FTS_CONTENT_VERSION}\n")
    os.replace(temp, marker)


def invalidate_fts_content_marker(index_dir: Path) -> None:
    """Remove the content marker of the index at `index_dir` (if any).
    Called BEFORE an index is cleared or recreated, so a run that dies
    mid-rebuild leaves it "not current" and the next run rebuilds it."""
    (index_dir / FTS_CONTENT_VERSION_FILE).unlink(missing_ok=True)


class FtsIndexEmptyError(RuntimeError):
    """An FTS run ended with source files on disk but no committed FTS
    document: full-text search over the repository would answer nothing."""


def ensure_fts_index_not_empty(
    document_count: int, source_files: Iterable[Path], index_dir: Path
) -> None:
    """Fail loudly when the committed FTS index is empty although the
    repository has source files that get FTS documents. A populated index
    returns at once, without walking the repository; an empty one pulls at
    most one file.

    Raises:
        FtsIndexEmptyError: `document_count` is 0 and a source file exists.
    """
    if document_count > 0:
        return
    first_file = next(iter(source_files), None)
    if first_file is None:
        return
    raise FtsIndexEmptyError(
        f"FTS index at {index_dir} holds no documents although the repository "
        f"has source files (e.g. {first_file}); full-text search would return "
        f"nothing. Run 'cidx index --fts' again (an empty FTS index is rebuilt "
        f"from disk) or 'cidx index --rebuild-fts-index'."
    )


def fts_index_needs_repopulation(index_dir: Path) -> bool:
    """True when the existing on-disk FTS index at `index_dir` must be
    rebuilt from disk once, because per-file indexing would never repair it
    (Bug #2056):

    * it holds no document at all -- what the pre-fix `cidx index --fts`
      bootstrap committed; a refresh that selects no file never refills it;
    * it holds a document stored under an absolute path -- what the pre-fix
      `--rebuild-fts-index` wrote; per-file supersession deletes by the
      relative path, so those would stay as duplicates forever.

    Cheap: the document count is segment metadata, and the absolute-path
    probe is one regex TERM query on the untokenized exact-path field,
    limit 1 (walks the term dictionary, never the documents).

    Precondition: the index has the current schema (the exact-path field
    exists). A pre-#1761 schema is rebuilt anyway (Bug #1763), so callers
    check that first; tantivy raises ValueError on a missing field.
    """
    import tantivy

    from .tantivy_index_manager import _PATH_EXACT_FIELD

    index = tantivy.Index.open(str(index_dir))
    index.reload()
    # tantivy-py's Searcher has no close()/context manager and holds no
    # writer lock; it is released when this local reference drops.
    searcher = index.searcher()
    if searcher.num_docs == 0:
        return True
    query = tantivy.Query.regex_query(index.schema, _PATH_EXACT_FIELD, "/.*")
    return len(searcher.search(query, 1).hits) > 0
