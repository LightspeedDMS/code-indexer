"""
Unit tests for LangfuseApiClient (Story #165 Code Review Fixes).

Tests retry logic (Finding 4) and observation pagination (Finding 3).
"""

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, Mock, call, patch

import pytest
import requests

from code_indexer.server.services.langfuse_api_client import LangfuseApiClient
from code_indexer.server.utils.config_manager import LangfusePullProject


@pytest.fixture
def mock_creds():
    """Mock credentials for testing."""
    return LangfusePullProject(public_key="test_pk", secret_key="test_sk")


@pytest.fixture
def api_client(mock_creds):
    """Create API client for testing."""
    return LangfuseApiClient("https://test.langfuse.com", mock_creds)


class TestDiscoverProject:
    """Test project discovery."""

    def test_discover_project_success(self, api_client):
        """Test successful project discovery."""
        mock_response = Mock()
        mock_response.json.return_value = {
            "data": [{"name": "test-project", "id": "proj-123"}]
        }

        with patch.object(api_client, "_request_with_retry", return_value=mock_response):
            result = api_client.discover_project()

        assert result == {"name": "test-project", "id": "proj-123"}

    def test_discover_project_no_projects(self, api_client):
        """Test discovery when no projects available."""
        mock_response = Mock()
        mock_response.json.return_value = {"data": []}

        with patch.object(api_client, "_request_with_retry", return_value=mock_response):
            result = api_client.discover_project()

        assert result == {"name": "unknown"}

    def test_discover_project_calls_correct_endpoint(self, api_client):
        """Test that discovery calls the correct API endpoint."""
        mock_response = Mock()
        mock_response.json.return_value = {"data": [{"name": "test"}]}

        with patch.object(api_client, "_request_with_retry", return_value=mock_response) as mock_request:
            api_client.discover_project()

        mock_request.assert_called_once_with(
            "GET", "https://test.langfuse.com/api/public/projects", timeout=15
        )


class TestFetchTracesPage:
    """Test trace fetching."""

    def test_fetch_traces_page_success(self, api_client):
        """Test successful trace fetching."""
        mock_response = Mock()
        mock_response.json.return_value = {
            "data": [
                {"id": "trace1", "name": "Test Trace 1"},
                {"id": "trace2", "name": "Test Trace 2"},
            ]
        }

        from_time = datetime(2024, 1, 1, tzinfo=timezone.utc)
        with patch.object(api_client, "_request_with_retry", return_value=mock_response):
            result = api_client.fetch_traces_page(1, from_time)

        assert len(result) == 2
        assert result[0]["id"] == "trace1"
        assert result[1]["id"] == "trace2"

    def test_fetch_traces_page_params(self, api_client):
        """Test that correct parameters are passed."""
        mock_response = Mock()
        mock_response.json.return_value = {"data": []}

        from_time = datetime(2024, 1, 1, 12, 30, 45, tzinfo=timezone.utc)
        with patch.object(api_client, "_request_with_retry", return_value=mock_response) as mock_request:
            api_client.fetch_traces_page(3, from_time)

        mock_request.assert_called_once_with(
            "GET",
            "https://test.langfuse.com/api/public/traces",
            params={
                "limit": 100,
                "page": 3,
                "fromTimestamp": "2024-01-01T12:30:45+00:00",
            },
            timeout=30,
        )

    def test_fetch_traces_page_empty(self, api_client):
        """Test fetching when no traces available."""
        mock_response = Mock()
        mock_response.json.return_value = {"data": []}

        from_time = datetime(2024, 1, 1, tzinfo=timezone.utc)
        with patch.object(api_client, "_request_with_retry", return_value=mock_response):
            result = api_client.fetch_traces_page(1, from_time)

        assert result == []


