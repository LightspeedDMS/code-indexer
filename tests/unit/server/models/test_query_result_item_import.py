"""Test that QueryResultItem can be imported without server initialization.

This test ensures that importing QueryResultItem doesn't trigger server app
initialization, which causes unwanted logging and slow imports.

The "fresh import" check runs in a clean subprocess interpreter.  Purging
``code_indexer.server*`` entries from the pytest process's ``sys.modules`` and
re-importing them rebinds package attributes (e.g. ``code_indexer.server`` on
the ``code_indexer`` package) to new module objects; restoring
``sys.modules`` afterwards does not undo those bindings, so every later
``mock.patch("code_indexer.server....")`` in the same session fails to
resolve its target.  A subprocess proves the invariant with nothing to
restore.
"""

import subprocess
import sys
from pathlib import Path

import pytest

SRC_ROOT = str(Path(__file__).parent.parent.parent.parent.parent / "src")
SUBPROCESS_TIMEOUT_SECONDS = 60


# A fresh interpreter importing the server exceeds the suite's default 15 s
# pytest-timeout under parallel gate load; the ceiling must sit above the
# subprocess's own budget.
@pytest.mark.timeout(SUBPROCESS_TIMEOUT_SECONDS + 15)
def test_query_result_item_import_no_server_init():
    """Test that importing QueryResultItem doesn't initialize server app."""
    code = (
        "import sys; "
        f"sys.path.insert(0, {SRC_ROOT!r}); "
        "from code_indexer.server.models.api_models import QueryResultItem; "
        "assert QueryResultItem is not None; "
        "print('server_app_loaded:', 'code_indexer.server.app' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )

    assert result.returncode == 0, (
        f"Subprocess failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "server_app_loaded: False" in result.stdout, (
        "Server app should not be imported when importing QueryResultItem; "
        f"subprocess output: {result.stdout!r}"
    )


def test_query_result_item_has_required_fields():
    """Test that QueryResultItem has all required fields."""
    from code_indexer.server.models.api_models import QueryResultItem

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
