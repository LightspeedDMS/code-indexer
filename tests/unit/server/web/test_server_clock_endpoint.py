"""
Unit tests for Story #89: Server Clock in Navigation - Server Time Endpoint.

Tests the /api/server-time endpoint that provides server time for client clock synchronization.

Following TDD methodology: Write failing tests FIRST, then implement.
"""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


class TestServerTimeEndpoint:
    """Test /api/server-time endpoint for server clock feature."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        from src.code_indexer.server.app import app

        return TestClient(app)

    def test_server_time_endpoint_exists(self, client):
        """Test AC1: /api/server-time endpoint exists and returns 200 OK."""
        response = client.get("/api/server-time")
        assert response.status_code == 200, "Server time endpoint must return 200 OK"

    def test_server_time_returns_json(self, client):
        """Test AC2: Endpoint returns valid JSON response."""
        response = client.get("/api/server-time")
        assert response.status_code == 200
        assert (
            response.headers["content-type"] == "application/json"
        ), "Response must be JSON"

        # Should not raise exception
        data = response.json()
        assert isinstance(data, dict), "Response must be a JSON object"

    def test_server_time_has_required_fields(self, client):
        """Test AC3: Response includes 'timestamp' and 'timezone' fields."""
        response = client.get("/api/server-time")
        data = response.json()

        assert "timestamp" in data, "Response must include 'timestamp' field"
        assert "timezone" in data, "Response must include 'timezone' field"

    def test_server_time_timestamp_is_iso8601(self, client):
        """Test AC4: Timestamp is in ISO 8601 format."""
        response = client.get("/api/server-time")
        data = response.json()

        timestamp = data["timestamp"]
        assert isinstance(timestamp, str), "Timestamp must be a string"

        # Should be parseable as ISO 8601
        try:
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            assert dt is not None, "Timestamp must be valid ISO 8601 format"
        except ValueError as e:
            pytest.fail(f"Timestamp is not valid ISO 8601 format: {e}")

    def test_server_time_timezone_is_utc(self, client):
        """Test AC5: Timezone field is 'UTC'."""
        response = client.get("/api/server-time")
        data = response.json()

        assert data["timezone"] == "UTC", "Timezone must be 'UTC'"

    def test_server_time_timestamp_is_current(self, client):
        """Test AC6: Timestamp is approximately current server time (within 2 seconds)."""
        before = datetime.now(timezone.utc)
        response = client.get("/api/server-time")
        after = datetime.now(timezone.utc)

        data = response.json()
        server_time = datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))

        # Server time should be between before and after
        assert (
            before <= server_time <= after
        ), "Server time must be current (within request window)"

        # Additional check: within 2 seconds of request time
        diff = abs((server_time - before).total_seconds())
        assert diff < 2, "Server time must be within 2 seconds of actual time"

    def test_server_time_no_auth_required(self, client):
        """Test AC7: Endpoint does not require authentication (lightweight)."""
        # Call endpoint without any authentication headers
        response = client.get("/api/server-time")

        # Should succeed without 401 Unauthorized
        assert (
            response.status_code == 200
        ), "Endpoint must not require authentication for time sync"

    def test_server_time_consistent_format(self, client):
        """Test AC8: Multiple calls return consistent format."""
        # Make multiple requests
        responses = [client.get("/api/server-time").json() for _ in range(3)]

        # All should have same structure
        for data in responses:
            assert "timestamp" in data
            assert "timezone" in data
            assert data["timezone"] == "UTC"

            # All timestamps should be ISO 8601
            datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