class TestFetchObservations:
    """Test observation fetching with pagination (Finding 3)."""

    def test_fetch_observations_single_page(self, api_client):
        """Test fetching observations that fit in one page."""
        mock_response = Mock()
        mock_response.json.return_value = {
            "data": [
                {"id": "obs1", "type": "generation"},
                {"id": "obs2", "type": "span"},
            ]
        }

        with patch.object(api_client, "_request_with_retry", return_value=mock_response) as mock_request:
            result = api_client.fetch_observations("trace123")

        assert len(result) == 2
        assert result[0]["id"] == "obs1"
        assert result[1]["id"] == "obs2"
        # Should only call once since data < 100
        mock_request.assert_called_once()

    def test_fetch_observations_multiple_pages(self, api_client):
        """Test pagination when trace has >100 observations."""
        # First page: 100 observations
        page1_data = [{"id": f"obs{i}", "type": "generation"} for i in range(100)]
        # Second page: 50 observations
        page2_data = [{"id": f"obs{i}", "type": "span"} for i in range(100, 150)]

        responses = [
            Mock(json=lambda: {"data": page1_data}),
            Mock(json=lambda: {"data": page2_data}),
        ]

        with patch.object(api_client, "_request_with_retry", side_effect=responses) as mock_request:
            result = api_client.fetch_observations("trace123")

        # Should get all 150 observations
        assert len(result) == 150
        assert result[0]["id"] == "obs0"
        assert result[99]["id"] == "obs99"
        assert result[100]["id"] == "obs100"
        assert result[149]["id"] == "obs149"

        # Should have called twice (page 1 and page 2)
        assert mock_request.call_count == 2

    def test_fetch_observations_empty(self, api_client):
        """Test fetching observations when none exist."""
        mock_response = Mock()
        mock_response.json.return_value = {"data": []}

        with patch.object(api_client, "_request_with_retry", return_value=mock_response) as mock_request:
            result = api_client.fetch_observations("trace123")

        assert result == []
        mock_request.assert_called_once()

    def test_fetch_observations_pagination_params(self, api_client):
        """Test that pagination parameters are correct."""
        # Simulate 3 pages
        responses = [
            Mock(json=lambda: {"data": [{"id": f"obs{i}"} for i in range(100)]}),
            Mock(json=lambda: {"data": [{"id": f"obs{i}"} for i in range(100, 200)]}),
            Mock(json=lambda: {"data": [{"id": f"obs{i}"} for i in range(200, 250)]}),
        ]

        with patch.object(api_client, "_request_with_retry", side_effect=responses) as mock_request:
            result = api_client.fetch_observations("trace123")

        assert len(result) == 250

        # Verify pagination parameters
        calls = mock_request.call_args_list
        assert calls[0] == call(
            "GET",
            "https://test.langfuse.com/api/public/observations",
            params={"traceId": "trace123", "limit": 100, "page": 1},
            timeout=30,
        )
        assert calls[1] == call(
            "GET",
            "https://test.langfuse.com/api/public/observations",
            params={"traceId": "trace123", "limit": 100, "page": 2},
            timeout=30,
        )
        assert calls[2] == call(
            "GET",
            "https://test.langfuse.com/api/public/observations",
            params={"traceId": "trace123", "limit": 100, "page": 3},
            timeout=30,
        )


