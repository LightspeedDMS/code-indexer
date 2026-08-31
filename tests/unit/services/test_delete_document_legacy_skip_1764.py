"""Bug #1764: TantivyIndexManager.delete_document() over-deletes sibling
FTS entries on legacy indexes -- 3 live production callers.

`delete_document()` (distinct from the #1761-added
`delete_document_deferred()`) still falls back to
`parse_query(file_path, ["path"])` -- a TOKENIZED match, not exact --
when the on-disk index lacks `_PATH_EXACT_FIELD`. This over-matches:
deleting "src/foo/bar.py" also destroys an unrelated
"src/foo/bar.py.bak" document. Three live production callers reach this
path: services/fts_watch_handler.py, server/services/
golden_repo_manager.py's change_branch FTS cleanup (which deliberately
NEVER rebuilds -- create_new=False always, specifically so as not to wipe
legitimate data for files that ARE staying on the target branch), and
services/high_throughput_processor.py.

Fix: `delete_document()` now gates on `_path_exact_field_available()` and
SKIPS (safe no-op, matching `delete_document_deferred()`'s existing
safety behavior) rather than falling through to the over-matching
tokenized query, whenever the physical index is still legacy-schema. A
stale/duplicate entry is an acceptable trade-off; destroying an unrelated
document's search results is not.
"""

from pathlib import Path
from typing import Any, Dict, List

import pytest

from code_indexer.services.tantivy_index_manager import TantivyIndexManager

pytestmark = pytest.mark.slow

# Tantivy IndexWriter heap size used by the raw-tantivy legacy-index test
# helper below -- mirrors the value already used by the established
# _build_legacy_index pattern in test_tantivy_path_exact_delete_1761.py.
_TEST_WRITER_HEAP_BYTES = 50_000_000

# A target file + an unrelated sibling whose path is a token-superset of
# the target's -- the classic Bug #1761/#1764 over-deletion shape.
_TARGET_AND_SUPERSET_DOCS = [
    {
        "path": "src/foo/bar.py",
        "content": "content A",
        "content_raw": "content A",
        "identifiers": ["content", "A"],
        "line_start": 1,
        "line_end": 1,
        "language": "python",
    },
    {
        "path": "src/foo/bar.py.bak",
        "content": "content B superset tokens",
        "content_raw": "content B superset tokens",
        "identifiers": ["content", "B", "superset", "tokens"],
        "line_start": 1,
        "line_end": 1,
        "language": "python",
    },
]


def _build_legacy_index(index_dir: Path, documents: List[Dict[str, Any]]) -> None:
    """Build a real on-disk Tantivy index using the PRE-#1761 schema shape
    (no path_exact_raw / content_raw_verbatim fields), pre-populated with
    `documents`. Same technique as
    test_tantivy_path_exact_delete_1761.py's _build_legacy_index.
    """
    import tantivy
    from tantivy import Facet

    index_dir.mkdir(parents=True, exist_ok=True)

    schema_builder = tantivy.SchemaBuilder()
    schema_builder.add_text_field("path", stored=True)
    schema_builder.add_text_field("content", stored=False)
    schema_builder.add_text_field("content_raw", stored=True)
    schema_builder.add_text_field("identifiers", stored=True)
    schema_builder.add_unsigned_field("line_start", indexed=True, stored=True)
    schema_builder.add_unsigned_field("line_end", indexed=True, stored=True)
    schema_builder.add_text_field("language", stored=True)
    schema_builder.add_facet_field("language_facet")
    schema = schema_builder.build()

    index = tantivy.Index(schema, str(index_dir))
    writer = index.writer(_TEST_WRITER_HEAP_BYTES)

    for doc_dict in documents:
        doc = tantivy.Document()
        doc.add_text("path", doc_dict["path"])
        doc.add_text("content", doc_dict["content"])
        doc.add_text("content_raw", doc_dict["content_raw"])
        doc.add_text("identifiers", " ".join(doc_dict["identifiers"]))
        doc.add_unsigned("line_start", doc_dict["line_start"])
        doc.add_unsigned("line_end", doc_dict["line_end"])
        doc.add_text("language", doc_dict["language"])
        doc.add_facet("language_facet", Facet.from_string(f"/{doc_dict['language']}"))
        writer.add_document(doc)
    writer.commit()
    writer.wait_merging_threads()


class TestBug1764DeleteDocumentOverDeletionClosed:
    """TDD requirement (d): delete_document()'s over-deletion is no longer
    reachable for a previously-legacy index, whether closed by (1)
    #1763's schema-rebuild having actually run, or (2) delete_document()
    itself now skipping (rather than over-matching) while an index
    remains legacy-schema -- closure path (2) is what covers callers like
    golden_repo_manager.py's change_branch FTS cleanup, which
    deliberately never rebuilds.
    """

    def test_delete_document_on_still_legacy_index_skips_instead_of_over_deleting(
        self, tmp_path: Path
    ) -> None:
        """Closure path (2): an index that is NEVER rebuilt (mirrors
        golden_repo_manager.py's change_branch, which always opens with
        create_new=False) must not have its sibling document destroyed
        when delete_document() is called directly.
        """
        index_dir = tmp_path / "legacy_never_rebuilt"
        _build_legacy_index(index_dir, _TARGET_AND_SUPERSET_DOCS)

        manager = TantivyIndexManager(index_dir)
        manager.initialize_index(create_new=False)
        assert manager.get_document_count() == 2

        manager.delete_document("src/foo/bar.py")

        assert manager.get_document_count() == 2, (
            "delete_document() must be a safe no-op on a legacy "
            "(pre-#1761-schema) index -- neither deleting the target nor, "
            "worse, the unrelated sibling via the tokenized fallback query"
        )
        results = manager.search("superset", limit=5)
        assert len(results) == 1
        assert results[0]["path"] == "src/foo/bar.py.bak"

    def test_delete_document_after_schema_rebuild_no_longer_over_deletes(
        self, tmp_path: Path
    ) -> None:
        """Closure path (1): once a genuine schema rebuild has happened
        (mirrors #1763's self-healing trigger), delete_document() uses
        the exact-match path and correctly deletes ONLY the target,
        never the sibling.
        """
        import shutil

        index_dir = tmp_path / "legacy_then_rebuilt"
        _build_legacy_index(index_dir, _TARGET_AND_SUPERSET_DOCS)

        manager = TantivyIndexManager(index_dir)
        assert manager.schema_needs_rebuild() is True
        # Tantivy refuses to build the new schema in place over an
        # on-disk index whose physical schema differs -- clear it first,
        # same as smart_indexer.py's real self-heal flow (Bug #1763).
        shutil.rmtree(index_dir)
        manager.initialize_index(create_new=True)
        assert manager.get_document_count() == 0  # rebuild starts empty

        # Re-populate through the NEW (current) schema, as a real
        # full-reindex pass would after the rebuild.
        for doc in _TARGET_AND_SUPERSET_DOCS:
            manager.add_document(doc)
        manager.commit()
        assert manager.get_document_count() == 2

        manager.delete_document("src/foo/bar.py")

        assert manager.get_document_count() == 1, (
            "After a genuine schema rebuild, delete_document() must "
            "actually delete the exact target"
        )
        results = manager.search("superset", limit=5)
        assert len(results) == 1
        assert results[0]["path"] == "src/foo/bar.py.bak"
