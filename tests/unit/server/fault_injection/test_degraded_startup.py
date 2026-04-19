"""Regression test — degraded startup branch must wire app.state.http_client_factory.

Story #746 Codex architectural review MAJOR finding:

When startup_config is None in lifespan.py (degraded startup path), the original
code only set app.state.fault_injection_service = None.  It did NOT set
app.state.http_client_factory, causing AttributeError at request time in
api_keys.py:246 (_make_tester) which unconditionally reads
http_request.app.state.http_client_factory.

Fix: extract the degraded-startup wiring into _apply_fault_injection_state() in
lifespan.py, and set both attributes when startup_config is None:
    app.state.fault_injection_service = None
    app.state.http_client_factory = HttpClientFactory(fault_injection_service=None)
"""

from __future__ import annotations

from fastapi import FastAPI

from code_indexer.server.fault_injection.http_client_factory import HttpClientFactory
from code_indexer.server.startup.lifespan import _apply_fault_injection_state


class TestDegradedStartup:
    """Regression: degraded-startup branch (startup_config is None) must set
    app.state.http_client_factory so api_keys._make_tester() never raises."""

    def test_degraded_startup_sets_http_client_factory(self):
        """app.state.http_client_factory must be an HttpClientFactory after
        the degraded-startup branch executes (startup_config is None).

        RED before the fix: _apply_fault_injection_state does not exist yet
        in lifespan.py, and when startup_config is None it only sets
        fault_injection_service — http_client_factory is missing.
        GREEN after the fix: the function exists and sets both attributes.
        """
        app = FastAPI()
        _apply_fault_injection_state(app, startup_config=None)
        assert isinstance(app.state.http_client_factory, HttpClientFactory)
        assert app.state.fault_injection_service is None
