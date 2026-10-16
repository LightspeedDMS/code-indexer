"""Bug #2056: an FTS run that ends with source files on disk but zero
committed FTS documents must fail loudly (the server judges `cidx index` by
its exit code), never report success over an index that answers nothing.

Counts come from REAL Tantivy indexes.
"""

from pathlib import Path
from typing import Iterator, List

import pytest

from code_indexer.services.fts_file_documents import (
    FtsIndexEmptyError,
    ensure_fts_index_not_empty,
)
from code_indexer.services.tantivy_index_manager import TantivyIndexManager


def _committed_count(index_dir: Path, paths: List[str]) -> int:
    fts = TantivyIndexManager(index_dir)
    fts.initialize_index(create_new=True)
    try:
        for path in paths:
            fts.add_document(
                {
                    "path": path,
                    "content": "TOKEN",
                    "content_raw": "TOKEN",
                    "identifiers": ["TOKEN"],
                    "line_start": 1,
                    "line_end": 1,
                    "language": "py",
                }
            )
        fts.commit()
        return fts.get_document_count()
    finally:
        fts.close()


class _ExhaustionRecorder:
    """Iterable of source files that records how many were consumed."""

    def __init__(self, files: List[Path]) -> None:
        self.files = files
        self.consumed = 0

    def __iter__(self) -> Iterator[Path]:
        for file_path in self.files:
            self.consumed += 1
            yield file_path


class TestEnsureFtsIndexNotEmpty2056:
    def test_empty_index_with_source_files_fails_loudly(self, tmp_path: Path) -> None:
        index_dir = tmp_path / "tantivy_index"
        source = tmp_path / "auth.py"
        source.write_text("def check(): pass\n")
        count = _committed_count(index_dir, [])
        assert count == 0

        with pytest.raises(FtsIndexEmptyError) as raised:
            ensure_fts_index_not_empty(count, iter([source]), index_dir)

        message = str(raised.value)
        assert str(index_dir) in message
        assert "no documents" in message

    def test_empty_index_without_source_files_passes(self, tmp_path: Path) -> None:
        index_dir = tmp_path / "tantivy_index"
        count = _committed_count(index_dir, [])

        ensure_fts_index_not_empty(count, iter([]), index_dir)

    def test_populated_index_passes_without_walking_files(self, tmp_path: Path) -> None:
        index_dir = tmp_path / "tantivy_index"
        count = _committed_count(index_dir, ["src/auth.py"])
        files = _ExhaustionRecorder([tmp_path / "auth.py"])

        ensure_fts_index_not_empty(count, files, index_dir)

        assert count == 1
        assert files.consumed == 0, "a populated index must not walk the repo"


class TestFilesWithDocuments2056:
    """Only files that get FTS documents count as "source files" for the
    empty-index guard: a blank file gets none, in normal indexing too."""

    def test_yields_only_files_that_get_documents(self, tmp_path: Path) -> None:
        from code_indexer.config import Config
        from code_indexer.services.fts_file_documents import FileFtsDocuments

        blank = tmp_path / "blank.py"
        blank.write_text("  \n\n")
        real = tmp_path / "real.py"
        real.write_text("def real(): return 'TOKEN'\n")
        missing = tmp_path / "missing.py"
        documents = FileFtsDocuments(Config(codebase_dir=tmp_path))

        assert list(documents.files_with_documents([blank, missing, real])) == [real]
        assert documents.for_file(blank) == []

    def test_empty_index_with_only_blank_files_passes(self, tmp_path: Path) -> None:
        from code_indexer.config import Config
        from code_indexer.services.fts_file_documents import FileFtsDocuments

        blank = tmp_path / "blank.py"
        blank.write_text("\n")
        index_dir = tmp_path / "tantivy_index"
        count = _committed_count(index_dir, [])
        documents = FileFtsDocuments(Config(codebase_dir=tmp_path))

        ensure_fts_index_not_empty(
            count, documents.files_with_documents([blank]), index_dir
        )
