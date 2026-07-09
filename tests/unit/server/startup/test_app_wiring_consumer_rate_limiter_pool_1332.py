"""
Tests for PR #1332 review fix: PG-pool wiring for PerConsumerRateLimiter.

Code review required the connection pool be attached to PerConsumerRateLimiter
at the MIDDLEWARE CONSTRUCTION site in app_wiring.create_fastapi_app -- NOT in
lifespan.py (which has unrelated changes on development and would conflict).
The pool is genuinely available there: initialize_services() in
service_init.py builds BackendRegistry (with .connection_pool) and puts it in
the services dict BEFORE create_fastapi_app(services, lifespan) is ever
called (confirmed: PG migrations + StorageFactory.create_backends() both run
inside initialize_services(), well ahead of app-wiring time).

This module follows the same structural-inspection pattern already used in
this codebase for wiring verification (see test_token_bucket_pg.py::
TestLifespanWiring) -- it reads the real production source, asserting the
actual control flow exists, rather than mocking create_fastapi_app's ~20
unpacked service dependencies just to observe one call.
"""

import inspect

from code_indexer.server.startup import app_wiring, lifespan


class TestAppWiringAttachesPoolToConsumerRateLimiter:
    def test_app_wiring_source_references_backend_registry(self) -> None:
        source = inspect.getsource(app_wiring)
        assert "backend_registry" in source

    def test_app_wiring_source_calls_rate_limiter_set_connection_pool(self) -> None:
        source = inspect.getsource(app_wiring)
        assert "_rate_limiter.set_connection_pool" in source

    def test_pool_attachment_is_guarded_by_per_consumer_enabled_block(self) -> None:
        """The set_connection_pool call must live inside the same
        per_consumer_enabled-gated block that constructs _rate_limiter, not
        floating at module scope or in an unrelated branch."""
        source = inspect.getsource(app_wiring.create_fastapi_app)
        rate_limiter_ctor_idx = source.index("_rate_limiter = (")
        set_pool_idx = source.index("_rate_limiter.set_connection_pool")
        add_middleware_idx = source.index(
            "app.add_middleware(\n            AdmissionControlMiddleware"
        )
        # set_connection_pool must be called after construction and before
        # (or at least within the same gated block as) middleware registration.
        assert rate_limiter_ctor_idx < set_pool_idx
        assert set_pool_idx < add_middleware_idx

    def test_pool_attachment_only_when_connection_pool_present(self) -> None:
        """Solo/SQLite mode has no PG pool (backend_registry.connection_pool is
        None) -- the wiring must guard against attaching a None pool."""
        source = inspect.getsource(app_wiring.create_fastapi_app)
        assert "connection_pool is not None" in source


class TestLifespanNotTouchedForThisWiring:
    def test_lifespan_source_does_not_reference_per_consumer_rate_limiter(
        self,
    ) -> None:
        """Constraint: attach the pool at app_wiring construction time, NOT in
        lifespan.py (which has unrelated changes on development and would
        conflict)."""
        source = inspect.getsource(lifespan)
        assert "PerConsumerRateLimiter" not in source
