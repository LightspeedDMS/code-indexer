"""Bug #1763: #1761's FTS dedup fix is inert on pre-existing indexes --
schema_version never used to trigger a rebuild.

#1761's fix (deferred per-file FTS delete-before-add supersession, gated
on TantivyIndexManager._path_exact_field_available()) only takes effect
on an on-disk Tantivy index that already has the new `path_exact_raw`
schema field. Any on-disk index built BEFORE that fix lacks the field, so
the gate returns False and the new per-file delete is a safe no-op --
which also means duplicates keep accumulating exactly as before, forever,
on every pre-existing index. smart_indexer.py's
`create_new_fts = force_full or not fts_index_exists` has no
schema-compatibility check -- an index just needs to exist to be reused.

Fix: `TantivyIndexManager.schema_needs_rebuild()` -- a new public,
stateless (uncached, callable before initialize_index()) method that
reads the PHYSICAL on-disk meta.json directly (the same proven,
already-tested technique `_path_exact_field_available()` uses) and
reports whether the index is missing `_PATH_EXACT_FIELD`. Wired into the
create_new decision at smart_indexer.py and cli.py's watch-mode FTS
handler init -- see test_smart_indexer.py::TestFtsBootstrap for the
production-wiring-level tests proving smart_index() itself calls it.
"""

import hashlib
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import Mock

import pytest

from code_indexer.services.clean_slot_tracker import CleanSlotTracker
from code_indexer.services.file_chunking_manager import FileChunkingManager
from code_indexer.services.tantivy_index_manager import TantivyIndexManager

pytestmark = pytest.mark.slow

# Tantivy IndexWriter heap size used by the raw-tantivy legacy-index test
# helper below -- mirrors the value already used by the established
# _build_legacy_index pattern in test_tantivy_path_exact_delete_1761.py.
_TEST_WRITER_HEAP_BYTES = 50_000_000

# FileChunkingManager worker-pool sizing for the (lightweight, single-file)
# processing pass exercised below -- values are arbitrary but fixed so the
# pool is deterministically sized rather than magic-numbered inline.
_TEST_THREAD_COUNT = 2
_TEST_MAX_SLOTS = 4

# Synthetic chunk byte-size used by the single-chunk fake chunker below --
# not meaningful beyond being a fixed, deterministic placeholder.
_FAKE_CHUNK_SIZE = 40

# Bound on how long a single-file processing future may take before the
# test fails outright (well above real local runtime, generous safety
# margin against CI slowness).
_PROCESSING_FUTURE_TIMEOUT_SECONDS = 10.0

# FTS search result caps used by assertions below -- more than enough to
# surface a duplicate-row defect (which would show as 2 results) while
# still being a small, fast query.
_DUPLICATE_CHECK_SEARCH_LIMIT = 10
_MARKER_SEARCH_LIMIT = 5

# A single, reusable minimal legacy document for tests that only need
# "some pre-existing legacy-schema index" without caring about its
# specific content.
_SINGLE_LEGACY_DOC = [
    {
        "path": "src/a.py",
        "content": "hello",
        "content_raw": "hello",
        "identifiers": ["hello"],
        "line_start": 1,
        "line_end": 1,
        "language": "python",
    }
]


