"""
Unit tests for Bug #1497: `cidx query <pattern> --fts --regex` returns ZERO
results for patterns that plain `--fts` matches.

Root cause (proven empirically, see investigation notes in the fix):
Tantivy's RegexQuery matches whole TERMS in the term dictionary, not raw
document text. The "content"/"content_raw"/"identifiers" fields all use the
"default" tokenizer, which splits on non-alphanumeric characters (including
underscore). So an identifier like "cancel_job" is indexed as two separate
terms "cancel" and "job" -- no single term ever equals (or, since RegexQuery
requires a full-term match, contains) the substring "cancel_job". This holds
regardless of which field is queried or how the pattern is wrapped, because
the term dictionary itself never contains an unsplit "cancel_job" term.

These tests build a REAL Tantivy index via the real TantivyIndexManager (no
mocking of tantivy) and assert that regex search returns genuine matches for
patterns that occur in the indexed source text -- consistent with how a
grep/rg replacement is expected to behave.
"""

import pytest

from code_indexer.services.tantivy_index_manager import TantivyIndexManager


@pytest.fixture
def temp_index_dir(tmp_path):
    """Create temporary index directory."""
    return tmp_path / "tantivy_index"


@pytest.fixture
def tantivy_manager(temp_index_dir):
    """Create and initialize a fresh TantivyIndexManager, closed after the test."""
    manager = TantivyIndexManager(temp_index_dir)
    manager.initialize_index(create_new=True)
    yield manager
    manager.close()


@pytest.fixture
def sample_document():
    """Document containing identifiers with underscores, matching the bug repro."""
    return {
        "path": "src/jobs.py",
        "content": (
            "def cancel_job(self):\n"
            "    pass\n"
            "\n"
            "def register_child_process(self):\n"
            "    pass\n"
        ),
        "content_raw": (
            "def cancel_job(self):\n"
            "    pass\n"
            "\n"
            "def register_child_process(self):\n"
            "    pass\n"
        ),
        "identifiers": ["cancel_job", "register_child_process", "self"],
        "line_start": 1,
        "line_end": 5,
        "language": "python",
    }


@pytest.fixture
def indexed_manager(tantivy_manager, sample_document):
    """Manager with the sample document indexed and committed."""
    tantivy_manager.add_document(sample_document)
    tantivy_manager.commit()
    return tantivy_manager


