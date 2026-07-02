"""
Unit tests for ClaudeServerClient datetime parsing.

Bug #474: datetime.fromisoformat fails on 7-digit microseconds from Claude Server JWT
Bug #476: dateutil.parser import caused ModuleNotFoundError (dateutil not installed)

Tests verify that the authenticate() method correctly handles all ISO 8601 timestamp
variants returned by Claude Server (.NET), which produces 7-digit fractional seconds.
"""

import inspect
from datetime import timedelta

import pytest
from pytest_httpx import HTTPXMock

# Test constants
TEST_BASE_URL = "https://claude-server.example.com"
TEST_USERNAME = "test_user"
TEST_PASSWORD = "test_password"


def _make_auth_response(expires: str) -> dict:
    """Build a mock auth response with a given expires timestamp."""
    return {
        "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test",
        "expires": expires,
    }


class TestDatetimeParsing:
    """
    Tests for ISO 8601 datetime parsing in authenticate().

    Claude Server (.NET) returns 7-digit fractional seconds which Python's
    datetime.fromisoformat() cannot handle. The fix normalizes to max 6 digits.
    """

    @pytest.mark.asyncio
    async def test_parse_7_digit_fractional_seconds(self, httpx_mock: HTTPXMock):
        """
        Bug #474: authenticate() must parse 7-digit fractional seconds from .NET.

        Given Claude Server returns expires="2026-03-19T23:09:56.5910721+00:00"
        When I call authenticate()
        Then it must NOT raise ValueError and _jwt_expires must be set correctly
        """
        from code_indexer.server.clients.claude_server_client import ClaudeServerClient

        expires_7digits = "2026-03-19T23:09:56.5910721+00:00"
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/auth/login",
            json=_make_auth_response(expires_7digits),
            status_code=200,
        )

        client = ClaudeServerClient(
            base_url=TEST_BASE_URL,
            username=TEST_USERNAME,
            password=TEST_PASSWORD,
        )

        # Must not raise ValueError
        token = await client.authenticate()

        assert token == "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test"
        assert client._jwt_expires is not None
        # Verify the parsed date is correct (truncated to 6 digits = .591072)
        assert client._jwt_expires.year == 2026
        assert client._jwt_expires.month == 3
        assert client._jwt_expires.day == 19
        assert client._jwt_expires.hour == 23
        assert client._jwt_expires.minute == 9
        assert client._jwt_expires.second == 56

    @pytest.mark.asyncio
    async def test_parse_6_digit_fractional_seconds(self, httpx_mock: HTTPXMock):
        """
        Normal case: authenticate() parses standard 6-digit microseconds.

        Given Claude Server returns expires="2026-03-19T23:09:56.591072+00:00"
        When I call authenticate()
        Then _jwt_expires is set with correct microseconds
        """
        from code_indexer.server.clients.claude_server_client import ClaudeServerClient

        expires_6digits = "2026-03-19T23:09:56.591072+00:00"
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/auth/login",
            json=_make_auth_response(expires_6digits),
            status_code=200,
        )

        client = ClaudeServerClient(
            base_url=TEST_BASE_URL,
            username=TEST_USERNAME,
            password=TEST_PASSWORD,
        )

        token = await client.authenticate()

        assert token == "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test"
        assert client._jwt_expires is not None
        assert client._jwt_expires.microsecond == 591072

    @pytest.mark.asyncio
    async def test_parse_3_digit_fractional_seconds(self, httpx_mock: HTTPXMock):
        """
        Millisecond precision: authenticate() parses 3-digit fractional seconds.

        Given expires="2026-03-19T23:09:56.591+00:00"
        When I call authenticate()
        Then _jwt_expires is set correctly
        """
        from code_indexer.server.clients.claude_server_client import ClaudeServerClient

        expires_3digits = "2026-03-19T23:09:56.591+00:00"
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/auth/login",
            json=_make_auth_response(expires_3digits),
            status_code=200,
        )

        client = ClaudeServerClient(
            base_url=TEST_BASE_URL,
            username=TEST_USERNAME,
            password=TEST_PASSWORD,
        )

        token = await client.authenticate()

        assert token == "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test"
        assert client._jwt_expires is not None
        assert client._jwt_expires.second == 56

    @pytest.mark.asyncio
    async def test_parse_no_fractional_seconds(self, httpx_mock: HTTPXMock):
        """
        No fractional seconds: authenticate() parses whole-second timestamps.

        Given expires="2026-03-19T23:09:56+00:00"
        When I call authenticate()
        Then _jwt_expires is set correctly with microsecond=0
        """
        from code_indexer.server.clients.claude_server_client import ClaudeServerClient

        expires_no_fraction = "2026-03-19T23:09:56+00:00"
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/auth/login",
            json=_make_auth_response(expires_no_fraction),
            status_code=200,
        )

        client = ClaudeServerClient(
            base_url=TEST_BASE_URL,
            username=TEST_USERNAME,
            password=TEST_PASSWORD,
        )

        token = await client.authenticate()

        assert token == "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test"
        assert client._jwt_expires is not None
        assert client._jwt_expires.microsecond == 0

    @pytest.mark.asyncio
    async def test_parse_timestamp_with_utc_offset(self, httpx_mock: HTTPXMock):
        """
        Timezone-aware: parsed datetime must have UTC timezone info.

        Given expires="2026-03-19T23:09:56.5910721+00:00"
        When I call authenticate()
        Then _jwt_expires has tzinfo set (UTC)
        """
        from code_indexer.server.clients.claude_server_client import ClaudeServerClient

        expires_with_tz = "2026-03-19T23:09:56.5910721+00:00"
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/auth/login",
            json=_make_auth_response(expires_with_tz),
            status_code=200,
        )

        client = ClaudeServerClient(
            base_url=TEST_BASE_URL,
            username=TEST_USERNAME,
            password=TEST_PASSWORD,
        )

        await client.authenticate()

        assert client._jwt_expires is not None
        assert client._jwt_expires.tzinfo is not None
        # UTC offset should be zero
        assert client._jwt_expires.utcoffset() == timedelta(0)

    @pytest.mark.asyncio
    async def test_parse_timestamp_without_timezone(self, httpx_mock: HTTPXMock):
        """
        Naive datetime: authenticate() handles timestamps with no timezone offset.

        Given expires="2026-03-19T23:09:56.591072" (no timezone)
        When I call authenticate()
        Then _jwt_expires is set (naive datetime, tzinfo is None)
        """
        from code_indexer.server.clients.claude_server_client import ClaudeServerClient

        expires_naive = "2026-03-19T23:09:56.591072"
        httpx_mock.add_response(
            method="POST",
            url=f"{TEST_BASE_URL}/auth/login",
            json=_make_auth_response(expires_naive),
            status_code=200,
        )

        client = ClaudeServerClient(
            base_url=TEST_BASE_URL,
            username=TEST_USERNAME,
            password=TEST_PASSWORD,
        )

        await client.authenticate()

        assert client._jwt_expires is not None
        # Naive datetime has no timezone
        assert client._jwt_expires.tzinfo is None

    def test_no_dateutil_import_in_claude_server_client(self):
        """
        Bug #476: claude_server_client.py must NOT import dateutil.

        dateutil (python-dateutil) is not in the project's dependencies.
        Importing it causes ModuleNotFoundError at runtime.

        Verify by inspecting the module source code directly.
        """
        import code_indexer.server.clients.claude_server_client as module

        source = inspect.getsource(module)

        assert "dateutil" not in source, (
            "claude_server_client.py must not import dateutil — "
            "python-dateutil is not a project dependency (Bug #476)"
        )

    def test_no_dateutil_import_in_sys_modules_after_import(self):
        """
        Bug #476: importing ClaudeServerClient must not pull in dateutil.

        After importing the module, dateutil must not appear in sys.modules.
        """
        import sys

        # Re-import to ensure the module is loaded
        import code_indexer.server.clients.claude_server_client  # noqa: F401

        assert "dateutil" not in sys.modules, (
            "dateutil appeared in sys.modules after importing ClaudeServerClient — "
            "this means a dateutil import exists somewhere in the import chain (Bug #476)"
        )