def _build_legacy_index(index_dir: Path, documents: List[Dict[str, Any]]) -> None:
    """Build a real on-disk Tantivy index using the PRE-#1761 schema shape
    (no path_exact_raw / content_raw_verbatim fields), pre-populated with
    `documents`. `documents` entries are duck-typed dicts with
    heterogeneous value types (str path/content, List[str] identifiers,
    int line numbers) -- Any is the correct annotation for that shape,
    same convention as the established _build_legacy_index in
    test_tantivy_path_exact_delete_1761.py.
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


def _prepare_fts_index_for_reindex(
    fts_manager: TantivyIndexManager, fts_index_dir: Path, force_full: bool
) -> bool:
    """Mirrors smart_indexer.py's real pre-initialize_index() FTS block
    (post-#1763 fix) EXACTLY, one-for-one, so tests exercise the
    identical logic the production call site uses rather than a
    hand-rolled reimplementation:

        fts_index_exists = (fts_index_dir / "meta.json").exists()
        fts_schema_stale = fts_index_exists and fts_manager.schema_needs_rebuild()
        if fts_schema_stale:
            shutil.rmtree(fts_index_dir)
        create_new_fts = force_full or not fts_index_exists or fts_schema_stale

    The rmtree step is required: Tantivy raises "Schema error: An index
    exists but the schema does not match." if initialize_index(
    create_new=True) is called on a directory that still physically
    contains a different-schema index -- clearing it first is what makes
    create_new=True a genuine rebuild rather than a crash.

    NOTE: this exercises TantivyIndexManager.schema_needs_rebuild()'s real
    behavior against a real on-disk index (the unit under test in this
    file). Separate, wiring-level tests in
    test_smart_indexer.py::TestFtsBootstrap prove smart_indexer.py's
    actual production code calls schema_needs_rebuild() and that its
    answer really drives create_new_fts through the real smart_index()
    entry point.
    """
    import shutil

    fts_index_exists = (fts_index_dir / "meta.json").exists()
    fts_schema_stale = fts_index_exists and fts_manager.schema_needs_rebuild()
    if fts_schema_stale:
        shutil.rmtree(fts_index_dir)
    return force_full or not fts_index_exists or fts_schema_stale


def _build_and_get_rebuild_decision(
    tmp_path: Path, subdir: str, documents: List[Dict[str, Any]]
) -> Tuple[TantivyIndexManager, Path, bool]:
    """Build a legacy index under tmp_path/subdir, then return
    (manager, index_dir, create_new_decision) using the real
    prepare-for-reindex flow. Shared setup for several tests below."""
    index_dir = tmp_path / subdir
    _build_legacy_index(index_dir, documents)
    manager = TantivyIndexManager(index_dir)
    create_new = _prepare_fts_index_for_reindex(manager, index_dir, force_full=False)
    return manager, index_dir, create_new


def _make_stub_vector_manager() -> Mock:
    """Configured Mock stand-in for VectorCalculationManager: returns a
    deterministic, always-truthy embedding per chunk via submit_batch_task.
    Embedding computation is NOT under test in this file (FTS
    schema-rebuild self-healing is); this stub only lets a real
    FileChunkingManager run its real FTS write path (the code actually
    under test) without a live embedding provider network call."""
    manager = Mock()
    manager.cancellation_event = threading.Event()
    manager.embedding_provider = Mock()
    manager.embedding_provider.get_current_model.return_value = "voyage-large-2"
    manager.embedding_provider._get_model_token_limit.return_value = 120000

    def _submit_batch_task(
        chunk_texts: List[str], metadata: Dict[str, Any]
    ) -> "Future[Any]":
        from code_indexer.services.vector_calculation_manager import VectorResult

        future: "Future[Any]" = Future()
        embeddings = tuple(
            tuple(float(b) / 255.0 for b in hashlib.sha256(text.encode()).digest()[:8])
            for text in chunk_texts
        )
        result = VectorResult(
            task_id="batch_1",
            embeddings=embeddings,
            metadata=metadata.copy(),
            processing_time=0.0,
            error=None,
        )
        future.set_result(result)
        return future

    manager.submit_batch_task.side_effect = _submit_batch_task
    return manager


def _make_stub_vector_store_client() -> Mock:
    """Configured Mock stand-in for the vector store: accepts every
    upsert, reports no pre-existing content hashes. The vector store
    itself is NOT under test here -- only whether TantivyIndexManager's
    FTS write path resolves the #1761/#1763 duplicate-row defect."""
    client = Mock()
    client.upsert_points.return_value = True
    client.collection_exists.return_value = True
    client.create_collection.return_value = True
    client.get_existing_content_hashes.return_value = {}
    return client


def _make_single_chunk_chunker(unique_token: str) -> Mock:
    """Deterministic chunker returning exactly ONE chunk containing
    `unique_token`, with a fixed line range -- mirrors the real
    FixedSizeChunker's per-chunk dict shape. Chunking logic itself is not
    under test here; only the FTS document write path is."""
    chunker = Mock()
    chunker.chunk_file.return_value = [
        {
            "text": f"{unique_token} occurs exactly once in this file",
            "chunk_index": 0,
            "total_chunks": 1,
            "size": _FAKE_CHUNK_SIZE,
            "file_path": None,
            "file_extension": "py",
            "line_start": 2,
            "line_end": 2,
        }
    ]
    return chunker


def _process_file_once(
    codebase_dir: Path,
    test_file: Path,
    fts_manager: TantivyIndexManager,
    unique_token: str,
) -> None:
    """Runs exactly ONE real FileChunkingManager processing pass for
    `test_file`, wired to a REAL TantivyIndexManager -- the actual
    production FTS write path under test in this module -- with
    deterministic stand-ins only for the unrelated chunking/embedding/
    vector-store dependencies."""
    with FileChunkingManager(
        vector_manager=_make_stub_vector_manager(),
        chunker=_make_single_chunk_chunker(unique_token),
        vector_store_client=_make_stub_vector_store_client(),
        thread_count=_TEST_THREAD_COUNT,
        slot_tracker=CleanSlotTracker(max_slots=_TEST_MAX_SLOTS),
        codebase_dir=codebase_dir,
        fts_manager=fts_manager,
    ) as manager:
        metadata = {
            "project_id": "proj",
            "file_hash": "sha256:abc123",
            "collection_name": "col",
            "git_available": False,
        }
        future = manager.submit_file_for_processing(test_file, metadata, None)
        result = future.result(timeout=_PROCESSING_FUTURE_TIMEOUT_SECONDS)
        assert result.success, f"File processing failed: {result.error}"


