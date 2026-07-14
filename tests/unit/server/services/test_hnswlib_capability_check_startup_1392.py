"""Tests for Bug #1392: server-side hnswlib fork-capability startup check.

Production bug: a drifted (stock PyPI) hnswlib install on this Python
environment causes every finalize-time orphan detect+repair call to fail
with AttributeError. The CLI side (storage/hnsw_index_manager.py) fails
LOUD via HNSWCapabilityError at build/finalize entry points. The SERVER
side is different: hard-failing server STARTUP over this would take down
ALL query serving (confirmed unaffected by the underlying bug) merely
because HNSW finalize/repair would eventually fail -- a wildly
disproportionate blast-radius increase, and a violation of the "Query Is
Everything" invariant. So the server-side check logs ERROR loudly but NEVER
raises/blocks startup.

`check_hnswlib_capability()` is intentionally decoupled from FastAPI/lifespan
plumbing so it is independently unit-testable without spinning up the app.
"""

import sys
from unittest.mock import patch

import hnswlib
import pytest

from code_indexer.server.services.hnswlib_capability_check import (
    check_hnswlib_capability,
    run_hnswlib_capability_startup_check,
)
from code_indexer.storage.hnsw_index_manager import EXPECTED_HNSWLIB_FORK_COMMIT


@pytest.fixture
def missing_capability():
    """Temporarily remove check_integrity/repair_orphans from the REAL
    hnswlib.Index class, restoring them unconditionally afterward."""
    saved = {}
    for attr in ("check_integrity", "repair_orphans"):
        if hasattr(hnswlib.Index, attr):
            saved[attr] = getattr(hnswlib.Index, attr)
            delattr(hnswlib.Index, attr)
    try:
        yield
    finally:
        for attr, value in saved.items():
            setattr(hnswlib.Index, attr, value)


class TestCheckHnswlibCapability:
    """Unit tests for check_hnswlib_capability()."""

    def test_returns_success_when_capability_present(self) -> None:
        ok, message = check_hnswlib_capability()
        assert ok is True
        assert message == "ok"

    def test_returns_failure_with_actionable_message_when_capability_missing(
        self, missing_capability
    ) -> None:
        ok, message = check_hnswlib_capability()
        assert ok is False
        assert sys.executable in message
        assert EXPECTED_HNSWLIB_FORK_COMMIT in message
        assert "docs/hnswlib-custom-build.md" in message

    def test_returns_failure_when_hnswlib_not_installed(self) -> None:
        # Setting a sys.modules entry to None forces the next `import X` to
        # raise ImportError -- the standard technique for simulating an
        # absent package without touching the real installed package.
        with patch.dict(sys.modules, {"hnswlib": None}):
            ok, message = check_hnswlib_capability()
        assert ok is False
        assert sys.executable in message


class TestRunHnswlibCapabilityStartupCheck:
    """run_hnswlib_capability_startup_check() -- the small, independently
    testable helper the lifespan startup sequence calls. Its own internal
    try/except guarantees it NEVER raises/blocks server startup, regardless
    of what check_hnswlib_capability() does -- proven here without needing
    to spin up the whole FastAPI app."""

    def test_never_raises_when_check_raises_exception(self) -> None:
        with patch(
            "code_indexer.server.services.hnswlib_capability_check.check_hnswlib_capability",
            side_effect=RuntimeError("boom"),
        ):
            run_hnswlib_capability_startup_check()  # must not raise