class TestRequestWithRetry:
    """Test retry logic for transient errors (Finding 4)."""

    def test_success_on_first_attempt(self, api_client):
        """Test successful request on first attempt."""
        mock_response = Mock()
        mock_response.status_code = 200

        with patch("requests.request", return_value=mock_response) as mock_request:
            result = api_client._request_with_retry("GET", "https://test.com/api")

        assert result.status_code == 200
        mock_request.assert_called_once()

    def test_retry_on_429_rate_limit(self, api_client):
        """Test retry with exponential backoff on 429 rate limit."""
        # First attempt: 429, second attempt: 200
        responses = [
            Mock(status_code=429),
            Mock(status_code=200),
        ]

        with patch("requests.request", side_effect=responses) as mock_request:
            with patch("time.sleep") as mock_sleep:
                result = api_client._request_with_retry("GET", "https://test.com/api")

        assert result.status_code == 200
        assert mock_request.call_count == 2
        # Should wait 2 seconds after first 429
        mock_sleep.assert_called_once_with(2)

    def test_retry_on_429_exhausts_at_max_retries(self, api_client):
        """Test that 429 errors make exactly max_retries requests (not max_retries + 1)."""
        # All attempts return 429
        error_response = Mock(status_code=429)
        error_response.raise_for_status.side_effect = requests.HTTPError("Too Many Requests")

        with patch("requests.request", return_value=error_response) as mock_request:
            with patch("time.sleep") as mock_sleep:
                with pytest.raises(requests.HTTPError):
                    api_client._request_with_retry("GET", "https://test.com/api", max_retries=3)

        # Should make exactly 3 requests (not 4)
        assert mock_request.call_count == 3
        # Should have 2 sleeps (after attempt 0 and 1, but NOT after attempt 2)
        assert mock_sleep.call_count == 2

    def test_retry_on_502_server_error(self, api_client):
        """Test retry on 502 server error."""
        # First attempt: 502, second attempt: 200
        responses = [
            Mock(status_code=502),
            Mock(status_code=200),
        ]

        with patch("requests.request", side_effect=responses) as mock_request:
            with patch("time.sleep") as mock_sleep:
                result = api_client._request_with_retry("GET", "https://test.com/api")

        assert result.status_code == 200
        assert mock_request.call_count == 2
        mock_sleep.assert_called_once()

    def test_retry_on_503_service_unavailable(self, api_client):
        """Test retry on 503 service unavailable."""
        # First attempt: 503, second attempt: 200
        responses = [
            Mock(status_code=503),
            Mock(status_code=200),
        ]

        with patch("requests.request", side_effect=responses) as mock_request:
            with patch("time.sleep") as mock_sleep:
                result = api_client._request_with_retry("GET", "https://test.com/api")

        assert result.status_code == 200
        assert mock_request.call_count == 2
        mock_sleep.assert_called_once()

    def test_exponential_backoff(self, api_client):
        """Test exponential backoff timing."""
        # All attempts fail with 429, including final attempt
        error_response = Mock(status_code=429)
        error_response.raise_for_status.side_effect = requests.HTTPError("Too Many Requests")

        with patch("requests.request", return_value=error_response):
            with patch("time.sleep") as mock_sleep:
                with pytest.raises(requests.HTTPError):
                    api_client._request_with_retry("GET", "https://test.com/api")

        # Should have exponential backoff: 2s, 4s (for attempts 0 and 1)
        # Attempt 2 (last) should fall through without sleep
        calls = [call[0][0] for call in mock_sleep.call_args_list]
        assert calls == [2, 4]

    def test_max_backoff_30_seconds(self, api_client):
        """Test backoff capped at 30 seconds."""
        # All attempts fail with 429, including final attempt
        error_response = Mock(status_code=429)
        error_response.raise_for_status.side_effect = requests.HTTPError("Too Many Requests")

        with patch("requests.request", return_value=error_response):
            with patch("time.sleep") as mock_sleep:
                with pytest.raises(requests.HTTPError):
                    api_client._request_with_retry("GET", "https://test.com/api", max_retries=8)

        # Check that no sleep is > 30 seconds
        calls = [call[0][0] for call in mock_sleep.call_args_list]
        assert all(sleep <= 30 for sleep in calls)
        # Should have 7 sleep calls (attempts 0-6, but NOT attempt 7 which is last)
        assert len(calls) == 7

    def test_retry_on_connection_error(self, api_client):
        """Test retry on connection error."""
        # First attempt: ConnectionError, second attempt: success
        def side_effect(*args, **kwargs):
            if side_effect.call_count == 0:
                side_effect.call_count += 1
                raise requests.ConnectionError("Connection failed")
            return Mock(status_code=200)

        side_effect.call_count = 0

        with patch("requests.request", side_effect=side_effect):
            with patch("time.sleep"):
                result = api_client._request_with_retry("GET", "https://test.com/api")

        assert result.status_code == 200

    def test_connection_error_exhausts_retries(self, api_client):
        """Test that connection errors eventually raise."""
        with patch("requests.request", side_effect=requests.ConnectionError("Failed")):
            with patch("time.sleep"):
                with pytest.raises(requests.ConnectionError):
                    api_client._request_with_retry("GET", "https://test.com/api")

    def test_non_retryable_error_raises_immediately(self, api_client):
        """Test that non-retryable errors (400, 401, 404) raise immediately."""
        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = requests.HTTPError("Not found")

        with patch("requests.request", return_value=mock_response) as mock_request:
            with pytest.raises(requests.HTTPError):
                api_client._request_with_retry("GET", "https://test.com/api")

        # Should only try once
        mock_request.assert_called_once()

    def test_auth_header_included(self, api_client):
        """Test that auth credentials are passed correctly."""
        mock_response = Mock(status_code=200)

        with patch("requests.request", return_value=mock_response) as mock_request:
            api_client._request_with_retry("GET", "https://test.com/api")

        # Verify auth was included
        call_kwargs = mock_request.call_args[1]
        assert "auth" in call_kwargs
        assert call_kwargs["auth"].username == "test_pk"
        assert call_kwargs["auth"].password == "test_sk"