class TestBug1763SchemaNeedsRebuildDetection:
    """Direct unit coverage of the new schema_needs_rebuild() method."""

    def test_legacy_index_needs_rebuild(self, tmp_path: Path) -> None:
        index_dir = tmp_path / "legacy_index"
        _build_legacy_index(index_dir, _SINGLE_LEGACY_DOC)

        manager = TantivyIndexManager(index_dir)
        assert manager.schema_needs_rebuild() is True

    def test_current_schema_index_does_not_need_rebuild(self, tmp_path: Path) -> None:
        index_dir = tmp_path / "current_index"
        manager = TantivyIndexManager(index_dir)
        manager.initialize_index(create_new=True)

        # Fresh instance mimicking a new process reopening the same
        # on-disk directory -- must independently detect the schema is
        # already current.
        reopened = TantivyIndexManager(index_dir)
        assert reopened.schema_needs_rebuild() is False

    def test_nonexistent_index_does_not_need_rebuild(self, tmp_path: Path) -> None:
        # No physical index at this path yet -- nothing to rebuild; the
        # caller's own "index doesn't exist" branch handles this case.
        manager = TantivyIndexManager(tmp_path / "does_not_exist_yet")
        assert manager.schema_needs_rebuild() is False

    def test_meta_json_read_error_fails_toward_no_rebuild(self, tmp_path: Path) -> None:
        """MEDIUM-3 (#1763 code review): schema_needs_rebuild() must fail
        toward the CONSERVATIVE answer (False, don't rebuild) on a
        meta.json read/parse error -- same fail-safe direction as the
        sibling _path_exact_field_available() method. Before the fix, any
        transient read glitch (OSError, PermissionError, a partial read
        mid-write producing invalid JSON) made this method return True,
        which drives create_new_fts=True and triggers a destructive
        shutil.rmtree() of a possibly perfectly healthy index -- a
        one-off filesystem hiccup should never delete real data.

        meta.json existing but being unparseable (malformed JSON, as a
        partial/corrupted write would produce) exercises the exact
        `except Exception` branch under test without needing to mock the
        filesystem layer itself.
        """
        index_dir = tmp_path / "corrupted_meta_index"
        index_dir.mkdir(parents=True)
        (index_dir / "meta.json").write_text("{not valid json")

        manager = TantivyIndexManager(index_dir)
        assert manager.schema_needs_rebuild() is False, (
            "A meta.json read/parse error must fail toward False (no "
            "rebuild) -- failing toward True risks destroying a healthy "
            "index on a transient filesystem glitch"
        )


class TestBug1763LegacyIndexRebuiltRatherThanReused:
    """TDD requirement (a) + (b): a real on-disk legacy-schema index, when
    reopened via the real create_new_fts-style decision logic, gets
    rebuilt (new schema present) rather than silently reused -- and a
    SECOND reopen of the now-rebuilt index does NOT trigger a second
    rebuild (proves no repeated-rebuild regression at fleet scale).
    """

    def test_legacy_index_gets_rebuilt_on_reopen(self, tmp_path: Path) -> None:
        manager, _index_dir, create_new = _build_and_get_rebuild_decision(
            tmp_path, "legacy_index", _SINGLE_LEGACY_DOC
        )
        assert create_new is True, (
            "A legacy-schema index must be flagged for a one-time rebuild "
            "by the real create_new_fts decision logic"
        )

        manager.initialize_index(create_new=create_new)

        # Rebuild must have actually happened: the physical on-disk index
        # now has the new field, and the pre-existing legacy document is
        # gone (create_new=True always starts from empty -- the caller's
        # normal full-reindex loop is what repopulates it; see the
        # duplicate-results test below for that full flow).
        assert manager._path_exact_field_available() is True
        assert manager.get_document_count() == 0

    def _rebuild_and_add_marker(self, tmp_path: Path) -> Path:
        manager, index_dir, create_new = _build_and_get_rebuild_decision(
            tmp_path, "legacy_index", _SINGLE_LEGACY_DOC
        )
        assert create_new is True
        manager.initialize_index(create_new=create_new)
        manager.add_document(
            {
                "path": "src/marker.py",
                "content": "MARKERDOC1763 unique content",
                "content_raw": "MARKERDOC1763 unique content",
                "identifiers": ["MARKERDOC1763"],
                "line_start": 1,
                "line_end": 1,
                "language": "python",
            }
        )
        manager.commit()
        assert manager.get_document_count() == 1
        return index_dir

    def test_second_reopen_of_rebuilt_index_does_not_rebuild_again(
        self, tmp_path: Path
    ) -> None:
        # --- Run N: first post-fix run rebuilds the legacy index and adds
        # one marker document. ---
        index_dir = self._rebuild_and_add_marker(tmp_path)

        # --- Run N+1: a fresh manager instance (mimics a new `cidx index`
        # process) reopens the now-current-schema index. It must NOT be
        # flagged for another rebuild. ---
        manager_n1 = TantivyIndexManager(index_dir)
        create_new_n1 = _prepare_fts_index_for_reindex(
            manager_n1, index_dir, force_full=False
        )
        assert create_new_n1 is False, (
            "Repeated-rebuild regression: a schema that is already current "
            "must not be flagged for rebuild again on every subsequent run "
            "-- at fleet scale (~900 repos) that would be a permanent, "
            "unbounded rebuild cost instead of a one-time migration"
        )

        manager_n1.initialize_index(create_new=create_new_n1)

        # The marker document from run N must have SURVIVED -- proof this
        # was a genuine open-existing, not a silent re-wipe.
        assert manager_n1.get_document_count() == 1
        results = manager_n1.search("MARKERDOC1763", limit=_MARKER_SEARCH_LIMIT)
        assert len(results) == 1
        assert results[0]["path"] == "src/marker.py"


