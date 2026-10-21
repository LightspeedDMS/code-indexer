"""
Tantivy Index Manager for full-text search indexing.

Manages Tantivy-based FTS indexes alongside semantic vector indexes,
providing fast exact text search capabilities.
"""

import json
import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Collection, Dict, List, Optional, Tuple, cast

from ..utils.bounded_regex import BoundedPattern, PatternBudgetExhausted

if TYPE_CHECKING:
    from tantivy import Index, Schema  # type: ignore[import-untyped]

from .fts_query_syntax import (  # noqa: F401  (re-exported query syntax)
    _BOOL_OPS,
    _contains_valid_boolean_ops,
    _drop_orphaned_bool_ops,
    sanitize_fts_query,
)

logger = logging.getLogger(__name__)

# Bug #1497: field name for verbatim (untokenized) regex matching.
#
# RegexQuery matches whole TERMS in the term dictionary, not raw document
# text. "content"/"content_raw"/"identifiers" all use the "default" tokenizer,
# which splits on non-alphanumeric characters (including underscore), so an
# identifier like "cancel_job" is indexed as two separate terms "cancel" and
# "job" -- no single term ever contains the substring "cancel_job". This new
# field stores each document's raw content as ONE untokenized term
# (tokenizer_name="raw"), so a regex wrapped for substring semantics can
# match it via Tantivy's own DFA-based regex engine (linear time, unlike
# falling back to Python-level regex scanning across broad candidate sets).
_REGEX_VERBATIM_FIELD = "content_raw_verbatim"

# Bug #1761: field name for verbatim (untokenized) exact path matching.
#
# delete_document()/update_document() need to remove ALL prior FTS
# documents for a given file path before/while a file is (re)indexed, so
# that reprocessing an already-indexed, unchanged file (e.g. two separate
# `cidx index` runs against the same on-disk Tantivy index, or an
# activation branch-delta reindex) never leaves stale duplicate chunk
# documents behind. The existing "path" field uses the "default"
# tokenizer, so a delete built via `index.parse_query(file_path, ["path"])`
# is a TOKEN match, not an exact-path match -- verified empirically that a
# path whose tokens are a contiguous prefix of another path's tokens (e.g.
# deleting "src/foo/bar.py" while "src/foo/bar.py.bak" also exists) gets
# BOTH documents deleted, a real over-deletion risk once delete-by-path is
# used on every indexed file rather than only in the low-traffic watch-mode
# path. This field stores each document's path as ONE untokenized term
# (tokenizer_name="raw", same technique as _REGEX_VERBATIM_FIELD), so a
# Query.term_query() against it matches only the exact path.
_PATH_EXACT_FIELD = "path_exact_raw"

# Bug #1497: a Tantivy-decided hit with no extracted highlight keeps this position.
_NO_MATCH_POSITION = -1

# Bug #1497: per-call cap on highlight extraction (one budget per search).
_REGEX_EXTRACTION_TIMEOUT_SECONDS = 0.03

# Bug #1497 (follow-up): substring marker identifying Tantivy's
# "Compiled regex exceeds size limit of N states" RegexQueryError, as
# distinct from a genuine pattern syntax error. See
# _is_regex_state_limit_error() and its use in search() below.
_REGEX_STATE_LIMIT_ERROR_MARKER = "size limit"

# Story #1494 AC2 (Finding A4, GIL-blocking analysis report): caps the
# number of per-result candidates that receive the GIL-held Python-level
# regex-extraction step in search() below. See that method's use of this
# constant for the full rationale and fallback behavior.
_MAX_REGEX_EXTRACTION_CANDIDATES = 2000

# The "effectively unlimited" hit count used for limit=0 (grep-like) searches.
# No search() ever asks Tantivy for more hits than this; a limit above it
# returns at most this many results.
_FTS_UNLIMITED_HITS = 100000

# #2047: a filtered FTS search inspects at most this many hits per requested
# result in one repository (50 x limit; 500 for the default limit of 10).
# Why 50: the extension push-down already restricts hits to the requested
# extensions for every alphanumeric value, so the refetch only compensates for
# hits the suffix re-check drops -- extensionless files (stored as "txt") when
# "txt" is requested, and every hit for a value that cannot be pushed down
# ("c++", "foo-bar"). 50 x limit lets matching files be outnumbered 49:1 by
# dropped hits before the answer can come up short, while a fleet-wide
# filtered query over 900 repositories stays near 450k inspected hits at the
# default limit, all under one request time budget.
_FTS_FILL_HITS_PER_RESULT = 50


def _is_regex_state_limit_error(exc: Exception) -> bool:
    """
    Bug #1497 (follow-up): detect whether `exc` is Tantivy's DFA
    state-limit RegexQueryError (a structurally valid pattern that is too
    complex for the fixed 1000-state limit -- e.g. a Unicode `\\w`/`\\s`
    class combined with the required substring-wrapping wildcards on both
    sides), as opposed to a genuine regex syntax error (e.g. an unmatched
    parenthesis).

    Proven empirically: wrapping a pattern containing `\\w` with
    `[\\s\\S]*` on both sides reliably exceeds the limit regardless of
    wrapping syntax (anchors and lazy quantifiers are rejected outright by
    Tantivy's regex parser, and there is no Python-exposed way to raise
    the limit), while the same wrap without `\\w` compiles fine. search()
    uses this to gracefully degrade to legacy token-level matching for
    just this one search, rather than failing the entire query.
    """
    return _REGEX_STATE_LIMIT_ERROR_MARKER in str(exc).lower()


