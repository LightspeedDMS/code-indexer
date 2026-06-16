"""
Protocol conformance test for SelfMonitoringBackend (Bug #1140 co-defect fix).

Asserts that EVERY method declared on the SelfMonitoringBackend Protocol is
implemented as a real callable attribute on both concrete backend classes:

  - SelfMonitoringSqliteBackend  (sqlite_backends.py)
  - SelfMonitoringPostgresBackend (postgres/self_monitoring_backend.py)

This test MUST:
  - FAIL (RED)  before list_issues is added to SelfMonitoringPostgresBackend.
  - PASS (GREEN) after the fix.

It prevents future regressions where a new Protocol method is added but one
backend implementation is forgotten.
"""

from __future__ import annotations

import inspect
from typing import Any, List


def _get_protocol_methods() -> List[str]:
    """
    Return all method names declared on SelfMonitoringBackend Protocol.

    Iterates the class members and returns names that are plain functions
    (not dunder methods, not class-level non-callables like __slots__,
    __abstractmethods__ etc.).  We only look at the Protocol class's own
    __dict__ to avoid picking up inherited Protocol machinery.
    """
    from code_indexer.server.storage.protocols import SelfMonitoringBackend

    methods = []
    for name, obj in inspect.getmembers(SelfMonitoringBackend):
        # Skip Python dunder internals
        if name.startswith("__") and name.endswith("__"):
            continue
        # Skip Protocol machinery attributes (e.g. _is_protocol, _abc_data)
        if name.startswith("_"):
            continue
        # Must be callable
        if callable(obj):
            methods.append(name)
    return sorted(methods)


class TestSelfMonitoringBackendProtocolConformance:
    """
    Asserts that every public callable on SelfMonitoringBackend Protocol is
    implemented as a real callable attribute on both concrete backends.

    A plain hasattr + callable sweep is sufficient because the Protocol is not
    @runtime_checkable-checked anywhere at runtime — this is why the gap
    (missing list_issues on PG backend) survived until now.
    """

    def _assert_backend_conforms(
        self,
        backend_class: Any,
        protocol_methods: List[str],
        backend_name: str,
    ) -> None:
        """Assert each protocol method is a callable attribute on backend_class."""
        missing = []
        for method_name in protocol_methods:
            if not hasattr(backend_class, method_name):
                missing.append(f"{method_name} (missing attribute)")
            elif not callable(getattr(backend_class, method_name)):
                missing.append(f"{method_name} (not callable)")

        assert not missing, (
            f"{backend_name} is missing Protocol methods: {missing}\n"
            f"All Protocol methods required: {protocol_methods}"
        )

    def test_sqlite_backend_conforms_to_protocol(self) -> None:
        """SelfMonitoringSqliteBackend must implement every SelfMonitoringBackend method."""
        from code_indexer.server.storage.sqlite_backends import (
            SelfMonitoringSqliteBackend,
        )

        methods = _get_protocol_methods()
        assert methods, "No protocol methods found — check _get_protocol_methods()"
        self._assert_backend_conforms(
            SelfMonitoringSqliteBackend, methods, "SelfMonitoringSqliteBackend"
        )

    def test_postgres_backend_conforms_to_protocol(self) -> None:
        """SelfMonitoringPostgresBackend must implement every SelfMonitoringBackend method.

        This test FAILS (RED) before list_issues is added to the PG backend and
        PASSES (GREEN) after the fix.
        """
        from code_indexer.server.storage.postgres.self_monitoring_backend import (
            SelfMonitoringPostgresBackend,
        )

        methods = _get_protocol_methods()
        assert methods, "No protocol methods found — check _get_protocol_methods()"
        self._assert_backend_conforms(
            SelfMonitoringPostgresBackend, methods, "SelfMonitoringPostgresBackend"
        )

    def test_protocol_declares_list_issues(self) -> None:
        """Sanity check: list_issues must be in the Protocol's declared methods."""
        methods = _get_protocol_methods()
        assert "list_issues" in methods, (
            "list_issues is not declared on SelfMonitoringBackend Protocol — "
            "check protocols.py"
        )

    def test_protocol_declares_list_scans(self) -> None:
        """Sanity check: list_scans must be in the Protocol's declared methods."""
        methods = _get_protocol_methods()
        assert "list_scans" in methods, (
            "list_scans is not declared on SelfMonitoringBackend Protocol — "
            "check protocols.py"
        )

    def test_conformance_covers_all_expected_methods(self) -> None:
        """
        Canary test: the protocol must declare at least the core methods we know
        about.  If this fails, either the Protocol was refactored without updating
        this test, or _get_protocol_methods() has a bug.
        """
        methods = _get_protocol_methods()
        expected = {
            "create_scan_record",
            "get_last_scan_log_id",
            "update_scan_record",
            "cleanup_orphaned_scans",
            "get_last_started_at",
            "fetch_stored_fingerprints",
            "store_issue_metadata",
            "list_scans",
            "list_issues",
            "get_running_scan_count",
            "close",
        }
        missing_from_protocol = expected - set(methods)
        assert not missing_from_protocol, (
            f"Expected protocol methods not found by _get_protocol_methods(): "
            f"{missing_from_protocol}"
        )
