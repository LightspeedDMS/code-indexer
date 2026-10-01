"""
Unit tests for Bug #1551: SSHKeySyncService's pytest-guard refusal must not
log at CRITICAL.

The guard in `SSHKeySyncService.sync()` correctly refuses to run when a test
process would otherwise reconcile against the real, unoverridden `~/.ssh`.
That refusal is by-design, expected, and requires no operator action -- it is
not an emergency. Bug #1551 observed 8 of 14 high-severity (ERROR-or-above)
log entries in a 24h window on the local server being this single benign
message, crowding out genuine emergencies.

This mirrors the precedent set by Bug #1535 (demote by-design happy-path log
noise, leave genuinely anomalous conditions untouched): only the SEVERITY of
this specific by-design refusal changes. The guard's behavior -- refusing to
touch the real ssh_dir, never consulting the backend, returning an
informative error string -- must be byte-identical before and after.

Acceptance criteria:
AC1: the pytest-guard refusal's log record is emitted BELOW WARNING (i.e.
     DEBUG or INFO, never CRITICAL/ERROR/WARNING).
AC2: sync() still refuses under the exact same condition -- written/removed/
     unchanged all empty, an informative "refus..." error present, and the
     backend's list_keys() is never called.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from code_indexer.server.services.ssh_key_sync_service import SSHKeySyncService


def _make_backend() -> MagicMock:
    backend = MagicMock()
    backend.list_keys.return_value = []
    return backend


class TestPytestGuardRefusalLogSeverity:
    """The by-design pytest-guard refusal must log below WARNING."""

    def test_pytest_guard_refusal_logs_below_warning(self, caplog) -> None:
        backend = _make_backend()
        # Deliberately do NOT override ssh_dir -- this is exactly the
        # condition the guard exists to catch: the class default resolves
        # to the real, unoverridden ~/.ssh while running under pytest.
        svc = SSHKeySyncService(ssh_keys_backend=backend)

        with caplog.at_level(
            logging.DEBUG,
            logger="code_indexer.server.services.ssh_key_sync_service",
        ):
            svc.sync()

        refusal_records = [
            record
            for record in caplog.records
            if "refused" in record.getMessage().lower()
        ]
        assert refusal_records, (
            "Expected a log record documenting the pytest-guard refusal, "
            f"found none. All records: {[r.getMessage() for r in caplog.records]}"
        )
        offending = [r for r in refusal_records if r.levelno >= logging.WARNING]
        assert not offending, (
            "AC1: the by-design pytest-guard refusal must log below "
            f"WARNING, but found level(s): {[r.levelname for r in offending]}. "
            "This is an expected, correctly-handled condition, not an "
            "emergency -- it must not crowd out genuine high-severity "
            "entries (Bug #1551)."
        )

    def test_pytest_guard_still_refuses_and_does_not_touch_backend(self) -> None:
        """AC2: severity is the ONLY thing that changes -- the guard's
        actual refusal behavior (empty results, informative error, backend
        never consulted) must be byte-identical to before the fix."""
        backend = _make_backend()
        svc = SSHKeySyncService(ssh_keys_backend=backend)

        result = svc.sync()

        assert result["written"] == []
        assert result["removed"] == []
        assert result["unchanged"] == []
        assert any("refus" in e.lower() for e in result["errors"])
        backend.list_keys.assert_not_called()