class TestBug1497RegexUnderscoreIdentifiers:
    """Discriminating RED tests: must fail against the unpatched implementation."""

    def test_baseline_plain_fts_matches_underscore_identifier(self, indexed_manager):
        """
        GIVEN indexed content containing the identifier 'cancel_job'
        WHEN searching with plain --fts (use_regex=False)
        THEN it finds the document (cross-check baseline, per bug report:
             plain --fts already returns 3 real hits for 'cancel_job').
        """
        results = indexed_manager.search(
            query_text="cancel_job",
            use_regex=False,
            snippet_lines=0,
            limit=10,
        )
        assert len(results) > 0, (
            "Baseline plain FTS should match 'cancel_job' (sanity check)"
        )

    def test_regex_bare_identifier_with_underscore_matches(self, indexed_manager):
        """
        GIVEN indexed content containing the identifier 'cancel_job'
        WHEN searching with --fts --regex for the bare identifier 'cancel_job'
        THEN it finds the document (matches plain FTS behavior).

        THIS IS THE CORE BUG #1497 REPRODUCTION: today this returns [] because
        RegexQuery does a full-term match against tokenized fields that split
        "cancel_job" into "cancel" + "job" terms.
        """
        results = indexed_manager.search(
            query_text="cancel_job",
            use_regex=True,
            snippet_lines=0,
            limit=10,
        )
        assert len(results) > 0, (
            "Bug #1497: --fts --regex for 'cancel_job' must match, "
            "consistent with plain --fts"
        )
        assert any(r["path"] == "src/jobs.py" for r in results)

    def test_regex_dot_star_pattern_matches_underscore_identifier(
        self, indexed_manager
    ):
        """
        GIVEN indexed content containing 'cancel_job'
        WHEN searching with --fts --regex for 'cancel_.*'
        THEN it finds the document.

        Bug #1497 scenario (b): a regex with `.*` bridging the underscore.
        """
        results = indexed_manager.search(
            query_text="cancel_.*",
            use_regex=True,
            snippet_lines=0,
            limit=10,
        )
        assert len(results) > 0, "Bug #1497: --fts --regex for 'cancel_.*' must match"
        assert any(r["path"] == "src/jobs.py" for r in results)

    def test_regex_cross_word_pattern_matches_def_line(self, indexed_manager):
        """
        GIVEN indexed content with the line 'def cancel_job(self):'
        WHEN searching with --fts --regex for 'def.*cancel_job'
        THEN it finds the document.

        Bug #1497 scenario: a regex spanning the 'def' keyword and the
        underscore-containing identifier on the same line.
        """
        results = indexed_manager.search(
            query_text="def.*cancel_job",
            use_regex=True,
            snippet_lines=0,
            limit=10,
        )
        assert len(results) > 0, (
            "Bug #1497: --fts --regex for 'def.*cancel_job' must match a def line"
        )
        assert any(r["path"] == "src/jobs.py" for r in results)

    def test_regex_matches_second_identifier_register_child_process(
        self, indexed_manager
    ):
        """
        GIVEN indexed content containing 'register_child_process'
        WHEN searching with --fts --regex for the bare identifier
        THEN it finds the document (second repro pattern from the bug report).
        """
        results = indexed_manager.search(
            query_text="register_child_process",
            use_regex=True,
            snippet_lines=0,
            limit=10,
        )
        assert len(results) > 0, (
            "Bug #1497: --fts --regex for 'register_child_process' must match"
        )

    def test_regex_case_insensitive_still_matches_underscore_identifier(
        self, indexed_manager
    ):
        """
        GIVEN indexed content containing 'cancel_job' (lowercase in source)
        WHEN searching case-insensitively with --fts --regex for 'CANCEL_JOB'
        THEN it finds the document (case-insensitivity must be preserved by the fix).
        """
        results = indexed_manager.search(
            query_text="CANCEL_JOB",
            use_regex=True,
            case_sensitive=False,
            snippet_lines=0,
            limit=10,
        )
        assert len(results) > 0, (
            "Case-insensitive regex must still match underscore identifier"
        )

    def test_regex_case_sensitive_rejects_wrong_case(self, indexed_manager):
        """
        GIVEN indexed content containing 'cancel_job' (lowercase in source)
        WHEN searching case-SENSITIVELY with --fts --regex for 'CANCEL_JOB'
        THEN it does NOT match (case sensitivity must still be honored).
        """
        results = indexed_manager.search(
            query_text="CANCEL_JOB",
            use_regex=True,
            case_sensitive=True,
            snippet_lines=0,
            limit=10,
        )
        assert len(results) == 0, (
            "Case-sensitive regex must not match wrong-case pattern"
        )


