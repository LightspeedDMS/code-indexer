"""
Regression test for Bug #434 / Bug #435: SQLite Connection Leak.

Scans src/code_indexer/server/ for direct sqlite3.connect() calls and fails
if any are found outside the explicitly allowed list.

Allowed exceptions (files that legitimately use direct sqlite3.connect()):
- database_manager.py        : implements the connection manager itself
- startup/database_init.py   : one-time initialisation before manager is set up
- database_health_service.py : _check_not_locked() needs isolated connection
                                (BEGIN IMMEDIATE would corrupt shared thread-local state)
"""

import re
from pathlib import Path
from typing import List, Tuple

# Root of the server source tree
_SERVER_SRC = (
    Path(__file__).parent.parent.parent.parent / "src" / "code_indexer" / "server"
)

# Files (relative to _SERVER_SRC) that are explicitly allowed to call
# sqlite3.connect() directly.  Paths use forward slashes for cross-platform
# matching.
_ALLOWED_FILES = {
    "storage/database_manager.py",  # implements the connection manager itself
    "startup/database_init.py",  # one-time init before manager is ready
    "services/database_health_service.py",  # _check_not_locked() needs an isolated
    # connection to avoid corrupting in-flight
    # transactions on the shared thread-local conn
}


def _relative(path: Path) -> str:
    """Return path relative to _SERVER_SRC with forward slashes."""
    return path.relative_to(_SERVER_SRC).as_posix()


def _find_sqlite_connect_calls(src: str) -> List[int]:
    """
    Return line numbers where sqlite3.connect( appears in source text.

    Uses a simple regex rather than AST so it catches all forms including
    aliased imports.  False positives (e.g. in comments/strings) are
    acceptable — they would still indicate a code smell worth reviewing.
    """
    pattern = re.compile(r"\bsqlite3\.connect\s*\(")
    return [
        lineno
        for lineno, line in enumerate(src.splitlines(), start=1)
        if pattern.search(line)
    ]


def test_no_direct_sqlite_connect_outside_allowed_list() -> None:
    """
    Scan every .py file under src/code_indexer/server/ and assert that
    sqlite3.connect() is only called in explicitly allowed files.

    Fails with a detailed message listing every violation found.
    """
    violations: List[Tuple[str, List[int]]] = []

    for py_file in sorted(_SERVER_SRC.rglob("*.py")):
        rel = _relative(py_file)
        if rel in _ALLOWED_FILES:
            continue  # explicitly allowed

        try:
            src = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # unreadable file — skip

        lines = _find_sqlite_connect_calls(src)
        if lines:
            violations.append((rel, lines))

    if violations:
        detail = "\n".join(f"  {rel}: lines {lines}" for rel, lines in violations)
        raise AssertionError(
            f"Bug #434 regression: direct sqlite3.connect() found in "
            f"{len(violations)} file(s) that should use DatabaseConnectionManager:\n"
            f"{detail}\n\n"
            "Fix: replace sqlite3.connect(...) with "
            "DatabaseConnectionManager.get_instance(db_path).get_connection() "
            "or .execute_atomic()."
        )
