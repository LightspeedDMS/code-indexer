"""Unit tests for SCIP query backend abstraction."""

import pytest
from pathlib import Path

try:
    from pysqlite3 import dbapi2 as sqlite3
except ImportError:
    import sqlite3


@pytest.fixture
def scip_fixture_path():
    """Path to comprehensive SCIP test fixture."""
    return (
        Path(__file__).parent.parent / "scip" / "fixtures" / "comprehensive_index.scip"
    )


@pytest.fixture
def db_fixture_path(scip_fixture_path):
    """Path to SCIP database fixture (must exist)."""
    db_path = Path(str(scip_fixture_path) + ".db")
    if not db_path.exists():
        pytest.skip(f"Database fixture not available: {db_path}")
    return db_path


class TestDatabaseBackend:
    """Tests for DatabaseBackend implementation."""

    def test_database_backend_initialization(self, db_fixture_path):
        """Should initialize DatabaseBackend with database connection."""
        from code_indexer.scip.query.backends import DatabaseBackend

        backend = DatabaseBackend(db_fixture_path)

        assert backend.db_path == db_fixture_path
        assert backend.conn is not None
        assert isinstance(backend.conn, sqlite3.Connection)

    def test_database_backend_find_definition(self, db_fixture_path):
        """Should find symbol definitions using database queries."""
        from code_indexer.scip.query.backends import DatabaseBackend
        from code_indexer.scip.query.primitives import QueryResult

        backend = DatabaseBackend(db_fixture_path)

        results = backend.find_definition("UserService", exact=True)

        assert len(results) > 0
        assert all(isinstance(r, QueryResult) for r in results)
        assert all(r.kind == "definition" for r in results)
        assert any("UserService" in r.symbol for r in results)

    def test_database_backend_find_references(self, db_fixture_path):
        """Should find symbol references using database queries."""
        from code_indexer.scip.query.backends import DatabaseBackend
        from code_indexer.scip.query.primitives import QueryResult

        backend = DatabaseBackend(db_fixture_path)

        results = backend.find_references(
            "UserService#authenticate().", limit=10, exact=True
        )

        assert len(results) > 0
        assert all(isinstance(r, QueryResult) for r in results)
        assert all(r.kind == "reference" for r in results)

    def test_database_backend_find_references_substring(self, db_fixture_path):
        """Should find symbol references using substring matching (exact=False).

        Bug reproduction: DatabaseBackend.find_references() returns empty list
        when exact=False, even though database contains matching symbols.

        This test verifies the fix that enables LIKE pattern matching in the
        database query when exact=False.
        """
        from code_indexer.scip.query.backends import DatabaseBackend
        from code_indexer.scip.query.primitives import QueryResult

        backend = DatabaseBackend(db_fixture_path)

        # Test substring matching: "UserService" should match full symbols like
        # ".../.../UserService#authenticate()." in the database
        results = backend.find_references("UserService", limit=10, exact=False)

        # Should find references (not empty)
        assert len(results) > 0, (
            "find_references with exact=False should return results for substring matching"
        )
        assert all(isinstance(r, QueryResult) for r in results)
        assert all(r.kind == "reference" for r in results)
        # Verify substring matching worked - all symbols should contain "UserService"
        assert all("UserService" in r.symbol for r in results)

    def test_database_backend_get_dependencies(self, db_fixture_path):
        """Should find symbol dependencies using database queries."""
        from code_indexer.scip.query.backends import DatabaseBackend
        from code_indexer.scip.query.primitives import QueryResult

        backend = DatabaseBackend(db_fixture_path)

        results = backend.get_dependencies("UserService", depth=1, exact=True)

        # May or may not have results depending on fixture
        assert isinstance(results, list)
        assert all(isinstance(r, QueryResult) for r in results)
        if len(results) > 0:
            assert all(r.kind == "dependency" for r in results)

    def test_database_backend_get_dependents(self, db_fixture_path):
        """Should find symbol dependents using database queries."""
        from code_indexer.scip.query.backends import DatabaseBackend
        from code_indexer.scip.query.primitives import QueryResult

        backend = DatabaseBackend(db_fixture_path)

        results = backend.get_dependents("Logger", depth=1, exact=True)

        # May or may not have results depending on fixture
        assert isinstance(results, list)
        assert all(isinstance(r, QueryResult) for r in results)
        if len(results) > 0:
            assert all(r.kind == "dependent" for r in results)

    def test_database_backend_analyze_impact_validates_depth(self, db_fixture_path):
        """
        Test that DatabaseBackend.analyze_impact() validates depth parameter.

        Given invalid depth values (< 1 or > 10)
        When analyze_impact() is called
        Then ValueError is raised with appropriate message

        This tests Issue #3 from Story #603 code review (backends.py line 264).
        """
        from code_indexer.scip.query.backends import DatabaseBackend

        backend = DatabaseBackend(db_fixture_path)

        # Test depth < 1
        with pytest.raises(ValueError, match="Depth must be between 1 and 10"):
            backend.analyze_impact("SomeSymbol", depth=0)

        # Test depth > 10
        with pytest.raises(ValueError, match="Depth must be between 1 and 10"):
            backend.analyze_impact("SomeSymbol", depth=11)

    def test_database_backend_trace_call_chain(self, db_fixture_path):
        """
        Test DatabaseBackend.trace_call_chain() discovers call chains.

        Given a database with call graph edges
        When trace_call_chain() is called
        Then results contain CallChain objects with path, length, has_cycle
        """
        from code_indexer.scip.query.backends import DatabaseBackend, CallChain

        backend = DatabaseBackend(db_fixture_path)

        # Try to trace a call chain (may not find any depending on fixture)
        results = backend.trace_call_chain("UserService", "Logger", max_depth=3)

        # Assertions
        assert isinstance(results, list)
        assert all(isinstance(r, CallChain) for r in results)
        if len(results) > 0:
            # Verify CallChain structure
            for chain in results:
                assert isinstance(chain.path, list)
                assert len(chain.path) > 0
                assert isinstance(chain.length, int)
                assert chain.length >= 1
                assert isinstance(chain.has_cycle, bool)

    def test_get_dependencies_performance_no_redundant_expansion(self):
        """
        Test that get_dependencies completes in <1 second for class symbols.

        Validates that SQL CTE handles class-to-method expansion instead of
        Python code making N separate SQL calls (Story #611 performance fix).

        Given a class symbol with many methods (e.g., SmartIndexer with ~30 methods)
        When get_dependencies is called with depth=3
        Then it should complete in <1 second (not 4+ seconds from redundant expansion)
        """
        import time
        from code_indexer.scip.query.backends import DatabaseBackend

        # Get project root dynamically
        project_root = Path(__file__).resolve().parent.parent.parent
        db_path = project_root / ".code-indexer/scip/index.scip.db"
        scip_file = project_root / ".code-indexer/scip/code-indexer.scip"

        if not db_path.exists():
            pytest.skip(f"Database not found: {db_path}")
        if not scip_file.exists():
            pytest.skip(f"SCIP file not found: {scip_file}")

        backend = DatabaseBackend(
            db_path, project_root=str(project_root), scip_file=scip_file
        )

        # Query SmartIndexer class (ends with #, has ~30 methods)
        start = time.time()
        results = backend.get_dependencies("SmartIndexer", depth=3, exact=False)
        elapsed = time.time() - start

        # Performance assertion: Should complete in <1 second
        # Before fix: 4.3s (37 separate SQL calls)
        # After fix: <1s (1 SQL call with CTE expansion)
        assert elapsed < 1.0, f"get_dependencies took {elapsed:.3f}s, expected <1.0s"

        # Sanity check: Should find some dependencies
        assert isinstance(results, list)

    def test_get_dependents_performance_no_redundant_expansion(self):
        """
        Test that get_dependents completes in <1 second for class symbols.

        Validates that SQL CTE handles class-to-method expansion instead of
        Python code making N separate SQL calls (Story #611 performance fix).

        Given a class symbol with many methods
        When get_dependents is called with depth=3
        Then it should complete in <1 second
        """
        import time
        from code_indexer.scip.query.backends import DatabaseBackend

        # Get project root dynamically
        project_root = Path(__file__).resolve().parent.parent.parent
        db_path = project_root / ".code-indexer/scip/index.scip.db"
        scip_file = project_root / ".code-indexer/scip/code-indexer.scip"

        if not db_path.exists():
            pytest.skip(f"Database not found: {db_path}")
        if not scip_file.exists():
            pytest.skip(f"SCIP file not found: {scip_file}")

        backend = DatabaseBackend(
            db_path, project_root=str(project_root), scip_file=scip_file
        )

        # Query FileFinder class (should have methods)
        start = time.time()
        results = backend.get_dependents("FileFinder", depth=3, exact=False)
        elapsed = time.time() - start

        # Performance assertion: Should complete in <1 second
        assert elapsed < 1.0, f"get_dependents took {elapsed:.3f}s, expected <1.0s"

        # Sanity check: Should find some dependents
        assert isinstance(results, list)


