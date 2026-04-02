"""Test that QueryResultItem can be imported without server initialization.

This test ensures that importing QueryResultItem doesn't trigger server app
initialization, which causes unwanted logging and slow imports.
"""

import sys


def test_query_result_item_import_no_server_init():
    """Test that importing QueryResultItem doesn't initialize server app."""
    # Save a snapshot of all code_indexer.server modules so we can restore them
    # afterward — clearing without restoring contaminates subsequent tests in the
    # same pytest session (patches targeting the original module objects no longer
    # intercept calls from the newly-imported module objects).
    snapshot = {m: sys.modules[m] for m in sys.modules if "code_indexer.server" in m}
    for module in snapshot:
        del sys.modules[module]

    try:
        # Import QueryResultItem from api_models (new location)
        from src.code_indexer.server.models.api_models import QueryResultItem

        # Verify server app was NOT initialized
        # If server app initialized, it would be in sys.modules
        assert "src.code_indexer.server.app" not in sys.modules, (
            "Server app should not be imported when importing QueryResultItem"
        )

        # Verify QueryResultItem is a valid class
        assert QueryResultItem is not None
        assert hasattr(QueryResultItem, "__init__")
    finally:
        # Remove any freshly-imported code_indexer.server modules and restore the
        # originals so subsequent tests see a consistent module cache.
        for m in list(sys.modules):
            if "code_indexer.server" in m:
                del sys.modules[m]
        sys.modules.update(snapshot)
        # Repair parent package attribute: importing a fresh src.code_indexer.server
        # sets sys.modules["src.code_indexer"].server to the new module object, which
        # persists even after we restore sys.modules["src.code_indexer.server"] to the
        # original. unittest.mock._dot_lookup traverses package attributes (not
        # sys.modules), so it would get the stale new object — causing AttributeError
        # when later tests try to patch src.code_indexer.server.services.*.
        _server_key = "src.code_indexer.server"
        _parent_key = "src.code_indexer"
        if _server_key in snapshot and _parent_key in sys.modules:
            sys.modules[_parent_key].server = snapshot[_server_key]


def test_query_result_item_has_required_fields():
    """Test that QueryResultItem has all required fields."""
    from src.code_indexer.server.models.api_models import QueryResultItem

    # Create an instance to verify fields
    result = QueryResultItem(
        file_path="/test/path.py",
        line_number=42,
        code_snippet="def test(): pass",
        similarity_score=0.95,
        repository_alias="test-repo",
        file_last_modified=1699999999.0,
        indexed_timestamp=1700000000.0,
    )

    assert result.file_path == "/test/path.py"
    assert result.line_number == 42
    assert result.code_snippet == "def test(): pass"
    assert result.similarity_score == 0.95
    assert result.repository_alias == "test-repo"
    assert result.file_last_modified == 1699999999.0
    assert result.indexed_timestamp == 1700000000.0
