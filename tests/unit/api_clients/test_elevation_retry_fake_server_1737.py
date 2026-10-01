"""Tests for the fake server's TOTP elevation-simulation toggle (Bug #1737).

Bug #1725 deliberately shipped the fake test server's ``create_user`` and
``change_user_password`` routes WITHOUT mirroring the real production
``require_elevation()`` (TOTP step-up) gate documented in
``inline_admin_users.py`` -- every test against those routes only exercised
role-based (403) and validation (422) paths, never ``elevation_required``.
That left ``admin_client.py``'s ``_check_elevation_required``/
``ElevationRequiredError`` handling and the CLI's ``with_elevation_retry``
single-retry wrapper (Epic #922 / Story #980) with ZERO coverage from the
fake-server suite.

This module adds the minimal opt-in "elevation switch"
(``TestCIDXServer.simulate_elevation_required``, default ``False`` --
current behavior) and proves the real client-side retry contract against it:
the first call to a gated route returns 403 ``elevation_required``,
``with_elevation_retry`` elevates via a real round trip to the fake's new
``POST /auth/elevate`` route, then retries exactly once and succeeds.
"""

import tempfile
from pathlib import Path
from typing import Any, Dict

import pytest

from code_indexer.api_clients.admin_client import AdminAPIClient
from code_indexer.api_clients.elevation import with_elevation_retry
from tests.infrastructure.test_cidx_server import CIDXServerTestContext


@pytest.mark.slow
class TestElevationRetryAgainstFakeServer:
    """Elevation-retry round trip against the fake server's opt-in toggle."""

    @pytest.fixture
    async def test_server(self):
        """Start real (fake) CIDX server for testing."""
        context = CIDXServerTestContext()
        server = await context.__aenter__()
        server.server_url = context.base_url  # type: ignore[attr-defined]

        try:
            yield server
        finally:
            await context.__aexit__(None, None, None)

    @pytest.fixture
    def admin_credentials(self) -> Dict[str, Any]:
        """Admin credentials matching the fake server's built-in TEST_USERS."""
        return {
            "username": "admin",
            "password": "admin123",
        }

    @pytest.fixture
    def temp_project_root(self):
        """Create temporary project root for testing."""
        with tempfile.TemporaryDirectory() as temp_dir:
            yield Path(temp_dir)

    async def test_create_user_elevation_retry_round_trip(
        self, test_server, admin_credentials, temp_project_root
    ):
        """First create_user call hits elevation_required; single retry succeeds.

        Discriminating assertion: ``call_count`` proves ``fn()`` (the
        underlying ``create_user`` call) actually ran TWICE -- once
        rejected with 403 elevation_required, once succeeding after a real
        POST /auth/elevate round trip. Without the toggle wired correctly,
        the first call would succeed outright and call_count would be 1.
        """
        test_server.simulate_elevation_required = True

        admin_client = AdminAPIClient(
            server_url=test_server.server_url,
            credentials=admin_credentials,
            project_root=temp_project_root,
        )

        call_count = {"n": 0}

        def call_create_user() -> Dict[str, Any]:
            call_count["n"] += 1
            return admin_client.create_user(
                username="totp_elevation_user",
                password="ElevatedPass123!",
                role="normal_user",
            )

        try:
            result = with_elevation_retry(
                fn=call_create_user,
                session=admin_client.session,
                server_url=test_server.server_url,
                token=admin_client._get_valid_token(),
                prompt_totp=lambda: "123456",
            )

            assert call_count["n"] == 2, (
                "with_elevation_retry must call fn() exactly twice: once "
                "rejected with elevation_required, once succeeding after "
                f"elevate() -- got {call_count['n']} call(s)"
            )
            assert result["user"]["username"] == "totp_elevation_user"
            assert result["user"]["role"] == "normal_user"

        finally:
            admin_client.close()

    async def test_change_user_password_elevation_retry_round_trip(
        self, test_server, admin_credentials, temp_project_root
    ):
        """First change_user_password call hits elevation_required; retry succeeds."""
        test_server.simulate_elevation_required = True
        test_server.users["totp_target_user"] = {
            "username": "totp_target_user",
            "password": "OldPass123!",
            "user_id": "totp-target-user-id",
            "role": "normal_user",
            "created_at": "2024-01-01T00:00:00Z",
        }

        admin_client = AdminAPIClient(
            server_url=test_server.server_url,
            credentials=admin_credentials,
            project_root=temp_project_root,
        )

        call_count = {"n": 0}

        def call_change_password() -> Dict[str, Any]:
            call_count["n"] += 1
            return admin_client.change_user_password(
                username="totp_target_user",
                new_password="BrandNewPass123!",
            )

        try:
            result = with_elevation_retry(
                fn=call_change_password,
                session=admin_client.session,
                server_url=test_server.server_url,
                token=admin_client._get_valid_token(),
                prompt_totp=lambda: "123456",
            )

            assert call_count["n"] == 2, (
                "with_elevation_retry must call fn() exactly twice for "
                f"change_user_password -- got {call_count['n']} call(s)"
            )
            assert "message" in result
            assert (
                test_server.users["totp_target_user"]["password"] == "BrandNewPass123!"
            )

        finally:
            admin_client.close()

    async def test_create_user_default_toggle_off_succeeds_without_elevation(
        self, test_server, admin_credentials, temp_project_root
    ):
        """Regression proof: default (toggle OFF) behavior is unchanged.

        Zero-regression guard for the 12+ consumer files of this fake
        server that never touch ``simulate_elevation_required`` and expect
        the pre-existing role-only-gated 201 success path.
        """
        assert test_server.simulate_elevation_required is False

        admin_client = AdminAPIClient(
            server_url=test_server.server_url,
            credentials=admin_credentials,
            project_root=temp_project_root,
        )

        try:
            result = admin_client.create_user(
                username="no_elevation_user",
                password="RegularPass123!",
                role="normal_user",
            )
            assert result["user"]["username"] == "no_elevation_user"
        finally:
            admin_client.close()
