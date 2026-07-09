"""
Tests for PR #1332 review fix: accurate per-consumer rate-limit documentation.

The original PR docstrings claimed the per-consumer rate limiter guaranteed
"one client cannot starve the fleet" / "cluster-aware when a PG pool is
attached" -- but PerConsumerRateLimiter never actually called
set_connection_pool(), so the claim was false (per-worker only). Now that
set_connection_pool() genuinely wires a dedicated cluster-shared PG table,
the docs must state the TRUE semantics: cluster-shared ONLY when a PG pool
is attached (cluster/postgres mode); per-process otherwise (solo/SQLite).
"""

import inspect

from code_indexer.server.middleware import admission_control
from code_indexer.server.utils.config_manager import AdmissionControlConfig


class TestConfigManagerDocsAccuracy:
    def test_admission_control_config_docstring_does_not_overclaim(self) -> None:
        doc = AdmissionControlConfig.__doc__ or ""
        # The old unconditional claim must be gone.
        assert "cannot starve the fleet" not in doc

    def test_admission_control_config_docstring_states_conditional_guarantee(
        self,
    ) -> None:
        source = inspect.getsource(admission_control) + (
            AdmissionControlConfig.__doc__ or ""
        )
        # Somewhere in the module or config docstring the conditional truth
        # must be documented: cluster-shared only with a PG pool attached.
        assert "per-process" in source.lower() or "per worker" in source.lower()
        assert "pg pool" in source.lower() or "connection pool" in source.lower()


class TestPerConsumerRateLimiterDocsAccuracy:
    def test_docstring_does_not_overclaim_unconditional_fleet_guarantee(self) -> None:
        doc = admission_control.PerConsumerRateLimiter.__doc__ or ""
        assert "cannot starve the fleet" not in doc

    def test_docstring_documents_cluster_vs_solo_semantics(self) -> None:
        doc = admission_control.PerConsumerRateLimiter.__doc__ or ""
        assert "set_connection_pool" in doc

    def test_docstring_states_strict_atomic_bound(self) -> None:
        """Bug #1334 fix: the PG path is now a single atomic conditional
        UPDATE ... WHERE ... decrement (checked via cursor.rowcount) -- no
        SELECT-then-UPDATE race window -- so the docstring must state
        STRICT bounding / zero overshoot, not the old 'small transient
        overshoot possible' / SELECT-then-UPDATE caveat."""
        doc = admission_control.PerConsumerRateLimiter.__doc__ or ""
        assert "small transient overshoot is possible" not in doc.lower()
        assert "select-then-update" not in doc.lower()
        assert "strictly" in doc.lower() or "strict" in doc.lower()
        assert "zero" in doc.lower() and "overshoot" in doc.lower()

    def test_set_connection_pool_docstring_states_strict_atomic_bound(
        self,
    ) -> None:
        """Same Bug #1334 fix, documented on set_connection_pool() too."""
        doc = admission_control.PerConsumerRateLimiter.set_connection_pool.__doc__ or ""
        assert "small transient overshoot is possible" not in doc.lower()
        assert "select-then-update" not in doc.lower()
        assert "strictly" in doc.lower() or "strict" in doc.lower()
        assert "zero" in doc.lower() and "overshoot" in doc.lower()
