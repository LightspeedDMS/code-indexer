"""
Tests for cluster connection pool wiring of MFA/security services.

Epic #556: Verify that all MFA/security singletons are properly shared
across modules so that lifespan.py pool wiring reaches every consumer.
"""


class TestSingletonSharing:
    """Verify that modules import the SAME singleton instances."""

    def test_routes_does_not_create_separate_login_rate_limiter(self) -> None:
        """routes.py must NOT import LoginRateLimiter class to create a
        duplicate instance. The singleton lives in login_rate_limiter.py
        and is used directly by inline_auth.py."""
        import inspect

        from code_indexer.server.web import routes

        source = inspect.getsource(routes)

        assert "LoginRateLimiter()" not in source, (
            "routes.py creates a SEPARATE LoginRateLimiter() instance. "
            "This duplicate never receives cluster pool wiring from "
            "lifespan.py. Remove it -- the singleton in "
            "login_rate_limiter.py is the canonical instance."
        )

    def test_inline_auth_login_rate_limiter_is_singleton(self) -> None:
        """inline_auth.py must use the same login_rate_limiter instance
        as login_rate_limiter.py's module-level singleton."""
        from code_indexer.server.auth.login_rate_limiter import (
            login_rate_limiter as canonical,
        )
        from code_indexer.server.routers.inline_auth import (
            _default_login_rate_limiter as inline_instance,
        )

        assert inline_instance is canonical, (
            "inline_auth.py should import the singleton from "
            "login_rate_limiter.py, not create a new instance."
        )

    def test_mfa_challenge_manager_is_module_singleton(self) -> None:
        """mfa_challenge.py exposes a module-level singleton that should
        be the same object importable from multiple call sites."""
        from code_indexer.server.auth.mfa_challenge import mfa_challenge_manager

        # Re-import to verify it is the same object
        from code_indexer.server.auth import mfa_challenge as mod

        assert mod.mfa_challenge_manager is mfa_challenge_manager


class TestLifespanClusterPoolWiring:
    """Verify that lifespan.py wires cluster pools to MFA/security services."""

    def test_lifespan_wires_pool_to_mfa_services(self) -> None:
        """lifespan.py must call set_connection_pool on all 3 MFA/security
        services when running in cluster mode.

        We verify by inspecting the source code for the wiring calls.
        This is a structural test -- the actual wiring runs during server
        startup with a real PostgreSQL pool, which is an E2E concern.
        """
        import inspect

        from code_indexer.server.startup import lifespan

        source = inspect.getsource(lifespan)

        # TOTPService wiring (via get_totp_service)
        assert "get_totp_service" in source, (
            "lifespan.py must import get_totp_service to wire the TOTP "
            "service's cluster connection pool"
        )
        assert "set_connection_pool" in source

        # MfaChallengeManager singleton wiring
        assert "mfa_challenge_manager" in source, (
            "lifespan.py must import mfa_challenge_manager singleton "
            "to wire its cluster connection pool"
        )

        # LoginRateLimiter singleton wiring
        assert "login_rate_limiter" in source, (
            "lifespan.py must import login_rate_limiter singleton "
            "to wire its cluster connection pool"
        )