# ============================================================================
# Bug #662: context field always null — populate from source file
# ============================================================================

# Named constants for the minimal SCIP fixture used in Bug #662 tests.
# The fixture source file has exactly two lines:
#   line 0: "class MyClass:"          <- definition of MyClass at col 6
#   line 1: "MyClass()"               <- reference  of MyClass at col 0
_DEF_LINE = 0
_DEF_START_COL = 6
_DEF_END_COL = 13  # len("MyClass") == 7 chars starting at col 6 → end at 13
_REF_LINE = 1
_REF_START_COL = 0
_REF_END_COL = 7  # len("MyClass") == 7

_SCIP_ROLE_DEFINITION = 1
_SCIP_ROLE_REFERENCE = 0

_FIXTURE_SOURCE_LINES = ["class MyClass:", "MyClass()"]


def _add_occurrence(doc, line: int, start: int, end: int, roles: int) -> None:
    """Add a single-line occurrence to a SCIP document."""
    occ = doc.occurrences.add()
    occ.symbol = "python test `sample`/MyClass#"
    occ.range.extend([line, start, end])
    occ.symbol_roles = roles


def _build_minimal_scip_db(tmp_path: Path, source_lines: list) -> tuple:
    """
    Create a minimal SCIP database alongside a real source file.

    Returns (db_path, project_root).  The source file is written to
    project_root/src/sample.py so that DB entries with relative_path
    'src/sample.py' resolve to actual lines on disk.

    The SCIP protobuf encodes two occurrences in src/sample.py:
      - _DEF_LINE: definition of MyClass (role=DEFINITION)
      - _REF_LINE: reference  of MyClass (role=REFERENCE)
    """
    from code_indexer.scip.database.builder import SCIPDatabaseBuilder
    from code_indexer.scip.protobuf import scip_pb2

    # Write the actual source file
    project_root = tmp_path / "project"
    src_dir = project_root / "src"
    src_dir.mkdir(parents=True)
    source_file = src_dir / "sample.py"
    source_file.write_text("\n".join(source_lines) + "\n")

    # Build a minimal SCIP protobuf
    index = scip_pb2.Index()

    # Declare the symbol
    sym_info = index.external_symbols.add()
    sym_info.symbol = "python test `sample`/MyClass#"
    sym_info.display_name = "MyClass"
    sym_info.kind = scip_pb2.SymbolInformation.Class  # type: ignore[attr-defined]

    # Document with two occurrences
    doc = index.documents.add()
    doc.relative_path = "src/sample.py"
    doc.language = "python"

    _add_occurrence(doc, _DEF_LINE, _DEF_START_COL, _DEF_END_COL, _SCIP_ROLE_DEFINITION)
    _add_occurrence(doc, _REF_LINE, _REF_START_COL, _REF_END_COL, _SCIP_ROLE_REFERENCE)

    scip_file = tmp_path / "index.scip"
    scip_file.write_bytes(index.SerializeToString())

    db_path = tmp_path / "index.scip.db"
    SCIPDatabaseBuilder().build(scip_file, db_path)

    return db_path, project_root


