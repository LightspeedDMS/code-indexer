"""Bug #1761 regression: exact-path deletion in TantivyIndexManager.

While fixing the FTS-duplicate-rows-on-reindex defect (delete-before-add
supersession in file_chunking_manager.py), an existing latent bug was
found in TantivyIndexManager's delete_document()/update_document(): the
delete query was built via `index.parse_query(file_path, ["path"])`,
which is a TOKEN match against the "path" field's "default" tokenizer,
not an exact match. Empirically verified: deleting "src/foo/bar.py" also
deleted an unrelated "src/foo/bar.py.bak" document, because its path
tokens are a contiguous prefix match of the deleted path's tokens.

Widening delete_document() usage from the low-traffic watch-mode path to
every indexed file (Bug #1761's fix) would have measurably amplified this
over-deletion risk. The fix adds a new untokenized `_PATH_EXACT_FIELD`
(mirroring the established `_REGEX_VERBATIM_FIELD` verbatim-field
pattern from Bug #1497) and an exact `Query.term_query()` lookup against
it, falling back to the legacy tokenized query only for pre-fix on-disk
indexes that lack the new field.
"""

import tempfile
import time
from pathlib import Path

import pytest

from code_indexer.services.tantivy_index_manager import TantivyIndexManager

pytestmark = pytest.mark.slow


