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

    def test_docstring_does_not_overclaim_strict_atomic_bound(self) -> None:
        """Code review LOW note: cross-node SELECT-then-UPDATE under a
        per-process lock (same mechanism as the auth login limiter) allows a
        small BOUNDED overshoot under simultaneous cross-node bursts -- the
        rate is bounded fleet-wide, not strictly atomic/exact."""
        doc = admission_control.PerConsumerRateLimiter.__doc__ or ""
        assert "genuinely cannot exceed" not in doc
        assert "genuinely bounds" not in doc
        assert "bounded" in doc.lower()
        assert "overshoot" in doc.lower()

    def test_set_connection_pool_docstring_does_not_overclaim_strict_atomic_bound(
        self,
    ) -> None:
        doc = admission_control.PerConsumerRateLimiter.set_connection_pool.__doc__ or ""
        assert "genuinely bounds" not in doc
        assert "genuinely cannot exceed" not in doc