class TestBug1497LegacyIndexBackwardCompatibility:
    """
    Regression guard: the fix adds a new schema field (tokenizer_name='raw')
    for verbatim regex matching. Querying a field that a physical on-disk
    index does not have panics at the Rust FFI boundary (proven empirically:
    pyo3_runtime.PanicException "index out of bounds"). A legacy FTS index
    built BEFORE this fix must therefore never crash when queried with
    --fts --regex -- it must gracefully fall back instead.
    """

    def _build_legacy_index(self, index_dir):
        """Build a real on-disk Tantivy index using the PRE-FIX schema shape
        (no 'content_raw_verbatim' raw-tokenizer field), simulating an FTS
        index that already existed before Bug #1497's fix shipped.
        """
        import tantivy

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

        doc = tantivy.Document()
        doc.add_text("path", "src/legacy.py")
        doc.add_text("content", "def cancel_job(self): pass")
        doc.add_text("content_raw", "def cancel_job(self): pass")
        doc.add_text("identifiers", "cancel_job")
        doc.add_unsigned("line_start", 1)
        doc.add_unsigned("line_end", 1)
        doc.add_text("language", "python")
        from tantivy import Facet

        doc.add_facet("language_facet", Facet.from_string("/python"))
        writer.add_document(doc)
        writer.commit()
        writer.wait_merging_threads()

    def test_regex_search_on_legacy_index_without_verbatim_field_does_not_crash(
        self, tmp_path
    ):
        """
        GIVEN a real on-disk FTS index built with the PRE-FIX schema (missing
             the new raw-tokenizer verbatim field)
        WHEN opening it for search and running a regex query for an
             underscore-containing identifier
        THEN it must NOT raise a Rust panic (pyo3_runtime.PanicException) --
             it must return a list (results may legitimately be empty, since
             this legacy index still exhibits Bug #1497's pre-fix limitation
             until it is naturally rebuilt) without crashing.
        """
        index_dir = tmp_path / "legacy_tantivy_index"
        self._build_legacy_index(index_dir)

        manager = TantivyIndexManager(index_dir)
        manager.open_for_search()

        # Must not raise -- this is the critical regression guard.
        results = manager.search(
            query_text="cancel_job",
            use_regex=True,
            snippet_lines=0,
            limit=10,
        )
        assert isinstance(results, list)

        # Non-regex search on the same legacy index must be completely unaffected.
        plain_results = manager.search(
            query_text="cancel_job",
            use_regex=False,
            snippet_lines=0,
            limit=10,
        )
        assert len(plain_results) > 0, (
            "Plain FTS on a legacy index must remain fully functional"
        )


class TestBug1497ExtractionReDoSFollowup:
    """
    Follow-up regression guard found while implementing the main fix above.

    Before this fix, Tantivy's (broken) term-based RegexQuery accidentally
    never found genuine matches for adversarial ReDoS-shaped patterns like
    "(a|a)*b" against realistic multi-line content -- so the Python-level
    match-position/text EXTRACTION step's regex.search(content_raw) call
    was never reached with such patterns in practice, masking a latent
    catastrophic-backtracking vulnerability in that extraction step. Making
    Tantivy's candidate generation CORRECT (the main fix) makes this
    extraction step reachable with genuinely adversarial patterns for the
    first time -- empirically reproduced as a 15s+ hang before adding a
    bounded timeout= to the extraction's regex.search() call.
    """

    @pytest.mark.timeout(5)
    def test_regex_evil_pattern_does_not_hang_after_extraction_timeout_fix(
        self, tantivy_manager
    ):
        """
        GIVEN indexed content containing a run of 'a' characters and a run
             of 'b' characters (mirroring test_tantivy_regex_dfa_safety.py's
             ReDoS fixture)
        WHEN searching with the ReDoS-shaped pattern '(a|a)*b'
        THEN search() completes quickly (well under the 5s safety-net
             timeout) instead of hanging indefinitely.
        """
        import time

        long_repeated_text = "a" * 30 + "X"
        content = (
            "# Test file for ReDoS testing\n"
            f"This line has repeated characters: {long_repeated_text}\n"
            f"Another line with pattern: {'b' * 20}c\n"
            "Normal text without patterns here\n"
            f"Final line: {long_repeated_text}"
        )
        doc = {
            "path": "test/vulnerable.txt",
            "content": content,
            "content_raw": content,
            "identifiers": ["test", "vulnerable"],
            "line_start": 1,
            "line_end": 5,
            "language": "text",
        }
        tantivy_manager.add_document(doc)
        tantivy_manager.commit()

        start_time = time.time()
        results = tantivy_manager.search(
            query_text=r"(a|a)*b",
            use_regex=True,
            limit=10,
        )
        elapsed_time = time.time() - start_time

        assert elapsed_time < 2.0, (
            f"Search took too long: {elapsed_time:.2f}s -- may indicate the "
            f"extraction-timeout ReDoS fix regressed"
        )
        assert isinstance(results, list)
