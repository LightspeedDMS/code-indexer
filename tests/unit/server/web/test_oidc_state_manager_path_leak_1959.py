"""
Bug #1959 (Gate 1 follow-on): a SECOND, distinct root cause found while
proving `pytest tests/unit/server/web/ tests/unit/server/auth/` passes.

The issue's headline symptom was the event-loop RuntimeError (fixed in
tests/unit/server/conftest.py -- see test_event_loop_leak_protection_1959.py).
But even after that fix, the SAME combined command still failed 47 tests,
all under tests/unit/server/auth/oidc/ and tests/unit/server/auth/oauth/,
every one with:

    sqlite3.OperationalError: unable to open database file

Root cause: `code_indexer.server.auth.oidc.state_manager` keeps a
module-level global, `_configured_sqlite_path`, written once by
`configure_sqlite_path()` -- called from
`server/startup/service_init.py` in the SAME block that wires
`token_blacklist.set_sqlite_path(db_path)` and
`elevated_session_manager.set_sqlite_path(db_path)` (the two singletons
`tests/unit/server/web/conftest.py`'s `_restore_dependency_globals` fixture
ALREADY captures and restores -- see that file's docstring for the
original two-singleton leak this fixture was written to close). Multiple
`web/` test fixtures call `create_app()` inside
`patch.dict("os.environ", {"CIDX_SERVER_DATA_DIR": tmpdir})`, which wires
ALL THREE singletons to a path under that per-test tmpdir. The existing
fixture restored two of the three; `state_manager._configured_sqlite_path`
was missed, so once `patch.dict` exits and the tmpdir is cleaned up, every
subsequent `StateManager()` construction anywhere in the process (e.g.
every test in `tests/unit/server/auth/oidc/` and
`tests/unit/server/auth/oauth/`) tries to open a SQLite file under a
directory that no longer exists.

This is the third instance of this exact leak SHAPE in one day (per the
issue's own history: the two-singleton fix above, and Bug #1957's JWT
logout guard), confirming the issue's own prediction that more existed.
The fix extends the SAME `_restore_dependency_globals` fixture with the
third singleton, refactored into a plain `_restore_dependency_globals_impl`
generator (mirroring the tree-wide conftest.py `_impl` pattern) so this
test can drive it directly via `next()` and assert the exact repair
against the REAL module global -- no test double standing in for either
the generator or `state_manager`.
"""

from __future__ import annotations

import pytest

from code_indexer.server.auth.oidc import state_manager
from tests.unit.server.web.conftest import _restore_dependency_globals_impl


def test_configured_sqlite_path_leak_is_restored_after_teardown() -> None:
    """A `_configured_sqlite_path` value set mid-test (mimicking what
    `create_app()`'s real `service_init.py` wiring does for a per-test
    tmpdir) must be reverted to its pre-test value once the generator's
    teardown phase runs -- not left pointing at a directory that may no
    longer exist by the time a later test constructs a `StateManager()`.
    """
    original_value = state_manager._configured_sqlite_path
    try:
        gen = _restore_dependency_globals_impl()
        next(gen)  # setup phase: snapshot taken before any mutation

        state_manager._configured_sqlite_path = "/tmp/leaked-oidc-tmpdir-1959/db"
        assert (
            state_manager._configured_sqlite_path == "/tmp/leaked-oidc-tmpdir-1959/db"
        )

        with pytest.raises(StopIteration):
            next(gen)  # teardown phase: restore

        assert state_manager._configured_sqlite_path == original_value, (
            "Bug #1959: state_manager._configured_sqlite_path leaked past "
            "the web/conftest.py restore fixture's teardown -- a later "
            "test (e.g. anything under tests/unit/server/auth/oidc/) would "
            "try to open a SQLite file under a directory that may no "
            "longer exist."
        )
    finally:
        state_manager._configured_sqlite_path = original_value