@pytest.fixture
def tantivy_manager():
    """Create and initialize a fresh TantivyIndexManager for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = TantivyIndexManager(Path(tmpdir) / "tantivy_index")
        manager.initialize_index(create_new=True)
        yield manager


def test_delete_document_does_not_over_match_path_token_superset(tantivy_manager):
    """delete_document() must delete ONLY the exact path requested, never
    an unrelated file whose path happens to be a token superset/prefix of
    the deleted path under the "path" field's tokenizer.
    """
    doc_target = {
        "path": "src/foo/bar.py",
        "content": "content A",
        "content_raw": "content A",
        "identifiers": ["content", "A"],
        "line_start": 1,
        "line_end": 1,
        "language": "python",
    }
    doc_superset = {
        "path": "src/foo/bar.py.bak",
        "content": "content B superset tokens",
        "content_raw": "content B superset tokens",
        "identifiers": ["content", "B", "superset", "tokens"],
        "line_start": 1,
        "line_end": 1,
        "language": "python",
    }
    tantivy_manager.add_document(doc_target)
    tantivy_manager.add_document(doc_superset)
    tantivy_manager.commit()
    assert tantivy_manager.get_document_count() == 2

    tantivy_manager.delete_document("src/foo/bar.py")

    assert tantivy_manager.get_document_count() == 1
    results = tantivy_manager.search("superset", limit=5)
    assert len(results) == 1
    assert results[0]["path"] == "src/foo/bar.py.bak"


def test_delete_document_still_removes_the_exact_target(tantivy_manager):
    """Sanity companion: the exact-match fix must still delete the target
    document itself, not just spare the unrelated superset path.
    """
    doc_target = {
        "path": "src/foo/bar.py",
        "content": "content A unique marker QRSTUV",
        "content_raw": "content A unique marker QRSTUV",
        "identifiers": ["content", "A", "QRSTUV"],
        "line_start": 1,
        "line_end": 1,
        "language": "python",
    }
    tantivy_manager.add_document(doc_target)
    tantivy_manager.commit()
    assert tantivy_manager.get_document_count() == 1

    tantivy_manager.delete_document("src/foo/bar.py")

    assert tantivy_manager.get_document_count() == 0
    assert tantivy_manager.search("QRSTUV", limit=5) == []


def _populate_documents(manager: TantivyIndexManager, count: int) -> None:
    """Add `count` distinct committed documents, one per synthetic path."""
    for i in range(count):
        manager.add_document(
            {
                "path": f"src/file_{i}.py",
                "content": f"content marker {i}",
                "content_raw": f"content marker {i}",
                "identifiers": [f"content{i}"],
                "line_start": 1,
                "line_end": 1,
                "language": "python",
            }
        )
    manager.commit()


class TestBug1761Critical1DeferredDeleteNoCommit:
    """Code-review CRITICAL 1 regression guard: delete_document_deferred()
    must never trigger a full commit per call -- that was the per-file
    commit storm the review flagged (~241ms/call measured for
    delete_document() vs ~0.02ms for add_document()).
    """

    def test_delete_document_deferred_does_not_call_commit_inner(self, tantivy_manager):
        """Proves the absence of an implicit commit via OBSERVABLE
        behavior (no mocking of the SUT): get_document_count() reloads
        the index and reflects only COMMITTED state. If
        delete_document_deferred() secretly committed, the document
        count would already have dropped before the explicit commit()
        call below.
        """
        _populate_documents(tantivy_manager, 20)
        assert tantivy_manager.get_document_count() == 20

        for i in range(20):
            tantivy_manager.delete_document_deferred(f"src/file_{i}.py")

        assert tantivy_manager.get_document_count() == 20, (
            "delete_document_deferred() must not commit -- the deleted "
            "documents must still be visible (uncommitted) until the "
            "caller explicitly commits"
        )

        # Caller commits once, in bulk -- this is when the deferred
        # deletes actually take effect.
        tantivy_manager.commit()
        assert tantivy_manager.get_document_count() == 0

    def test_delete_document_deferred_is_measurably_faster_than_committing_delete(
        self, tmp_path
    ):
        """Real measured evidence (not an estimate): delete_document()
        commits on every call; delete_document_deferred() does not. Over
        N files this must be a large, measurable wall-clock difference.
        """
        n = 15

        committing_manager = TantivyIndexManager(tmp_path / "committing")
        committing_manager.initialize_index(create_new=True)
        _populate_documents(committing_manager, n)

        deferred_manager = TantivyIndexManager(tmp_path / "deferred")
        deferred_manager.initialize_index(create_new=True)
        _populate_documents(deferred_manager, n)

        start = time.monotonic()
        for i in range(n):
            committing_manager.delete_document(f"src/file_{i}.py")
        committing_elapsed = time.monotonic() - start

        start = time.monotonic()
        for i in range(n):
            deferred_manager.delete_document_deferred(f"src/file_{i}.py")
        deferred_elapsed = time.monotonic() - start
        deferred_manager.commit()  # mirrors the real end-of-run commit

        assert committing_manager.get_document_count() == 0
        assert deferred_manager.get_document_count() == 0

        assert deferred_elapsed < committing_elapsed / 5, (
            f"Expected delete_document_deferred() to be at least 5x "
            f"faster than delete_document() over {n} files; got "
            f"deferred={deferred_elapsed:.4f}s "
            f"committing={committing_elapsed:.4f}s"
        )


class TestBug1761Critical2DeferredDeleteSkipsLegacyIndex:
    """Code-review CRITICAL 2 regression guard: delete_document_deferred()
    must never attempt a path delete against a physical index built
    before Bug #1761's fix (missing _PATH_EXACT_FIELD) -- the tokenized
    fallback query over-matches sibling paths (see
    test_delete_document_does_not_over_match_path_token_superset above),
    and widening the per-file delete call site to fire on EVERY file
    (CRITICAL 1's fix) would otherwise make that happen on every file of
    every pre-existing on-disk index.
    """

    def _build_legacy_index(self, index_dir: Path) -> None:
        """Build a real on-disk Tantivy index using the PRE-FIX schema
        shape (no path_exact_raw / content_raw_verbatim fields) -- same
        established pattern as test_tantivy_regex_1497.py's
        TestBug1497LegacyIndexBackwardCompatibility._build_legacy_index.
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
        writer = index.writer(50_000_000)

        for path, content, idents in [
            ("src/foo/bar.py", "content A", ["content", "A"]),
            (
                "src/foo/bar.py.bak",
                "content B superset tokens",
                ["content", "B", "superset", "tokens"],
            ),
        ]:
            doc = tantivy.Document()
            doc.add_text("path", path)
            doc.add_text("content", content)
            doc.add_text("content_raw", content)
            doc.add_text("identifiers", " ".join(idents))
            doc.add_unsigned("line_start", 1)
            doc.add_unsigned("line_end", 1)
            doc.add_text("language", "python")
            doc.add_facet("language_facet", Facet.from_string("/python"))
            writer.add_document(doc)
        writer.commit()
        writer.wait_merging_threads()

    def test_deferred_delete_on_legacy_index_skips_rather_than_over_deletes(
        self, tmp_path
    ):
        index_dir = tmp_path / "legacy_tantivy_index"
        self._build_legacy_index(index_dir)

        manager = TantivyIndexManager(index_dir)
        manager.initialize_index(create_new=False)
        assert manager.get_document_count() == 2

        # Must not crash (proves the field-availability gate fires BEFORE
        # any query is built against the missing field) and must not
        # delete anything (the safe legacy behavior).
        manager.delete_document_deferred("src/foo/bar.py")
        manager.commit()

        assert manager.get_document_count() == 2, (
            "Deferred delete must be a no-op on a legacy (pre-#1761-schema) "
            "index -- neither deleting the target nor, worse, the sibling"
        )
        results = manager.search("superset", limit=5)
        assert len(results) == 1
        assert results[0]["path"] == "src/foo/bar.py.bak"