class TestContextFieldBug662:
    """
    Tests for Bug #662: context field is always null in DatabaseBackend results.

    The context field must be populated with the actual source line from the
    file at project_root/file_path at the 0-based line number stored in the DB.
    """

    def test_find_definition_context_is_populated(self, tmp_path: Path):
        """find_definition() must return context populated from source file."""
        from code_indexer.scip.query.backends import DatabaseBackend

        db_path, project_root = _build_minimal_scip_db(tmp_path, _FIXTURE_SOURCE_LINES)

        backend = DatabaseBackend(db_path, project_root=str(project_root))

        results = backend.find_definition("MyClass", exact=False)

        assert len(results) > 0, "Expected at least one definition result"
        # Before fix: context is None.  After fix: actual source line.
        assert results[0].context is not None, "context must be populated, not None"
        assert "MyClass" in results[0].context

    def test_find_references_context_is_populated(self, tmp_path: Path):
        """find_references() must return context populated from source file."""
        from code_indexer.scip.query.backends import DatabaseBackend

        db_path, project_root = _build_minimal_scip_db(tmp_path, _FIXTURE_SOURCE_LINES)

        backend = DatabaseBackend(db_path, project_root=str(project_root))

        results = backend.find_references("MyClass", exact=False)

        assert len(results) > 0, "Expected at least one reference result"
        contexts = [r.context for r in results if r.context is not None]
        assert len(contexts) > 0, "At least one reference must have context populated"

    def test_find_definition_context_matches_correct_line(self, tmp_path: Path):
        """context field value must be the exact source line at the reported line."""
        from code_indexer.scip.query.backends import DatabaseBackend

        custom_lines = ["class MyClass:  # annotated version", "MyClass()"]
        db_path, project_root = _build_minimal_scip_db(tmp_path, custom_lines)

        backend = DatabaseBackend(db_path, project_root=str(project_root))
        results = backend.find_definition("MyClass", exact=False)

        assert len(results) > 0
        defn = next(r for r in results if r.line == _DEF_LINE)
        assert defn.context == custom_lines[_DEF_LINE]

    def test_find_definition_context_graceful_on_missing_source(self, tmp_path: Path):
        """When source file is missing, context stays None (no crash)."""
        from code_indexer.scip.query.backends import DatabaseBackend

        db_path, project_root = _build_minimal_scip_db(tmp_path, _FIXTURE_SOURCE_LINES)

        # Delete the source file after building the DB
        (project_root / "src" / "sample.py").unlink()

        backend = DatabaseBackend(db_path, project_root=str(project_root))
        # Must not raise — context should be None when file is missing
        results = backend.find_definition("MyClass", exact=False)

        assert len(results) > 0
        for r in results:
            assert r.context is None

    def test_find_definition_no_project_root_context_is_none(self, tmp_path: Path):
        """When project_root is empty string, context stays None (no crash)."""
        from code_indexer.scip.query.backends import DatabaseBackend

        db_path, _ = _build_minimal_scip_db(tmp_path, _FIXTURE_SOURCE_LINES)

        backend = DatabaseBackend(db_path, project_root="")
        results = backend.find_definition("MyClass", exact=False)

        assert len(results) > 0
        for r in results:
            assert r.context is None
