"""Bug #2056: `cidx watch` keeps FTS exactly like normal indexing does --
per changed file, delete every document, then add one per current chunk,
under the repo-relative path; a deleted or blanked file has none.

Real Tantivy, real FTSWatchHandler, real watchdog events; no mocks.
"""

from collections import Counter
from pathlib import Path
from typing import List, Tuple

from watchdog.events import FileCreatedEvent, FileDeletedEvent, FileModifiedEvent

from code_indexer.config import Config
from code_indexer.indexing.fixed_size_chunker import FixedSizeChunker
from code_indexer.services.fts_watch_handler import FTSWatchHandler
from code_indexer.services.tantivy_index_manager import TantivyIndexManager


def _big_file(tail_token: str) -> str:
    """Several 4096-char chunks; TWICETOKEN in the first and last chunk."""
    filler = "".join(f"value_{i:05d} = {i}\n" for i in range(700))
    return f"HEAD = 1  # TWICETOKEN\n{filler}{tail_token} = 2  # TWICETOKEN\n"


def _setup(tmp_path: Path) -> Tuple[Config, FTSWatchHandler, TantivyIndexManager]:
    config = Config(codebase_dir=tmp_path)
    fts = TantivyIndexManager(tmp_path / ".code-indexer" / "tantivy_index")
    fts.initialize_index(create_new=True)
    return config, FTSWatchHandler(tantivy_index_manager=fts, config=config), fts


def _search(tmp_path: Path, token: str) -> List[Tuple[str, int]]:
    reader = TantivyIndexManager(tmp_path / ".code-indexer" / "tantivy_index")
    reader.open_for_search()
    return sorted((hit["path"], hit["line"]) for hit in reader.search(token, limit=50))


def _paths(tmp_path: Path) -> Counter:
    import tantivy

    index = tantivy.Index.open(str(tmp_path / ".code-indexer" / "tantivy_index"))
    index.reload()
    searcher = index.searcher()
    if searcher.num_docs == 0:
        return Counter()
    hits = searcher.search(tantivy.Query.all_query(), searcher.num_docs).hits
    return Counter(searcher.doc(address).get_first("path") for _s, address in hits)


class TestFtsWatchHandlerUsesNormalIndexingDocuments2056:
    def test_watched_edit_keeps_exactly_the_current_chunks(
        self, tmp_path: Path
    ) -> None:
        config, handler, fts = _setup(tmp_path)
        big = tmp_path / "big.py"
        big.write_text(_big_file("OLDTAILTOKEN"))
        try:
            handler.on_created(FileCreatedEvent(str(big)))
            big.write_text(_big_file("NEWTAILTOKEN"))
            handler.on_modified(FileModifiedEvent(str(big)))
        finally:
            fts.close()

        chunks = FixedSizeChunker(config).chunk_file(big, repo_root=tmp_path)
        assert len(chunks) > 1
        assert _paths(tmp_path) == Counter({"big.py": len(chunks)})
        matches = _search(tmp_path, "TWICETOKEN")
        assert [path for path, _line in matches] == ["big.py", "big.py"]
        assert matches[0][1] != matches[1][1]
        assert [path for path, _l in _search(tmp_path, "NEWTAILTOKEN")] == ["big.py"]
        assert _search(tmp_path, "OLDTAILTOKEN") == []

    def test_watched_delete_removes_the_files_documents(self, tmp_path: Path) -> None:
        _config, handler, fts = _setup(tmp_path)
        big = tmp_path / "big.py"
        big.write_text(_big_file("OLDTAILTOKEN"))
        other = tmp_path / "other.py"
        other.write_text("OTHERTOKEN = 1\n")
        try:
            handler.on_created(FileCreatedEvent(str(big)))
            handler.on_created(FileCreatedEvent(str(other)))
            big.unlink()
            handler.on_deleted(FileDeletedEvent(str(big)))
        finally:
            fts.close()

        assert _paths(tmp_path) == Counter({"other.py": 1})
        assert _search(tmp_path, "TWICETOKEN") == []

    def test_watched_write_failure_drops_the_content_marker(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from code_indexer.services.fts_file_documents import (
            fts_content_version_is_current,
            mark_fts_content_current,
        )

        _config, handler, fts = _setup(tmp_path)
        index_dir = tmp_path / ".code-indexer" / "tantivy_index"
        mark_fts_content_current(index_dir)
        big = tmp_path / "big.py"
        big.write_text(_big_file("OLDTAILTOKEN"))

        def failing_add(self, doc):
            raise OSError("injected FTS write failure")

        # Fault injection at the external FTS library boundary.
        monkeypatch.setattr(TantivyIndexManager, "add_document", failing_add)
        try:
            handler.on_created(FileCreatedEvent(str(big)))
        finally:
            monkeypatch.undo()
            fts.close()

        assert not fts_content_version_is_current(index_dir)

    def test_watched_read_failure_drops_the_content_marker(
        self, tmp_path: Path
    ) -> None:
        import os

        from code_indexer.services.fts_file_documents import (
            fts_content_version_is_current,
            mark_fts_content_current,
        )

        _config, handler, fts = _setup(tmp_path)
        index_dir = tmp_path / ".code-indexer" / "tantivy_index"
        mark_fts_content_current(index_dir)
        unreadable = tmp_path / "unreadable.py"
        unreadable.write_text("def x(): return 'UNREADABLETOKEN'\n")
        os.chmod(unreadable, 0)
        try:
            handler.on_modified(FileModifiedEvent(str(unreadable)))
        finally:
            os.chmod(unreadable, 0o644)
            fts.close()

        assert not fts_content_version_is_current(index_dir)

    def test_watched_commit_failure_drops_the_content_marker(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from code_indexer.services.fts_file_documents import (
            fts_content_version_is_current,
            mark_fts_content_current,
        )

        _config, handler, fts = _setup(tmp_path)
        index_dir = tmp_path / ".code-indexer" / "tantivy_index"
        mark_fts_content_current(index_dir)
        edited = tmp_path / "edited.py"
        edited.write_text("def x(): return 'EDITEDTOKEN'\n")

        def failing_commit(self):
            raise OSError("injected FTS commit failure")

        # Fault injection at the external FTS library boundary.
        monkeypatch.setattr(TantivyIndexManager, "commit", failing_commit)
        try:
            handler.on_modified(FileModifiedEvent(str(edited)))
        finally:
            monkeypatch.undo()
            fts.close()

        assert not fts_content_version_is_current(index_dir)

    def test_watched_edit_that_blanks_a_file_removes_its_documents(
        self, tmp_path: Path
    ) -> None:
        _config, handler, fts = _setup(tmp_path)
        big = tmp_path / "big.py"
        big.write_text(_big_file("OLDTAILTOKEN"))
        try:
            handler.on_created(FileCreatedEvent(str(big)))
            big.write_text("  \n")
            handler.on_modified(FileModifiedEvent(str(big)))
        finally:
            fts.close()

        assert _paths(tmp_path) == Counter()