class TestBug1763DuplicateResultsResolvedFromLegacyIndex:
    """TDD requirement (c): proof, through the real production FTS write
    path (FileChunkingManager + TantivyIndexManager -- the components the
    bug actually lives in), that the original #1761 duplicate-results
    scenario is now actually resolved even starting from a legacy on-disk
    index -- the exact gap #1763 was filed about (#1761's fix is a safe
    no-op on a legacy index, so duplicates kept accumulating). Chunking
    and embedding are unrelated concerns, stood in for with deterministic
    stubs so this test stays fast and focused on the FTS defect.
    """

    _UNIQUE_TOKEN = "ZORPTASTIC1763"

    def _seed_legacy_indexed_file(self, tmp_path: Path) -> tuple:
        """Pass 1 (pre-#1761, pre-#1763 world): writes the source file to
        disk and seeds a legacy-schema on-disk FTS index already
        containing ONE document for it (as if a prior `cidx index` run,
        before either fix existed, had indexed it once). Returns
        (codebase_dir, test_file, fts_index_dir)."""
        codebase_dir = tmp_path
        test_file = codebase_dir / "e2e_file.py"
        chars_written = test_file.write_text(
            f"# line 1\n# {self._UNIQUE_TOKEN} occurs exactly once\n"
        )
        assert chars_written > 0

        fts_index_dir = codebase_dir / ".code-indexer" / "tantivy_index"
        legacy_doc = {
            "path": "e2e_file.py",
            "content": f"{self._UNIQUE_TOKEN} occurs exactly once in this file",
            "content_raw": f"{self._UNIQUE_TOKEN} occurs exactly once in this file",
            "identifiers": [
                self._UNIQUE_TOKEN,
                "occurs",
                "exactly",
                "once",
                "in",
                "this",
                "file",
            ],
            "line_start": 2,
            "line_end": 2,
            "language": "py",
        }
        _build_legacy_index(fts_index_dir, [legacy_doc])
        return codebase_dir, test_file, fts_index_dir

    def test_reindexing_legacy_indexed_file_yields_one_result_not_two(
        self, tmp_path: Path
    ) -> None:
        codebase_dir, test_file, fts_index_dir = self._seed_legacy_indexed_file(
            tmp_path
        )

        # --- Pass 2: real create_new_fts-style decision logic (post-#1763
        # fix), exactly as smart_indexer.py computes it for every
        # `cidx index` run against an existing FTS index. ---
        fts_manager_pass2 = TantivyIndexManager(fts_index_dir)
        create_new_fts = _prepare_fts_index_for_reindex(
            fts_manager_pass2, fts_index_dir, force_full=False
        )
        assert create_new_fts is True, (
            "The legacy index must be detected as stale so pass 2 performs "
            "a genuine full rebuild instead of reusing it"
        )
        fts_manager_pass2.initialize_index(create_new=create_new_fts)

        _process_file_once(
            codebase_dir, test_file, fts_manager_pass2, self._UNIQUE_TOKEN
        )
        fts_manager_pass2.commit()

        results = fts_manager_pass2.search(
            self._UNIQUE_TOKEN, limit=_DUPLICATE_CHECK_SEARCH_LIMIT
        )

        assert len(results) == 1, (
            f"Expected exactly 1 FTS result for unique token "
            f"'{self._UNIQUE_TOKEN}' after self-healing rebuild of a "
            f"legacy index (file has exactly 1 real occurrence), got "
            f"{len(results)}: {results}"
        )