class TantivyIndexManager:
    """
    Manages Tantivy full-text search index for CIDX.

    Thread Safety:
        The Tantivy writer is thread-safe at the Rust level (Arc<Mutex<...>>).
        However, concurrent calls to update_document() or delete_document()
        from multiple threads may result in unpredictable commit ordering.

        For watch mode with multiple handlers, this is acceptable because:
        - Each file operation is atomic (delete + add + commit)
        - Tantivy's MVCC architecture prevents read/write conflicts
        - Final state is eventually consistent regardless of commit order

    Performance Characteristics:
        - First operation: ~300-600ms (includes index initialization)
        - Subsequent operations: ~50-150ms per commit
        - Search visibility: Immediate after commit completes
        - Commit latency target: 5-50ms (actual: 100-150ms with overhead)
    """

    def __init__(self, index_dir: Path):
        """
        Initialize the Tantivy index manager.

        Args:
            index_dir: Directory where Tantivy index will be stored
        """
        self.index_dir = Path(index_dir)
        self._index: Optional[Index] = None
        self._schema: Optional[Schema] = None
        self._writer: Optional[Any] = None
        self._heap_size = 1_000_000_000  # Fixed 1GB heap size
        self._metadata_file = self.index_dir / "metadata.json"
        self._lock = threading.Lock()  # Thread safety for writer operations

        # Bug #1497: lazily-computed, cached flag for whether the PHYSICAL
        # on-disk index actually has _REGEX_VERBATIM_FIELD. Legacy indexes
        # built before this fix lack it; querying a field the physical index
        # doesn't have panics at the Rust FFI boundary. None = not yet checked.
        self._verbatim_field_available: Optional[bool] = None

        # Bug #1761: lazily-computed, cached flag for whether the PHYSICAL
        # on-disk index actually has _PATH_EXACT_FIELD. Legacy indexes built
        # before this fix lack it; querying a field the physical index
        # doesn't have panics at the Rust FFI boundary. None = not yet
        # checked. Named distinctly from the _path_exact_field_available()
        # METHOD below (mirrors the existing _verbatim_field_available
        # attribute / _regex_verbatim_field_available() method split) --
        # an identically-named attribute would shadow the method on this
        # instance.
        self._exact_path_field_available: Optional[bool] = None

        # Try to import tantivy
        try:
            import tantivy

            self._tantivy = tantivy
        except ImportError as e:
            logger.error("Tantivy library not installed")
            raise ImportError(
                "Tantivy is required for FTS indexing. "
                "Install it with: pip install tantivy==0.25.0"
            ) from e

    def get_schema(self) -> Dict[str, Any]:
        """
        Get the Tantivy schema configuration.

        Returns:
            Dictionary describing the schema fields
        """
        if self._schema is None:
            self._create_schema()

        # Return dictionary representation for testing
        return {
            "path": "stored",
            "content": "tokenized",
            "content_raw": "stored",
            "identifiers": "simple_tokenizer",
            "line_start": "u64_indexed",
            "line_end": "u64_indexed",
            "language": "stored_text",
            "language_facet": "facet",
        }

    def _create_schema(self) -> None:
        """Create the Tantivy schema with required fields."""
        schema_builder = self._tantivy.SchemaBuilder()

        # path: stored field for file path
        schema_builder.add_text_field("path", stored=True)

        # content: tokenized field for full-text search
        schema_builder.add_text_field("content", stored=False)

        # content_raw: stored field for retrieving original content
        schema_builder.add_text_field("content_raw", stored=True)

        # identifiers: simple tokenizer for exact identifier matches
        schema_builder.add_text_field("identifiers", stored=True)

        # line_start/line_end: indexed u64 fields for line number filtering
        schema_builder.add_unsigned_field("line_start", indexed=True, stored=True)
        schema_builder.add_unsigned_field("line_end", indexed=True, stored=True)

        # language: stored as text field for retrieval AND facet for filtering
        schema_builder.add_text_field("language", stored=True)
        schema_builder.add_facet_field("language_facet")

        # Bug #1497: verbatim (untokenized) field for regex matching.
        # MUST be added LAST so existing field IDs are unchanged for indexes
        # built before this fix (backward compatibility -- see
        # _regex_verbatim_field_available()).
        schema_builder.add_text_field(
            _REGEX_VERBATIM_FIELD, stored=False, tokenizer_name="raw"
        )

        # Bug #1761: verbatim (untokenized) field for exact path-based
        # delete/lookup. MUST be added LAST (after _REGEX_VERBATIM_FIELD) so
        # existing field IDs are unchanged for indexes built before this fix
        # (backward compatibility -- see _path_exact_field_available()).
        schema_builder.add_text_field(
            _PATH_EXACT_FIELD, stored=False, tokenizer_name="raw"
        )

        self._schema = schema_builder.build()

    def initialize_index(self, create_new: bool = True) -> None:
        """
        Initialize the Tantivy index.

        Args:
            create_new: If True, create new index. If False, open existing.

        Raises:
            PermissionError: If directory permissions are insufficient
            ImportError: If Tantivy library is not available
        """
        try:
            # Create directory if it doesn't exist
            if create_new:
                self.index_dir.mkdir(parents=True, exist_ok=True)

            # Check permissions
            if not self.index_dir.exists():
                raise PermissionError(
                    f"Cannot create index directory: {self.index_dir}"
                )

            # Test write permissions
            test_file = self.index_dir / ".permission_test"
            try:
                test_file.touch()
                test_file.unlink()
            except Exception as e:
                raise PermissionError(
                    f"Insufficient permissions for index directory: {self.index_dir}"
                ) from e

            # Create schema if needed
            if self._schema is None:
                self._create_schema()

            # Create or open index
            assert self._schema is not None  # For mypy
            if create_new or not (self.index_dir / "meta.json").exists():
                self._index = self._tantivy.Index(self._schema, str(self.index_dir))
                logger.info(
                    f"🔨 FULL FTS INDEX BUILD: Creating Tantivy index from scratch at {self.index_dir}"
                )
            else:
                self._index = self._tantivy.Index.open(str(self.index_dir))
                logger.info(f"Opened existing Tantivy index at {self.index_dir}")

            # Create writer with fixed heap size
            assert self._index is not None  # For mypy
            self._writer = self._index.writer(self._heap_size)

            # Save metadata
            self._save_metadata()

        except PermissionError:
            logger.error(f"Permission denied for index directory: {self.index_dir}")
            raise
        except Exception as e:
            logger.error(f"Failed to initialize Tantivy index: {e}")
            raise

    def open_for_search(self) -> None:
        """Open the Tantivy index for read-only search WITHOUT creating an IndexWriter.

        This is the correct entry point for the server FTS query path.  The server
        creates one TantivyIndexManager per query across potentially many concurrent
        uvicorn workers (separate OS processes).  Using initialize_index() on that
        path is wrong because it always calls self._index.writer(...) which takes the
        exclusive .tantivy-writer.lock — causing LockBusy failures when multiple
        workers attempt concurrent FTS reads (Bug #1233).

        Invariants:
          - self._index is set so search() works normally.
          - self._schema is set so _build_search_query() works normally.
          - self._writer remains None — the writer lockfile is NEVER acquired.

        Raises:
            FileNotFoundError: If the index directory does not exist.
            RuntimeError: If the Tantivy index cannot be opened (e.g. corrupted).
        """
        if not self.index_dir.exists():
            raise FileNotFoundError(
                f"FTS index directory does not exist: {self.index_dir}"
            )

        try:
            # Open existing index without creating/recreating the schema or writer.
            self._index = self._tantivy.Index.open(str(self.index_dir))
            # Rebuild the schema from scratch so _build_search_query() has a valid
            # schema object — tantivy-py's Index.open() does not expose the schema
            # directly in a way compatible with our query-building helpers.
            self._create_schema()
        except Exception as e:
            logger.error(f"Failed to open Tantivy index for search: {e}")
            raise RuntimeError(f"Cannot open FTS index at {self.index_dir}: {e}") from e

        # Explicitly ensure writer is not set (defensive — _create_schema never
        # touches _writer, but make the invariant visible).
        assert self._writer is None, "open_for_search() must never create a writer"
        logger.debug(f"Opened Tantivy index for read-only search at {self.index_dir}")

    def open_from_cached_index(self, index: "Index") -> None:
        """Adopt an already-open Tantivy Index object with NO disk I/O.

        Bug #1730: daemon mode keeps one already-open ``tantivy.Index`` per
        project resident in its own long-lived in-memory cache
        (``CacheEntry.tantivy_index``). Constructing a brand new
        TantivyIndexManager per query and reopening from disk via
        ``initialize_index()``/``open_for_search()`` defeats that cache on
        every single query — and ``initialize_index()`` additionally
        acquires the exclusive writer lock on every read (Bug #1233). This
        method adopts the caller-supplied, already-open index directly: no
        ``Index.open()`` call, no writer lock, only the cheap, purely
        in-memory schema rebuild ``search()`` needs.

        Invariants (mirrors ``open_for_search()``):
          - self._index is set so search() works normally.
          - self._schema is rebuilt so _build_search_query()/regex helpers work.
          - self._writer remains None — no writer lock is ever acquired here.

        Args:
            index: An already-open tantivy.Index instance (e.g. from a
                daemon's in-memory CacheEntry). Typed as the real
                ``tantivy.Index`` (imported under TYPE_CHECKING at module
                top, matching ``self._index``'s own annotation) rather than
                ``Any``, since callers are expected to hand this method a
                genuine already-open index, never an arbitrary object.

        Raises:
            ValueError: If index is None -- callers must check
                cache-availability themselves and fall back to
                open_for_search() instead of calling this method with
                nothing to adopt.
        """
        if index is None:
            raise ValueError(
                "open_from_cached_index() requires an already-open Index; "
                "got None. Callers must fall back to open_for_search() "
                "when no cached index is available."
            )

        self._index = index
        self._create_schema()

        assert self._writer is None, (
            "open_from_cached_index() must never create a writer"
        )
        logger.debug(
            f"Adopted cached Tantivy index for read-only search at {self.index_dir}"
        )

    def get_writer_heap_size(self) -> int:
        """
        Get the configured writer heap size.

        Returns:
            Heap size in bytes (1GB)
        """
        return self._heap_size

    def add_document(self, doc: Dict[str, Any]) -> None:
        """
        Add a document to the FTS index.

        Args:
            doc: Document dictionary with required fields

        Raises:
            ValueError: If required fields are missing
            RuntimeError: If writer is not initialized
        """
        if self._writer is None:
            raise RuntimeError(
                "Index writer not initialized. Call initialize_index() first."
            )

        # Validate required fields
        required_fields = [
            "path",
            "content",
            "content_raw",
            "identifiers",
            "line_start",
            "line_end",
            "language",
        ]
        missing_fields = [f for f in required_fields if f not in doc]
        if missing_fields:
            raise ValueError(f"Missing required fields: {missing_fields}")

        try:
            # Create Tantivy document
            tantivy_doc = self._tantivy.Document()

            # Add fields
            tantivy_doc.add_text("path", doc["path"])
            tantivy_doc.add_text("content", doc["content"])
            tantivy_doc.add_text("content_raw", doc["content_raw"])

            # Bug #1761: mirror path into the untokenized exact-match field
            # used for precise delete-by-path (supersession on reindex).
            # Safe no-op on a legacy (pre-fix) physical index that lacks
            # this field -- tantivy silently ignores add_text() calls for
            # unknown fields.
            tantivy_doc.add_text(_PATH_EXACT_FIELD, doc["path"])

            # Bug #1497: mirror content_raw into the untokenized verbatim
            # field used for regex substring matching. Safe no-op on a
            # legacy (pre-fix) physical index that lacks this field --
            # tantivy silently ignores add_text() calls for unknown fields.
            tantivy_doc.add_text(_REGEX_VERBATIM_FIELD, doc["content_raw"])

            # Add identifiers (convert list to space-separated string)
            identifiers_str = (
                " ".join(doc["identifiers"])
                if isinstance(doc["identifiers"], list)
                else str(doc["identifiers"])
            )
            tantivy_doc.add_text("identifiers", identifiers_str)

            # Add line numbers
            tantivy_doc.add_unsigned("line_start", int(doc["line_start"]))
            tantivy_doc.add_unsigned("line_end", int(doc["line_end"]))

            # Add language as text field (for retrieval) and facet (for filtering)
            tantivy_doc.add_text("language", doc["language"])
            from tantivy import Facet

            language_facet = Facet.from_string(f"/{doc['language']}")
            tantivy_doc.add_facet("language_facet", language_facet)

            # Add to writer (thread-safe)
            with self._lock:
                self._writer.add_document(tantivy_doc)

        except Exception as e:
            logger.error(f"Failed to add document: {e}")
            raise

    def _commit_inner(self) -> None:
        """Core commit logic: commit, wait for merges, re-create writer.

        MUST be called while holding self._lock. Does not acquire the lock
        itself.  Separating this from commit() allows update_document() and
        delete_document() to reuse the same logic inside their own lock blocks
        without a nested lock acquisition (which would deadlock on a
        non-reentrant threading.Lock).
        """
        assert self._writer is not None  # Callers guarantee writer exists
        writer = self._writer
        writer.commit()
        writer.wait_merging_threads()
        # Bug #1799: Tantivy releases the directory lockfile only when the
        # underlying Rust IndexWriter is dropped, which for this PyO3-wrapped
        # object happens only once its CPython refcount reaches zero. Before
        # this fix, `self._writer = self._index.writer(self._heap_size)`
        # evaluated the RHS (acquiring a NEW writer) while BOTH the local
        # `writer` variable above AND the not-yet-reassigned `self._writer`
        # attribute still referenced the OLD writer -- delaying the lock
        # release and causing an intermittent LockBusy ValueError under
        # production load. Drop every Python reference to the old writer
        # FIRST so the lock is released deterministically before requesting
        # its replacement.
        self._writer = None
        del writer
        # wait_merging_threads() consumes the writer; re-create it so
        # subsequent add_document() calls remain valid.
        assert self._index is not None
        self._writer = self._index.writer(self._heap_size)

    def commit(self) -> None:
        """
        Commit pending documents to the index (atomic operation).

        Raises:
            RuntimeError: If writer is not initialized
        """
        if self._writer is None:
            raise RuntimeError("Index writer not initialized")

        try:
            with self._lock:
                self._commit_inner()
            logger.info("Committed documents to Tantivy index")
        except Exception as e:
            logger.error(f"Failed to commit documents: {e}")
            raise

    def rollback(self) -> None:
        """
        Rollback uncommitted changes.

        Raises:
            RuntimeError: If writer is not initialized
        """
        if self._writer is None:
            raise RuntimeError("Index writer not initialized")

        try:
            self._writer.rollback()
            logger.info("Rolled back uncommitted changes")
        except Exception as e:
            logger.error(f"Failed to rollback changes: {e}")
            raise

    def get_document_count(self) -> int:
        """
        Get the number of documents in the index.

        Returns:
            Number of documents

        Raises:
            RuntimeError: If index is not initialized
        """
        if self._index is None:
            raise RuntimeError("Index not initialized")

        try:
            # Reload index to get latest count
            self._index.reload()
            searcher = self._index.searcher()
            # num_docs is a property, not a method
            return cast(int, searcher.num_docs)
        except Exception as e:
            logger.error(f"Failed to get document count: {e}")
            return 0

    def get_all_indexed_paths(self) -> List[str]:
        """Return a list of all unique file paths indexed in this FTS index.

        Uses searcher.search() with an all-matching query to collect every
        stored 'path' field value. This API is compatible with tantivy-py v0.25.0
        which does NOT have segment_readers() on the Searcher object.

        Used by Bug #307 fix to determine which FTS documents need cleanup
        after a branch change CoW snapshot.

        Returns:
            List of unique file path strings currently in the index.
            Returns empty list if index is not initialized or empty.
        """
        if self._index is None:
            return []

        try:
            self._index.reload()
            searcher = self._index.searcher()
            total_docs = searcher.num_docs
            if total_docs == 0:
                return []

            # Build a match-all query using tantivy's Query class
            from tantivy import Query as TantivyQuery

            all_query = TantivyQuery.all_query()

            # Search with limit = total_docs to retrieve every document
            search_result = searcher.search(all_query, total_docs)
            hits = search_result.hits

            paths: set = set()
            for _score, doc_address in hits:
                try:
                    doc = searcher.doc(doc_address)
                    path_values = doc.get_all("path")
                    for path_value in path_values:
                        if path_value:
                            paths.add(str(path_value))
                except Exception:
                    continue  # Skip docs that can't be read
            return list(paths)
        except Exception as e:
            logger.warning(f"Failed to get all indexed paths: {e}")
            return []

    def _build_search_query(
        self,
        query_text: str,
        search_field: str,
        edit_distance: int,
        tantivy: Any,
        TantivyQuery: Any,
    ) -> Any:
        """
        Build Tantivy search query with proper AND semantics for multi-word queries.

        Query Building Strategy:
        - Single-term queries: Use existing behavior (backward compatibility)
        - Multi-word exact queries: Require ALL terms to match (AND semantics)
        - Multi-word fuzzy queries: Apply fuzzy matching to each term, combine with AND

        Args:
            query_text: Search query string
            search_field: Field to search ("content" or "content_raw")
            edit_distance: Fuzzy matching tolerance (0 for exact)
            tantivy: Tantivy module instance
            TantivyQuery: Tantivy Query class

        Returns:
            Tantivy query object, or None when the sanitized query has no terms
            (the caller returns no results).

        Raises:
            RuntimeError: If index is not initialized
            ValueError: If Tantivy rejects the query, or any one term of a
                multi-word exact query (every term is required)
        """
        if self._index is None:
            raise RuntimeError("Index not initialized")

        # Sanitize query to prevent ValueError from unmatched double-quotes
        query_text = sanitize_fts_query(query_text)

        # Split query into terms to detect single vs multi-word queries
        query_terms = query_text.split()
        if not query_terms:
            # Nothing left to search (e.g. the query was just '-'). Building
            # a query here would parse an empty string, or fuzzy-match an empty
            # term against every short token.
            return None
        is_multi_word = len(query_terms) > 1

        # Detect boolean operators: only route if multi-word and has valid boolean ops
        has_boolean_ops = is_multi_word and _contains_valid_boolean_ops(query_text)

        if edit_distance > 0:
            # FUZZY MATCHING: Apply fuzzy matching to each term independently
            if has_boolean_ops:
                # Boolean + fuzzy incompatible: strip operators, fuzzy-match remaining terms
                logger.warning(
                    "Boolean operators ignored in fuzzy mode: %s", query_text
                )
                non_op_terms = [t for t in query_terms if t not in _BOOL_OPS]
                fuzzy_queries = [
                    TantivyQuery.fuzzy_term_query(
                        self._schema,
                        search_field,
                        term,
                        distance=edit_distance,
                        transposition_cost_one=True,
                    )
                    for term in non_op_terms
                ]
                subqueries = [(tantivy.Occur.Must, q) for q in fuzzy_queries]
                return TantivyQuery.boolean_query(subqueries)
            elif is_multi_word:
                # Multi-word fuzzy query: Apply fuzzy matching to each term, combine with AND
                # Example: "gloc pattern" with edit_distance=1 fuzzy-matches "glob" AND "pattern"
                fuzzy_queries = [
                    TantivyQuery.fuzzy_term_query(
                        self._schema,
                        search_field,
                        term,
                        distance=edit_distance,
                        transposition_cost_one=True,
                    )
                    for term in query_terms
                ]

                # Combine all fuzzy queries with AND semantics (all terms must fuzzy-match)
                subqueries = [(tantivy.Occur.Must, q) for q in fuzzy_queries]
                return TantivyQuery.boolean_query(subqueries)
            else:
                # Single-term fuzzy query: Use fuzzy_term_query directly (backward compatibility)
                return TantivyQuery.fuzzy_term_query(
                    self._schema,
                    search_field,
                    query_text,
                    distance=edit_distance,
                    transposition_cost_one=True,
                )
        else:
            # EXACT MATCHING: Require ALL terms to match
            if has_boolean_ops:
                # Boolean query: pass entire query to Tantivy's parse_query() which natively
                # supports OR, AND, NOT operators. sanitize_fts_query() already ensured validity.
                return self._index.parse_query(
                    query_text, [search_field, "identifiers"]
                )
            elif is_multi_word:
                # Multi-word exact query: Require ALL terms to exist (AND semantics)
                # Example: "gloc pattern" returns 0 results if "gloc" doesn't exist
                # Every term is required, so a term Tantivy rejects (e.g. '^')
                # fails the whole query: dropping it would silently broaden the
                # result. search() turns this into one WARNING and no results.
                term_queries = []
                for term in query_terms:
                    try:
                        term_queries.append(
                            self._index.parse_query(
                                sanitize_fts_query(term), [search_field, "identifiers"]
                            )
                        )
                    except ValueError as e:
                        raise ValueError(f"unparseable FTS term {term!r}: {e}") from e

                # Combine all term queries with AND semantics (all terms must match)
                subqueries = [(tantivy.Occur.Must, q) for q in term_queries]
                return TantivyQuery.boolean_query(subqueries)
            else:
                # Single-term exact query: Use standard query parser (backward compatibility)
                return self._index.parse_query(
                    query_text, [search_field, "identifiers"]
                )

    def _regex_verbatim_field_available(self) -> bool:
        """
        Bug #1497: check whether the PHYSICAL on-disk index actually has
        _REGEX_VERBATIM_FIELD in its schema.

        Legacy indexes built before this fix lack this field. Querying a
        field the physical index doesn't have panics at the Rust FFI
        boundary (pyo3_runtime.PanicException: "index out of bounds") --
        proven empirically. This check reads meta.json directly (pure JSON
        parsing, no tantivy API calls), so detection itself can never panic.

        Cached after the first call since an open index's on-disk schema is
        fixed for the lifetime of this manager instance.

        Returns:
            True if regex search may safely target _REGEX_VERBATIM_FIELD.
        """
        if self._verbatim_field_available is not None:
            return self._verbatim_field_available

        meta_path = self.index_dir / "meta.json"
        try:
            with open(meta_path, "r") as f:
                meta = json.load(f)
            field_names = {field.get("name") for field in meta.get("schema", [])}
            self._verbatim_field_available = _REGEX_VERBATIM_FIELD in field_names
        except Exception as e:
            logger.warning(
                "Could not determine verbatim regex field availability "
                "from %s (falling back to legacy regex matching): %s",
                meta_path,
                e,
            )
            self._verbatim_field_available = False

        return self._verbatim_field_available

    def _path_exact_field_available(self) -> bool:
        """
        Bug #1761: check whether the PHYSICAL on-disk index actually has
        _PATH_EXACT_FIELD in its schema (module constant defined at top of
        file; self._exact_path_field_available is the cache attribute,
        already initialized in __init__).

        Legacy indexes built before this fix lack this field. Querying a
        field the physical index doesn't have panics at the Rust FFI
        boundary (see _regex_verbatim_field_available() immediately above
        for the identical, already-proven Bug #1497 precedent this method
        mirrors). This check reads meta.json directly (pure JSON parsing,
        no tantivy API calls, using the `json` module already imported at
        the top of this file), so detection itself can never panic.

        Cached (in self._exact_path_field_available) after the first call
        since an open index's on-disk schema is fixed for the lifetime of
        this manager instance.

        Returns:
            True if delete/update may safely target _PATH_EXACT_FIELD for
            an exact-match query.
        """
        if self._exact_path_field_available is not None:
            return self._exact_path_field_available

        meta_path = self.index_dir / "meta.json"
        try:
            with open(meta_path, "r") as f:
                meta = json.load(f)
            field_names = {field.get("name") for field in meta.get("schema", [])}
            self._exact_path_field_available = _PATH_EXACT_FIELD in field_names
        except Exception as e:
            logger.warning(
                "Could not determine exact path field availability "
                "from %s (falling back to legacy tokenized path matching): %s",
                meta_path,
                e,
            )
            self._exact_path_field_available = False

        return self._exact_path_field_available

    def schema_needs_rebuild(self) -> bool:
        """
        Bug #1763: True if an EXISTING on-disk index at self.index_dir has
        a stale (pre-#1761) physical schema and must be rebuilt via
        initialize_index(create_new=True) before reuse -- otherwise
        #1761's dedup fix (delete_document_deferred(), gated on
        _path_exact_field_available()) stays a permanent no-op on this
        index and duplicate FTS rows keep accumulating forever.

        Pure meta.json read (no tantivy API calls, so it can never
        panic), safely callable BEFORE initialize_index(). Deliberately
        uncached and independent of self._exact_path_field_available:
        a caller that finds the schema stale is about to trigger a real
        rebuild on this same instance, and caching "legacy" here would
        leave a stale answer baked in for this instance's lifetime even
        after the rebuild adds the field.

        Returns:
            True when meta.json exists and can be parsed but the schema
            lacks _PATH_EXACT_FIELD.
            False when no physical index exists yet (callers already
            have their own "doesn't exist" branch), the schema is
            already current, OR meta.json exists but can't be read/
            parsed (MEDIUM-3, #1763 code review: fail toward the
            CONSERVATIVE answer -- same fail-safe direction as the
            sibling _path_exact_field_available() method above -- so a
            transient read glitch (OSError, PermissionError, a partial
            read mid-write) can never trigger a destructive rmtree. A
            genuinely legacy index that happens to hit a transient read
            error here simply gets re-detected as stale on the NEXT
            call once the transient condition clears; the reverse
            (fail toward True) risked deleting a perfectly healthy
            index on a one-off filesystem hiccup).
        """
        meta_path = self.index_dir / "meta.json"
        if not meta_path.exists():
            return False

        try:
            with open(meta_path, "r") as f:
                meta = json.load(f)
            field_names = {field.get("name") for field in meta.get("schema", [])}
        except Exception as e:
            logger.warning(
                "Could not determine FTS schema compatibility from %s "
                "(assuming current schema -- conservative, never "
                "triggers a destructive rebuild from a transient read "
                "error): %s",
                meta_path,
                e,
            )
            return False

        return _PATH_EXACT_FIELD not in field_names

    def _build_path_delete_query(self, file_path: str) -> Any:
        """
        Bug #1761: build a Tantivy Query (return type Any -- Query is not
        importable at module scope here per this file's established lazy
        `from tantivy import Query as TantivyQuery` convention, used
        identically elsewhere in this same file) that matches ALL FTS
        documents for exactly `file_path`, for use with
        delete_documents_by_query() in delete_document()/update_document().

        Prefers an EXACT term_query against the untokenized
        _PATH_EXACT_FIELD when the physical index has it. Falls back to the
        legacy tokenized `index.parse_query(file_path, ["path"])` for
        indexes built before this fix -- preserves pre-fix behavior exactly
        rather than crashing on a missing field.

        Note: the legacy fallback is a TOKEN match on the "path" field's
        "default" tokenizer, not an exact match -- it can over-match a
        different file whose path tokens are a contiguous prefix of another
        file's path tokens (verified empirically, e.g. deleting
        "src/foo/bar.py" while "src/foo/bar.py.bak" also exists deletes
        both). This is accepted only as a legacy-index compatibility path;
        every index built after this fix uses the exact match above.
        """
        assert self._index is not None, (
            "Index must be initialized when writer is initialized"
        )
        assert self._schema is not None, "Schema must be initialized"

        if self._path_exact_field_available():
            from tantivy import Query as TantivyQuery

            return TantivyQuery.term_query(self._schema, _PATH_EXACT_FIELD, file_path)

        return self._index.parse_query(file_path, ["path"])

    def _wrap_regex_for_verbatim_match(self, pattern: str, case_sensitive: bool) -> str:
        """
        Bug #1497: wrap a user regex pattern for substring (grep-like) matching
        against the untokenized _REGEX_VERBATIM_FIELD term.

        RegexQuery requires a FULL match of the entire term against the
        pattern (proven empirically), so a bare user pattern like
        "cancel_job" never matches a term equal to an entire document's raw
        content. Wrapping with [\\s\\S]* on both sides (rather than a global
        (?s) DOTALL flag) lets the wrapper span newlines while leaving the
        user's own pattern semantics (e.g. their own ".") untouched -- proven
        via Tantivy's DFA regex engine to keep linear-time matching for any
        pattern.

        Args:
            pattern: The user-supplied regex pattern (unwrapped).
            case_sensitive: When False, prefixes an inline (?i) flag so
                Tantivy's own engine performs case folding (never the
                user's pattern text itself, which could corrupt
                case-sensitive escape sequences like \\S).

        Returns:
            The wrapped pattern string, ready for TantivyQuery.regex_query().
        """
        prefix = "" if case_sensitive else "(?i)"
        return f"{prefix}[\\s\\S]*(?:{pattern})[\\s\\S]*"

    def _build_legacy_regex_query(
        self, query_text: str, search_field: str, TantivyQuery: Any
    ) -> Any:
        """
        Bug #1497 (follow-up): build a RegexQuery against a TOKENIZED field
        using the bare (unwrapped) user pattern -- the exact pre-#1497
        matching behavior.

        Used in two situations:
          1. The physical index lacks _REGEX_VERBATIM_FIELD (legacy index
             built before Bug #1497's fix).
          2. The verbatim-wrapped pattern exceeds Tantivy's fixed
             1000-state DFA limit (see _is_regex_state_limit_error()) --
             graceful degradation to legacy token-level matching for just
             this one search, rather than failing the whole query.

        Args:
            query_text: The user-supplied regex pattern (unwrapped).
            search_field: Tokenized field to query ("content" or
                "content_raw").
            TantivyQuery: Tantivy Query class.

        Returns:
            Tantivy query object.

        Raises:
            ValueError: If the pattern itself fails to compile (a genuine
                syntax error), wrapped with a clear message.
        """
        try:
            return TantivyQuery.regex_query(self._schema, search_field, query_text)
        except Exception as e:
            raise ValueError(f"Invalid regex pattern '{query_text}': {str(e)}") from e

    def search(
        self,
        query_text: str,
        case_sensitive: bool = False,
        edit_distance: int = 0,
        snippet_lines: int = 5,
        limit: int = 10,
        language_filter: Optional[str] = None,  # Deprecated: use languages
        languages: Optional[List[str]] = None,
        path_filters: Optional[List[str]] = None,
        exclude_paths: Optional[List[str]] = None,
        exclude_languages: Optional[List[str]] = None,
        path_filter: Optional[str] = None,  # Deprecated: use path_filters
        query: Optional[str] = None,  # Backwards compatibility
        use_regex: bool = False,  # NEW: Enable regex pattern matching
        file_extensions: Optional[List[str]] = None,
        deadline: Optional[float] = None,
        allowed_paths: Optional[Collection[str]] = None,
        completeness_out: Optional[List[bool]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search the FTS index with configurable options.

        Args:
            query_text: Search query string (preferred parameter name)
            case_sensitive: Enable case-sensitive matching (default: False)
            edit_distance: Fuzzy matching tolerance (0-3, default: 0)
            snippet_lines: Context lines to include in snippet (0 for list only, default: 5)
            limit: Maximum number of results (default: 10, use 0 for unlimited grep-like output).
                No search asks Tantivy for more than _FTS_UNLIMITED_HITS hits, so a
                larger limit returns at most that many results.
            language_filter: Filter by single programming language (deprecated, use languages)
            languages: Filter by multiple programming languages (e.g., ["py", "js"])
            path_filters: Filter by path patterns (e.g., ["*/tests/*", "*/src/*"]) - OR logic
            exclude_paths: Exclude paths matching patterns (e.g., ["*/tests/*", "*.min.js"]) - OR logic, takes precedence
            exclude_languages: Exclude programming languages (e.g., ["javascript", "typescript"]) - OR logic, takes precedence over languages
            path_filter: Filter by single path pattern (deprecated, use path_filters)
            query: Backwards compatibility parameter (deprecated, use query_text)
            use_regex: Interpret query_text as regex pattern (incompatible with edit_distance > 0)
            file_extensions: Keep only files with one of these extensions
                (#2047 rule, see services/extension_filter.py:
                case-insensitive, leading dot optional, OR-ed, extensionless
                files never match), intersected with the language filters.
                When hits fail the suffix re-check, further pages are fetched
                (each hit inspected once) up to _FTS_FILL_HITS_PER_RESULT x
                limit inspected hits; if that bound or ``deadline`` stops the
                search with fewer than ``limit`` results, it is logged at INFO.
            deadline: A time.monotonic() reading (the request's time budget,
                shared with its other repositories): no further extension
                refetch page is fetched once it has passed. The first page
                always runs.
            allowed_paths: Only documents stored under one of these paths
                may match (an exact term-set restriction on the exact-path
                field when the index has it; every hit is re-checked).
            completeness_out: Receives one bool: True when every document
                matching the query was inspected (the result is complete for
                its filters), False when the limit cut matches off.

        Returns:
            List of dictionaries with keys:
                - path: File path
                - line: Line number where match occurs
                - column: Column number where match occurs
                - match_text: The matched text
                - snippet: Code snippet with context (empty if snippet_lines=0)
                - language: Programming language
                - score: Relevance score (if available)

        Raises:
            RuntimeError: If index is not initialized
            ValueError: If edit_distance is out of range (0-3) or use_regex combined with edit_distance
        """
        # Handle backwards compatibility
        if query is not None and not query_text:
            query_text = query

        # Handle path filtering: path_filters takes precedence over path_filter
        active_path_filters: Optional[List[str]] = None
        if path_filters is not None:
            active_path_filters = path_filters if path_filters else None
        elif path_filter is not None:
            active_path_filters = [path_filter]

        # Complete unless matches are left uninspected (set at the end).
        if completeness_out is not None:
            completeness_out.append(True)
        allowed_set = None if allowed_paths is None else frozenset(allowed_paths)

        # Handle language filtering: languages takes precedence over language_filter
        active_language_filter: Optional[List[str]] = None
        if languages is not None:
            active_language_filter = languages if languages else None
        elif language_filter is not None:
            active_language_filter = [language_filter]

        from code_indexer.services.extension_filter import (
            fts_pushdown_terms,
            normalize_extensions,
        )

        extension_set = normalize_extensions(file_extensions)

        if self._index is None:
            raise RuntimeError("Index not initialized")

        # Validate regex incompatibility with fuzzy matching
        if use_regex and edit_distance > 0:
            raise ValueError(
                "Cannot combine regex matching with fuzzy matching (edit_distance > 0). "
                "Regex provides its own pattern matching capabilities."
            )

        # Validate edit_distance
        if not (0 <= edit_distance <= 3):
            raise ValueError(f"edit_distance must be 0-3, got {edit_distance}")

        try:
            # Import tantivy for query building
            import tantivy
            from tantivy import Query as TantivyQuery

            # Reload index to get latest documents
            self._index.reload()
            searcher = self._index.searcher()

            # Select field based on case sensitivity
            # "content_raw" preserves case, "content" is lowercased during indexing
            search_field = "content_raw" if case_sensitive else "content"

            # Build query based on regex flag
            # Tantivy regex queries run in linear time for any caller pattern.
            if use_regex:
                # Build regex query using Tantivy's regex_query
                assert self._schema is not None  # For mypy
                if self._regex_verbatim_field_available():
                    # Bug #1497 fix: match against the untokenized verbatim
                    # field (whole-document term) so substring/cross-token
                    # patterns like "cancel_job" or "def.*cancel_job" work,
                    # instead of full-term matching against a tokenized field
                    # whose terms never contain underscores/spaces.
                    wrapped_pattern = self._wrap_regex_for_verbatim_match(
                        query_text, case_sensitive
                    )
                    try:
                        text_query = TantivyQuery.regex_query(
                            self._schema,
                            _REGEX_VERBATIM_FIELD,
                            wrapped_pattern,
                        )
                    except Exception as e:
                        if _is_regex_state_limit_error(e):
                            # Bug #1497 (follow-up): some patterns -- e.g. a
                            # Unicode \w/\s class combined with the required
                            # [\s\S]* substring wrapping on both sides --
                            # compile to an automaton that exceeds Tantivy's
                            # fixed 1000-state DFA limit (proven empirically;
                            # there is no Python-exposed way to raise this
                            # limit, and anchors/lazy quantifiers are
                            # rejected outright by Tantivy's regex parser).
                            # Gracefully degrade to the legacy token-level
                            # match for just this one search instead of
                            # failing the whole query.
                            logger.warning(
                                "Regex pattern '%s' exceeds Tantivy's "
                                "verbatim-field state limit; falling back "
                                "to legacy token-level regex matching: %s",
                                query_text,
                                e,
                            )
                            text_query = self._build_legacy_regex_query(
                                query_text, search_field, TantivyQuery
                            )
                        else:
                            # Genuine regex syntax error -- wrap with clear message
                            raise ValueError(
                                f"Invalid regex pattern '{query_text}': {str(e)}"
                            ) from e
                else:
                    # Legacy on-disk index built before Bug #1497's fix --
                    # lacks _REGEX_VERBATIM_FIELD. Preserve the exact pre-fix
                    # token-based behavior (no crash; Bug #1497 remains
                    # present for this index until it is rebuilt).
                    text_query = self._build_legacy_regex_query(
                        query_text, search_field, TantivyQuery
                    )
            else:
                # Build query using existing helper method for non-regex searches
                # Defense-in-depth: catch ValueError from Tantivy's parse_query() for
                # any edge-case syntax that Phase 3 sanitization misses. Return empty
                # results rather than propagating an error burst across repositories.
                try:
                    text_query = self._build_search_query(
                        query_text=query_text,
                        search_field=search_field,
                        edit_distance=edit_distance,
                        tantivy=tantivy,
                        TantivyQuery=TantivyQuery,
                    )
                except ValueError as e:
                    logger.warning(
                        "FTS query parse error (returning empty results): %s", e
                    )
                    return []
                if text_query is None:
                    # The query sanitized to nothing (e.g. just '-').
                    return []

            # Add language filter to query if specified AND no exclusions present
            # If exclusions present, we do post-processing for correct precedence
            if active_language_filter and not exclude_languages:
                # Build language facet queries (OR semantics: match any specified language).
                # The facet holds the stored suffix ("/py"), so a language name
                # ("python") is expanded to its extensions; an unknown name or a
                # bare extension maps to itself.
                from tantivy import Facet
                from code_indexer.services.language_mapper import LanguageMapper

                assert self._schema is not None  # For mypy
                facet_mapper = LanguageMapper()
                language_queries = [
                    TantivyQuery.term_query(
                        self._schema, "language_facet", Facet.from_string(f"/{ext}")
                    )
                    for lang in active_language_filter
                    for ext in sorted(facet_mapper.get_extensions(lang))
                ]

                # Combine language queries with OR semantics (any language matches)
                if len(language_queries) == 1:
                    language_query = language_queries[0]
                else:
                    language_subqueries = [
                        (tantivy.Occur.Should, q) for q in language_queries
                    ]
                    language_query = TantivyQuery.boolean_query(language_subqueries)

                # Combine text query AND language filter (both must match)
                tantivy_query = TantivyQuery.boolean_query(
                    [
                        (tantivy.Occur.Must, text_query),
                        (tantivy.Occur.Must, language_query),
                    ]
                )
            else:
                tantivy_query = text_query

            # #2047: push the extension filter down as an OR group of terms on
            # the tokenized `language` field (lowercased by the default
            # tokenizer; present in every schema version). Wrapped in a zero
            # constant score so it filters without changing the ranking.
            # Each hit is still post-verified below (extensionless files are
            # stored as "txt").
            pushdown_terms = (
                fts_pushdown_terms(extension_set) if extension_set else None
            )
            if pushdown_terms:
                assert self._schema is not None  # For mypy
                extension_query = TantivyQuery.boolean_query(
                    [
                        (
                            tantivy.Occur.Should,
                            TantivyQuery.term_query(self._schema, "language", term),
                        )
                        for term in pushdown_terms
                    ]
                )
                tantivy_query = TantivyQuery.boolean_query(
                    [
                        (tantivy.Occur.Must, tantivy_query),
                        (
                            tantivy.Occur.Must,
                            TantivyQuery.const_score_query(extension_query, 0.0),
                        ),
                    ]
                )

            # Only the allowed paths may match: pushed down (zero score)
            # when the index has the exact-path field, re-checked per hit.
            if allowed_set is not None and not allowed_set:
                return []
            if allowed_set is not None and self._path_exact_field_available():
                assert self._schema is not None  # For mypy
                path_query = TantivyQuery.term_set_query(
                    self._schema, _PATH_EXACT_FIELD, sorted(allowed_set)
                )
                tantivy_query = TantivyQuery.boolean_query(
                    [
                        (tantivy.Occur.Must, tantivy_query),
                        (
                            tantivy.Occur.Must,
                            TantivyQuery.const_score_query(path_query, 0.0),
                        ),
                    ]
                )

            # Handle limit=0 for unlimited results (grep-like output)
            # Tantivy requires limit > 0, so use very large limit and disable snippets
            if limit == 0:
                search_limit = _FTS_UNLIMITED_HITS  # Effectively unlimited
                snippet_lines = 0  # Disable snippets for grep-like output
            else:
                # Execute search with increased limit to account for filtering
                # If language exclusions present, we need higher limit for post-processing
                needs_increased_limit = (
                    active_path_filters
                    or exclude_paths
                    or exclude_languages
                    or (languages and exclude_languages)
                    or extension_set
                )
                # Never ask Tantivy for more than the cap (a larger limit
                # returns at most _FTS_UNLIMITED_HITS results).
                search_limit = min(
                    limit * 3 if needs_increased_limit else limit,
                    _FTS_UNLIMITED_HITS,
                )

            first_page = searcher.search(tantivy_query, search_limit)
            search_results = first_page.hits
            match_count = first_page.count

            # The per-hit filters, as one predicate shared with callers that
            # judge paths outside the engine (precedence in fts_hit_filter).
            from code_indexer.services.fts_hit_filter import build_hit_filter

            _passes_filters = build_hit_filter(
                allowed_paths=allowed_set,
                languages=languages,
                exclude_languages=exclude_languages,
                path_filters=active_path_filters,
                exclude_paths=exclude_paths,
                file_extensions=file_extensions,
            )

            # PERFORMANCE OPTIMIZATION: Compile regex pattern ONCE before loop (not per
            # result). This reduces 100x compilation overhead for searches with many
            # results.
            #
            # Bug #1497: this Python-level regex only extracts the highlight;
            # Tantivy made the match decision, and the extraction shares one
            # bounded evaluation budget per search.
            compiled_regex_pattern: Optional[BoundedPattern] = None
            # Story #1494 AC2 (Finding A4): count of candidates that
            # actually received the real (GIL-held) Python-level extraction
            # step; once this reaches _MAX_REGEX_EXTRACTION_CANDIDATES,
            # remaining candidates fall back to the sentinel position
            # instead of running the expensive search() call -- bounding
            # worst-case work on an unbounded (limit=0) regex query.
            # `regex_extraction_cap_warned` ensures the WARNING below is
            # logged exactly once per search() call, not once per skipped
            # candidate.
            regex_extraction_evaluated_count = 0
            regex_extraction_cap_warned = False
            if use_regex:
                # One total extraction budget per search, each call capped.
                try:
                    compiled_regex_pattern = BoundedPattern(
                        query_text,
                        0 if case_sensitive else re.IGNORECASE,
                        purpose="full-text highlight",
                        call_seconds=_REGEX_EXTRACTION_TIMEOUT_SECONDS,
                    )
                except re.error as e:
                    # Regex compilation failed - raise early before processing results
                    error_msg = f"Invalid regex pattern '{query_text}': {str(e)}"
                    logger.error(error_msg)
                    raise ValueError(error_msg) from e

            # Non-regex snippet location candidates, most precise first: the raw
            # query ('HashMap::new' on its own line), the text actually searched
            # ('--verbose' searched 'verbose'), then each one's first word.
            searched_text = sanitize_fts_query(query_text)
            snippet_candidates = [query_text, searched_text] + [
                words[0]
                for words in (query_text.split(), searched_text.split())
                if words
            ]

            def _path_and_language(doc: Any) -> Tuple[str, Optional[str]]:
                language = doc.get_first("language")
                # Parse language from facet format (/language_name)
                return (
                    doc.get_first("path") or "",
                    str(language).strip("/") if language else language,
                )

            # #2047: hits that fail the suffix re-check (extensionless "txt"
            # files, or values that cannot be pushed down) must not leave the
            # answer short. Fetch the NEXT page only (offset paging, so no hit
            # is inspected twice; the total doubles per page) until `limit`
            # hits pass, Tantivy has no more matches, the per-repository bound
            # of _FTS_FILL_HITS_PER_RESULT x limit inspected hits is reached,
            # or the request's `deadline` has passed. Every inspected hit's
            # stored document is kept (None when it fails the filters) and
            # reused by the result loop, so no document is fetched twice.
            # Bound: at most bit_length(_FTS_UNLIMITED_HITS) + 1 pages.
            inspected: Dict[Tuple[int, int], Any] = {}
            if extension_set and limit > 0:
                search_results = list(search_results)
                fill_bound = min(limit * _FTS_FILL_HITS_PER_RESULT, _FTS_UNLIMITED_HITS)
                page, requested = search_results, search_limit
                passing = 0
                stopped_by: Optional[str] = None
                for _ in range(_FTS_UNLIMITED_HITS.bit_length() + 1):
                    for _score, hit in page:
                        hit_doc = searcher.doc(hit)
                        kept_doc: Optional[Any] = None
                        if _passes_filters(*_path_and_language(hit_doc)):
                            passing += 1
                            kept_doc = hit_doc
                        inspected[(hit.segment_ord, hit.doc)] = kept_doc
                    if passing >= limit or len(page) < requested:
                        break  # filled, or Tantivy has no more matches
                    if len(search_results) >= fill_bound:
                        stopped_by = "per-repository hit bound"
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        stopped_by = "search time budget"
                        break
                    requested = min(
                        len(search_results), fill_bound - len(search_results)
                    )
                    page = searcher.search(
                        tantivy_query,
                        requested,
                        count=False,
                        offset=len(search_results),
                    ).hits
                    search_results.extend(page)
                if stopped_by is not None:
                    logger.info(
                        "FTS file_extensions filter stopped at the %s after "
                        "inspecting %d hits; returning %d of %d requested "
                        "results (%s)",
                        stopped_by,
                        len(search_results),
                        passing,
                        limit,
                        self.index_dir,
                    )

            # Process results
            docs = []
            stopped_at_limit = False
            for position, (score, address) in enumerate(search_results):
                hit_key = (address.segment_ord, address.doc)
                if hit_key in inspected:
                    doc = inspected[hit_key]
                    if doc is None:
                        continue  # already rejected by the filters
                else:
                    doc = searcher.doc(address)

                # Extract fields
                path, language = _path_and_language(doc)
                content_raw = doc.get_first("content_raw") or ""
                line_start = doc.get_first("line_start")

                if not _passes_filters(path, language):
                    continue

                # Find match position in content. Tantivy's DFA engine already
                # decided the hit; the Python extraction below only recovers the
                # highlight, bounded per call and per search (BoundedPattern).
                # A candidate that ran out of time, or past the candidate cap
                # (Story #1494 AC2), keeps its hit without a highlight and is
                # marked regex_extraction_capped (never silently dropped).
                regex_extraction_capped = False
                match_obj: Optional[Any] = None
                if use_regex and compiled_regex_pattern is not None:
                    if compiled_regex_pattern.exhausted:
                        regex_extraction_capped = True
                    elif (
                        regex_extraction_evaluated_count
                        < _MAX_REGEX_EXTRACTION_CANDIDATES
                    ):
                        regex_extraction_evaluated_count += 1
                        try:
                            match_obj = compiled_regex_pattern.search(content_raw)
                        except PatternBudgetExhausted:
                            # BoundedPattern itself logs one WARNING per search.
                            regex_extraction_capped = True
                    else:
                        regex_extraction_capped = True
                        if not regex_extraction_cap_warned:
                            logger.warning(
                                "Regex extraction candidate cap (%d) reached; "
                                "remaining candidates use a sentinel match position "
                                "and are marked regex_extraction_capped.",
                                _MAX_REGEX_EXTRACTION_CANDIDATES,
                            )
                            regex_extraction_cap_warned = True
                    if match_obj is not None:
                        match_text = match_obj.group(0)
                        match_start = match_obj.start()
                        if len(match_text) == 0:
                            logger.warning(
                                "Regex pattern produced a zero-length match in %s "
                                "at line %s; consider a more specific pattern.",
                                path,
                                line_start,
                            )
                    else:
                        # No highlight: skipped, out of time, or (unexpected)
                        # no Python-level match for a Tantivy hit.
                        match_text = query_text
                        match_start = _NO_MATCH_POSITION
                else:
                    # Non-regex search: literal string matching, most precise
                    # candidate first (see snippet_candidates).
                    match_text = query_text
                    match_start = -1
                    haystack = content_raw if case_sensitive else content_raw.lower()
                    for candidate in snippet_candidates:
                        needle = candidate if case_sensitive else candidate.lower()
                        match_start = haystack.find(needle)
                        if match_start != -1:
                            match_text = candidate
                            break

                    # If still not found and fuzzy search is enabled, use fuzzy matching
                    if match_start == -1 and edit_distance > 0:
                        fuzzy_start, fuzzy_text = self._find_fuzzy_match(
                            content_raw, searched_text, case_sensitive
                        )
                        if fuzzy_start >= 0:
                            match_start = fuzzy_start
                            match_text = fuzzy_text

                # Extract snippet and calculate line/column
                if match_start >= 0:
                    snippet, line, column, snippet_start_line = self._extract_snippet(
                        content_raw, match_start, len(match_text), snippet_lines
                    )
                else:
                    # Fallback: use line_start from document
                    snippet = ""
                    line = int(line_start) if line_start is not None else 1
                    column = 1
                    snippet_start_line = line

                result = {
                    "path": path,
                    "line": line,
                    "column": column,
                    "match_text": match_text,
                    "snippet": snippet if snippet_lines > 0 else "",
                    "snippet_start_line": snippet_start_line,
                    "language": language or "unknown",
                    "score": score,
                }
                # Story #1494 AC2: never silently drop a capped candidate --
                # surface the fact its regex extraction was skipped instead
                # of omitting the field (or the result) entirely.
                if regex_extraction_capped:
                    result["regex_extraction_capped"] = True

                docs.append(result)

                # Enforce limit after path filtering (unless limit=0 for unlimited)
                if limit > 0 and len(docs) >= limit:
                    stopped_at_limit = position < len(search_results) - 1
                    break

            if completeness_out is not None:
                fetched_all = match_count is None or match_count <= len(search_results)
                completeness_out[-1] = fetched_all and not stopped_at_limit
            # Return results (slice only if limit > 0)
            return docs if limit == 0 else docs[:limit]

        except ValueError:
            # Re-raise ValueError (includes invalid regex patterns and edit_distance validation)
            # These should not be silently caught
            raise
        except Exception as e:
            logger.error(f"Search failed: {e}")
            return []

    def _find_fuzzy_match(
        self, content: str, query_text: str, case_sensitive: bool = False
    ) -> tuple[int, str]:
        """
        Find the best fuzzy match location in content using approximate string matching.

        This method uses difflib to find the closest matching substring in the content
        when exact matching fails (e.g., for typos in fuzzy search).

        Args:
            content: Content to search in
            query_text: Text to search for (may have typos)
            case_sensitive: Whether to use case-sensitive matching

        Returns:
            Tuple of (match_start_position, actual_matched_text)
            Returns (-1, "") if no reasonable match found
        """
        from difflib import SequenceMatcher

        # Prepare search content and query
        search_content = content if case_sensitive else content.lower()
        search_query = query_text if case_sensitive else query_text.lower()

        # Split query into words for better matching
        query_words = search_query.split()
        if not query_words:
            return -1, ""

        # Try to find matches for each word and combinations
        best_match_start = -1
        best_match_text = ""
        best_ratio = 0.0

        # Generate sliding windows of content to compare against query
        query_len = len(search_query)
        # Allow some flexibility in match length (±30%)
        min_window = max(1, int(query_len * 0.7))
        max_window = int(query_len * 1.3)

        # Search for best matching substring
        for window_size in range(min_window, max_window + 1):
            for i in range(len(search_content) - window_size + 1):
                window = search_content[i : i + window_size]
                ratio = SequenceMatcher(None, search_query, window).ratio()

                if ratio > best_ratio:
                    best_ratio = ratio
                    best_match_start = i
                    best_match_text = content[i : i + window_size]  # Use original case

        # Only return match if similarity is reasonably high (>0.6 threshold)
        if best_ratio >= 0.6 and best_match_start >= 0:
            return best_match_start, best_match_text

        # Fallback: try to find the first word of query
        if query_words:
            first_word = query_words[0]
            # Use fuzzy matching for first word alone
            word_len = len(first_word)
            min_word_window = max(1, int(word_len * 0.7))
            max_word_window = int(word_len * 1.3)

            for window_size in range(min_word_window, max_word_window + 1):
                for i in range(len(search_content) - window_size + 1):
                    window = search_content[i : i + window_size]
                    ratio = SequenceMatcher(None, first_word, window).ratio()

                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_match_start = i
                        best_match_text = content[i : i + window_size]

            if best_ratio >= 0.6 and best_match_start >= 0:
                return best_match_start, best_match_text

        return -1, ""

    def _extract_snippet(
        self, content: str, match_start: int, match_len: int, snippet_lines: int
    ) -> tuple[str, int, int, int]:
        """
        Extract code snippet with context lines and calculate line/column position.

        Args:
            content: Full content string
            match_start: Character offset where match starts (NOT byte offset)
            match_len: Length of match in characters
            snippet_lines: Number of context lines before/after match

        Returns:
            Tuple of (snippet_text, line_number, column_number, snippet_start_line)

        CRITICAL: Uses CHARACTER offsets, not byte offsets, for correct Unicode handling.
        Python's match.start() returns character position, so we must use character lengths.
        """
        lines = content.split("\n")

        # Calculate line and column from CHARACTER offset (not bytes)
        # CRITICAL: Use len(line) not len(line.encode("utf-8"))
        # Python regex match.start() returns character positions, not byte positions
        current_pos = 0
        line_number = 1
        column = 1

        for line_idx, line in enumerate(lines):
            line_len = len(line)  # Character length (NOT bytes)
            if current_pos <= match_start < current_pos + line_len:
                line_number = line_idx + 1
                # Calculate column (1-indexed, character position)
                column = match_start - current_pos + 1
                break
            current_pos += line_len + 1  # +1 for newline character

        # If snippet_lines=0, return empty snippet but still return line/column
        if snippet_lines == 0:
            return "", line_number, column, line_number

        # Extract surrounding lines
        line_idx = line_number - 1  # Convert to 0-indexed
        start_line = max(0, line_idx - snippet_lines)
        end_line = min(len(lines), line_idx + snippet_lines + 1)

        snippet_lines_list = lines[start_line:end_line]
        snippet = "\n".join(snippet_lines_list)

        # Return snippet with absolute line number where snippet starts (1-indexed)
        snippet_start_line = start_line + 1

        return snippet, line_number, column, snippet_start_line

    def get_metadata(self) -> Dict[str, Any]:
        """
        Get index metadata.

        Returns:
            Dictionary with index metadata
        """
        if self._metadata_file.exists():
            try:
                with open(self._metadata_file, "r") as f:
                    metadata: Dict[str, Any] = json.load(f)
                    return metadata
            except Exception as e:
                logger.error(f"Failed to load metadata: {e}")

        # Return default metadata
        return {
            "fts_enabled": True,
            "fts_index_available": self._index is not None,
            "tantivy_version": "0.25.0",
            "schema_version": "1.0",
            "created_at": datetime.now().isoformat(),
            "index_path": str(self.index_dir),
        }

    def _save_metadata(self) -> None:
        """Save index metadata to disk."""
        metadata = {
            "fts_enabled": True,
            "fts_index_available": True,
            "tantivy_version": "0.25.0",
            "schema_version": "1.0",
            "created_at": datetime.now().isoformat(),
            "index_path": str(self.index_dir),
        }

        try:
            with open(self._metadata_file, "w") as f:
                json.dump(metadata, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save metadata: {e}")

    def update_document(self, file_path: str, doc: Dict[str, Any]) -> None:
        """
        Update a document in the index (atomic operation).

        If the document exists, it will be replaced. If it doesn't exist,
        it will be created. This is an atomic operation that commits immediately.

        Args:
            file_path: Path of the file to update
            doc: Document dictionary with required fields

        Raises:
            RuntimeError: If writer is not initialized
            ValueError: If required fields are missing
        """
        if self._writer is None:
            raise RuntimeError(
                "Index writer not initialized. Call initialize_index() first."
            )

        try:
            # DEBUG: Mark incremental update for manual testing
            total_docs = self.get_document_count()
            logger.info(
                f"⚡ INCREMENTAL FTS UPDATE: Adding/updating 1 document (total index: {total_docs})"
            )

            with self._lock:
                # Delete old version if it exists using query-based deletion (idempotent)
                assert self._index is not None, (
                    "Index must be initialized when writer is initialized"
                )
                delete_query = self._build_path_delete_query(file_path)
                self._writer.delete_documents_by_query(delete_query)

            # Add updated version (has its own lock)
            self.add_document(doc)

            # Commit atomically (5-50ms target): use _commit_inner() so that
            # wait_merging_threads() is called and the writer is re-created,
            # keeping segment count bounded (same guarantee as commit()).
            with self._lock:
                self._commit_inner()
            logger.debug(f"Updated document: {file_path}")

        except Exception as e:
            logger.error(f"Failed to update document {file_path}: {e}")
            raise

    def delete_document(self, file_path: str) -> None:
        """
        Delete a document from the index (atomic operation).

        This is an atomic operation that commits immediately. If the document
        doesn't exist, this is a no-op (idempotent).

        Args:
            file_path: Path of the file to delete

        Raises:
            RuntimeError: If writer is not initialized
        """
        if self._writer is None:
            raise RuntimeError(
                "Index writer not initialized. Call initialize_index() first."
            )

        # Bug #1764: on a legacy physical index, the fallback path query
        # is TOKENIZED, not exact, and can over-match a sibling document.
        # Skip rather than over-match, mirroring
        # delete_document_deferred()'s existing safety behavior.
        if not self._path_exact_field_available():
            logger.debug(
                "Skipping FTS delete for %s: physical index lacks %s "
                "(legacy pre-#1761 schema)",
                file_path,
                _PATH_EXACT_FIELD,
            )
            return

        try:
            with self._lock:
                # Delete document using query-based deletion (idempotent)
                assert self._index is not None, (
                    "Index must be initialized when writer is initialized"
                )
                delete_query = self._build_path_delete_query(file_path)
                self._writer.delete_documents_by_query(delete_query)

                # Commit atomically (5-50ms target): use _commit_inner() so that
                # wait_merging_threads() is called and the writer is re-created,
                # keeping segment count bounded (same guarantee as commit()).
                self._commit_inner()
            logger.debug(f"Deleted document: {file_path}")

        except Exception as e:
            logger.error(f"Failed to delete document {file_path}: {e}")
            raise

    def delete_document_deferred(self, file_path: str) -> bool:
        """Bug #1761 CRITICAL 1/2 fix: queue a delete-by-path on the
        writer WITHOUT committing (no _commit_inner()), and skip entirely
        on a legacy on-disk index lacking _PATH_EXACT_FIELD (see the
        module-level _PATH_EXACT_FIELD comment and delete_document()'s
        docstring for the full commit-cost and over-deletion rationale).

        Returns:
            True when the delete was queued; False when it was skipped
            because the index cannot delete by path (legacy schema) --
            Story #2120: a caller that must remove a document treats that
            as an unresolved deletion, never as done.

        Raises:
            RuntimeError: If writer is not initialized (propagates --
                a genuine wiring/lifecycle bug, not swallowed here).
            ValueError: If file_path is empty/None.
        """
        if not file_path:
            raise ValueError("file_path must be a non-empty string")

        if self._writer is None:
            raise RuntimeError(
                "Index writer not initialized. Call initialize_index() first."
            )

        if not self._path_exact_field_available():
            logger.debug(
                "Skipping deferred FTS delete for %s: physical index lacks "
                "%s (legacy pre-#1761 schema)",
                file_path,
                _PATH_EXACT_FIELD,
            )
            return False

        try:
            with self._lock:
                assert self._index is not None, (
                    "Index must be initialized when writer is initialized"
                )
                delete_query = self._build_path_delete_query(file_path)
                self._writer.delete_documents_by_query(delete_query)
            logger.debug(f"Deferred delete queued (uncommitted) for: {file_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to queue deferred delete for {file_path}: {e}")
            raise

    def rebuild_from_documents_background(
        self, collection_path: Path, documents: List[Dict[str, Any]]
    ) -> threading.Thread:
        """
        Rebuild Tantivy FTS index in background (non-blocking).

        Uses BackgroundIndexRebuilder for atomic swap pattern matching HNSW/ID
        indexes. This ensures queries continue during rebuild without blocking (AC3).

        Pattern:
            1. Acquire exclusive lock
            2. Cleanup orphaned .tmp directories
            3. Build new FTS index to tantivy_fts.tmp directory
            4. Atomic rename tantivy_fts.tmp → tantivy_fts
            5. Release lock

        Args:
            collection_path: Path to collection directory
            documents: List of document dictionaries with required FTS fields

        Returns:
            threading.Thread: Background rebuild thread (call .join() to wait)

        Note:
            Queries don't need locks - OS-level atomic rename guarantees they
            see either old or new index. This is the same pattern as HNSW/ID.
        """
        from ..storage.background_index_rebuilder import BackgroundIndexRebuilder

        def _build_fts_index_to_temp(temp_dir: Path) -> None:
            """Build Tantivy FTS index to temp directory."""
            # Create temp FTS manager
            temp_fts_manager = TantivyIndexManager(temp_dir)

            # Initialize new index in temp directory
            temp_fts_manager.initialize_index(create_new=True)

            # Add all documents
            for doc in documents:
                temp_fts_manager.add_document(doc)

            # Commit all documents
            temp_fts_manager.commit()

            # Close writer
            temp_fts_manager.close()

            logger.info(f"Built FTS index to temp directory: {temp_dir}")

        # Use BackgroundIndexRebuilder for atomic swap with locking
        rebuilder = BackgroundIndexRebuilder(collection_path)

        # FTS uses directory, not single file
        target_dir = collection_path / "tantivy_fts"
        temp_dir = Path(str(target_dir) + ".tmp")

        def rebuild_thread_fn():
            """Thread function for background rebuild."""
            try:
                with rebuilder.acquire_lock():
                    logger.info(f"Starting FTS background rebuild: {target_dir}")

                    # Cleanup orphaned .tmp directories (AC9)
                    removed_count = rebuilder.cleanup_orphaned_temp_files()
                    if removed_count > 0:
                        logger.info(
                            f"Cleaned up {removed_count} orphaned temp files before FTS rebuild"
                        )

                    # Build to temp directory
                    _build_fts_index_to_temp(temp_dir)

                    # Atomic swap (directory rename)
                    import shutil
                    import os

                    # Remove old target if exists
                    if target_dir.exists():
                        shutil.rmtree(target_dir)

                    # Atomic rename (directory)
                    os.rename(temp_dir, target_dir)

                    logger.info(f"Completed FTS background rebuild: {target_dir}")

            except Exception as e:
                logger.error(f"FTS background rebuild failed: {e}")
                # Cleanup temp directory on error
                if temp_dir.exists():
                    import shutil

                    shutil.rmtree(temp_dir)
                    logger.debug(f"Cleaned up temp directory after error: {temp_dir}")
                raise

        # Start background thread
        rebuild_thread = threading.Thread(target=rebuild_thread_fn, daemon=False)
        rebuild_thread.start()

        return rebuild_thread

    def set_cached_index(self, index: Any, schema: Any) -> None:
        """
        Set index and schema from external cache.

        Used by server-side caching to inject pre-loaded index
        without re-initializing from disk.

        Args:
            index: tantivy.Index instance from cache
            schema: tantivy.Schema instance from cache

        Note: Writer is NOT set - this is for read-only search operations only.
        """
        self._index = index
        self._schema = schema
        logger.debug(f"Set cached FTS index for {self.index_dir}")

    def get_index_for_caching(self) -> tuple[Any, Any]:
        """
        Get index and schema for external caching.

        Returns:
            Tuple of (tantivy_index, schema) for caching

        Raises:
            RuntimeError: If index is not initialized
        """
        if self._index is None:
            raise RuntimeError("Index not initialized. Call initialize_index() first.")

        return self._index, self._schema

    def close(self) -> None:
        """Close the index and writer."""
        if self._writer is not None:
            try:
                with self._lock:
                    self._writer.commit()
                    # Await pending background merge threads before releasing the
                    # writer so that all segment merges complete cleanly on close.
                    # No writer re-creation needed because we are shutting down.
                    self._writer.wait_merging_threads()
            except Exception as e:
                logger.error(f"Failed to commit on close: {e}")
            self._writer = None

        self._index = None
        logger.info("Closed Tantivy index")
