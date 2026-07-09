"""
Unit tests for LlmCredsClient (Story #365).

Uses httpx.MockTransport for HTTP testing — the httpx-native approach,
no external mocking libraries.
"""

import json

import httpx
import pytest

from code_indexer.server.services.llm_creds_client import (
    CheckoutResponse,
    LlmCredsAuthError,
    LlmCredsClient,
    LlmCredsConnectionError,
    LlmCredsProviderError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_transport(handler):
    """Wrap a handler function into an httpx.MockTransport."""
    return httpx.MockTransport(handler)


def _json_response(data: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=data)


def _empty_response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code)


def _make_client(handler) -> LlmCredsClient:
    """Build a test LlmCredsClient wired to the given handler."""
    return LlmCredsClient(
        provider_url="http://fake-provider",
        api_key="test-key",
        transport=_make_transport(handler),
    )


# ---------------------------------------------------------------------------
# health()
# ---------------------------------------------------------------------------


class TestHealth:
    def test_health_returns_true_on_200(self):
        def handler(request):
            return _json_response({"status": "ok"})

        client = LlmCredsClient(
            provider_url="http://fake-provider",
            api_key="test-key",
            transport=_make_transport(handler),
        )
        assert client.health() is True

    def test_health_returns_false_on_503(self):
        def handler(request):
            return _empty_response(503)

        client = LlmCredsClient(
            provider_url="http://fake-provider",
            api_key="test-key",
            transport=_make_transport(handler),
        )
        assert client.health() is False

    def test_health_returns_false_on_500(self):
        def handler(request):
            return _empty_response(500)

        client = LlmCredsClient(
            provider_url="http://fake-provider",
            api_key="test-key",
            transport=_make_transport(handler),
        )
        assert client.health() is False

    def test_health_includes_api_key_header(self):
        captured = {}

        def handler(request):
            captured["x-api-key"] = request.headers.get("x-api-key")
            return _json_response({"status": "ok"})

        client = LlmCredsClient(
            provider_url="http://fake-provider",
            api_key="my-secret-key",
            transport=_make_transport(handler),
        )
        client.health()
        assert captured["x-api-key"] == "my-secret-key"

    def test_health_raises_connection_error_on_connect_failure(self):
        def handler(request):
            raise httpx.ConnectError("Connection refused")

        client = LlmCredsClient(
            provider_url="http://fake-provider",
            api_key="test-key",
            transport=_make_transport(handler),
        )
        with pytest.raises(LlmCredsConnectionError):
            client.health()


# ---------------------------------------------------------------------------
# checkout()
# ---------------------------------------------------------------------------


class TestCheckout:
    def _full_checkout_response(self):
        return {
            "lease_id": "lease-abc123",
            "credential_id": "cred-xyz789",
            "access_token": "sk-ant-oat01-token",
            "refresh_token": "sk-ant-ort01-refresh",
        }

    def test_checkout_parses_full_response(self):
        def handler(request):
            return _json_response(self._full_checkout_response())

        client = LlmCredsClient(
            provider_url="http://fake-provider",
            api_key="test-key",
            transport=_make_transport(handler),
        )
        result = client.checkout(vendor="anthropic", consumer_id="cidx-server-01")

        assert isinstance(result, CheckoutResponse)
        assert result.lease_id == "lease-abc123"
        assert result.credential_id == "cred-xyz789"
        assert result.access_token == "sk-ant-oat01-token"
        assert result.refresh_token == "sk-ant-ort01-refresh"

    def test_checkout_parses_response_without_optional_tokens(self):
        def handler(request):
            return _json_response(
                {
                    "lease_id": "lease-minimal",
                    "credential_id": "cred-minimal",
                }
            )

        client = LlmCredsClient(
            provider_url="http://fake-provider",
            api_key="test-key",
            transport=_make_transport(handler),
        )
        result = client.checkout(vendor="anthropic", consumer_id="cidx-server-01")

        assert result.lease_id == "lease-minimal"
        assert result.credential_id == "cred-minimal"
        assert result.access_token is None
        assert result.refresh_token is None

    def test_checkout_sends_correct_payload(self):
        captured = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return _json_response(self._full_checkout_response())

        client = LlmCredsClient(
            provider_url="http://fake-provider",
            api_key="test-key",
            transport=_make_transport(handler),
        )
        client.checkout(vendor="anthropic", consumer_id="my-consumer")

        assert captured["body"]["vendor"] == "anthropic"
        assert captured["body"]["consumer_id"] == "my-consumer"

    def test_checkout_sends_post_to_checkout_endpoint(self):
        captured = {}

        def handler(request):
            captured["method"] = request.method
            captured["path"] = request.url.path
            return _json_response(self._full_checkout_response())

        client = LlmCredsClient(
            provider_url="http://fake-provider",
            api_key="test-key",
            transport=_make_transport(handler),
        )
        client.checkout(vendor="anthropic", consumer_id="cidx")

        assert captured["method"] == "POST"
        assert captured["path"] == "/checkout"

    def test_checkout_includes_api_key_header(self):
        captured = {}

        def handler(request):
            captured["x-api-key"] = request.headers.get("x-api-key")
            return _json_response(self._full_checkout_response())

        client = LlmCredsClient(
            provider_url="http://fake-provider",
            api_key="my-api-key",
            transport=_make_transport(handler),
        )
        client.checkout(vendor="anthropic", consumer_id="cidx")

        assert captured["x-api-key"] == "my-api-key"

    def test_checkout_raises_auth_error_on_401(self):
        def handler(request):
            return _empty_response(401)

        client = LlmCredsClient(
            provider_url="http://fake-provider",
            api_key="bad-key",
            transport=_make_transport(handler),
        )
        with pytest.raises(LlmCredsAuthError) as exc_info:
            client.checkout(vendor="anthropic", consumer_id="cidx")
        assert exc_info.value.status_code == 401

    def test_checkout_raises_auth_error_on_403(self):
        def handler(request):
            return _empty_response(403)

        client = LlmCredsClient(
            provider_url="http://fake-provider",
            api_key="bad-key",
            transport=_make_transport(handler),
        )
        with pytest.raises(LlmCredsAuthError) as exc_info:
            client.checkout(vendor="anthropic", consumer_id="cidx")
        assert exc_info.value.status_code == 403

    def test_checkout_raises_provider_error_on_500(self):
        def handler(request):
            return _empty_response(500)

        client = LlmCredsClient(
            provider_url="http://fake-provider",
            api_key="test-key",
            transport=_make_transport(handler),
        )
        with pytest.raises(LlmCredsProviderError) as exc_info:
            client.checkout(vendor="anthropic", consumer_id="cidx")
        assert exc_info.value.status_code == 500

    def test_checkout_raises_provider_error_on_404(self):
        def handler(request):
            return _empty_response(404)

        client = LlmCredsClient(
            provider_url="http://fake-provider",
            api_key="test-key",
            transport=_make_transport(handler),
        )
        with pytest.raises(LlmCredsProviderError) as exc_info:
            client.checkout(vendor="anthropic", consumer_id="cidx")
        assert exc_info.value.status_code == 404

    def test_checkout_raises_connection_error_on_connect_failure(self):
        def handler(request):
            raise httpx.ConnectError("Connection refused")

        client = LlmCredsClient(
            provider_url="http://fake-provider",
            api_key="test-key",
            transport=_make_transport(handler),
        )
        with pytest.raises(LlmCredsConnectionError):
            client.checkout(vendor="anthropic", consumer_id="cidx")

    def test_checkout_raises_connection_error_on_timeout(self):
        def handler(request):
            raise httpx.TimeoutException("Timed out")

        client = LlmCredsClient(
            provider_url="http://fake-provider",
            api_key="test-key",
            transport=_make_transport(handler),
        )
        with pytest.raises(LlmCredsConnectionError):
            client.checkout(vendor="anthropic", consumer_id="cidx")

    def test_checkout_raises_on_non_json_response(self):
        def handler(request):
            return httpx.Response(
                200, content=b"not valid json", headers={"content-type": "text/plain"}
            )

        client = LlmCredsClient(
            provider_url="http://fake-provider",
            api_key="test-key",
            transport=_make_transport(handler),
        )
        with pytest.raises(LlmCredsProviderError) as exc_info:
            client.checkout(vendor="anthropic", consumer_id="cidx")
        assert "invalid JSON" in str(exc_info.value)
        assert exc_info.value.status_code == 200

    def test_checkout_raises_on_missing_required_fields(self):
        def handler(request):
            # Returns valid JSON but missing required lease_id field
            return _json_response({"credential_id": "cred-xyz"})

        client = LlmCredsClient(
            provider_url="http://fake-provider",
            api_key="test-key",
            transport=_make_transport(handler),
        )
        with pytest.raises(LlmCredsProviderError) as exc_info:
            client.checkout(vendor="anthropic", consumer_id="cidx")
        assert "missing required field" in str(exc_info.value)
        assert exc_info.value.status_code == 200


# ---------------------------------------------------------------------------
# checkin()
# ---------------------------------------------------------------------------


class TestCheckin:
    def test_checkin_sends_lease_id(self):
        captured = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return _empty_response(200)

        client = LlmCredsClient(
            provider_url="http://fake-provider",
            api_key="test-key",
            transport=_make_transport(handler),
        )
        client.checkin(lease_id="lease-abc123")

        assert captured["body"]["lease_id"] == "lease-abc123"

    def test_checkin_sends_optional_writeback_fields(self):
        captured = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return _empty_response(200)

        client = LlmCredsClient(
            provider_url="http://fake-provider",
            api_key="test-key",
            transport=_make_transport(handler),
        )
        client.checkin(
            lease_id="lease-abc123",
            credential_id="cred-xyz",
            access_token="new-access",
            refresh_token="new-refresh",
        )

        body = captured["body"]
        assert body["lease_id"] == "lease-abc123"
        assert body["credential_id"] == "cred-xyz"
        assert body["access_token"] == "new-access"
        assert body["refresh_token"] == "new-refresh"

    def test_checkin_omits_none_writeback_fields(self):
        captured = {}

        def handler(request):
            captured["body"] = json.loads(request.content)
            return _empty_response(200)

        client = LlmCredsClient(
            provider_url="http://fake-provider",
            api_key="test-key",
            transport=_make_transport(handler),
        )
        client.checkin(lease_id="lease-abc123")

        body = captured["body"]
        assert "credential_id" not in body
        assert "access_token" not in body
        assert "refresh_token" not in body

    def test_checkin_sends_post_to_checkin_endpoint(self):
        captured = {}

        def handler(request):
            captured["method"] = request.method
            captured["path"] = request.url.path
            return _empty_response(200)

        client = LlmCredsClient(
            provider_url="http://fake-provider",
            api_key="test-key",
            transport=_make_transport(handler),
        )
        client.checkin(lease_id="lease-abc123")

        assert captured["method"] == "POST"
        assert captured["path"] == "/checkin"

    def test_checkin_includes_api_key_header(self):
        captured = {}

        def handler(request):
            captured["x-api-key"] = request.headers.get("x-api-key")
            return _empty_response(200)

        client = LlmCredsClient(
            provider_url="http://fake-provider",
            api_key="my-checkin-key",
            transport=_make_transport(handler),
        )
        client.checkin(lease_id="lease-abc123")

        assert captured["x-api-key"] == "my-checkin-key"

    def test_checkin_raises_auth_error_on_401(self):
        def handler(request):
            return _empty_response(401)

        client = LlmCredsClient(
            provider_url="http://fake-provider",
            api_key="bad-key",
            transport=_make_transport(handler),
        )
        with pytest.raises(LlmCredsAuthError):
            client.checkin(lease_id="lease-abc123")

    def test_checkin_raises_provider_error_on_500(self):
        def handler(request):
            return _empty_response(500)

        client = LlmCredsClient(
            provider_url="http://fake-provider",
            api_key="test-key",
            transport=_make_transport(handler),
        )
        with pytest.raises(LlmCredsProviderError):
            client.checkin(lease_id="lease-abc123")

    def test_checkin_raises_connection_error_on_connect_failure(self):
        def handler(request):
            raise httpx.ConnectError("Connection refused")

        client = LlmCredsClient(
            provider_url="http://fake-provider",
            api_key="test-key",
            transport=_make_transport(handler),
        )
        with pytest.raises(LlmCredsConnectionError):
            client.checkin(lease_id="lease-abc123")


# ---------------------------------------------------------------------------
# NestJS envelope format support (Story #1000)
# ---------------------------------------------------------------------------


def _nestjs_envelope(payload, status_code: int = 200) -> dict:
    """Build a NestJS-style response envelope."""
    return {
        "metadata": {"timestamp": "2026-05-13T00:00:00Z", "statusCode": status_code},
        "payload": payload,
    }


class TestCheckoutNestJsEnvelope:
    def test_checkout_nestjs_envelope_success(self):
        """Checkout succeeds when provider returns NestJS envelope with full payload."""

        def handler(request):
            return _json_response(
                _nestjs_envelope(
                    {
                        "lease_id": "lease-envelope-1",
                        "credential_id": "cred-envelope-1",
                        "access_token": "tok-access",
                        "refresh_token": "tok-refresh",
                        "api_key": "sk-key-envelope",
                    }
                )
            )

        result = _make_client(handler).checkout(
            vendor="anthropic", consumer_id="cidx-server-01"
        )

        assert isinstance(result, CheckoutResponse)
        assert result.lease_id == "lease-envelope-1"
        assert result.credential_id == "cred-envelope-1"
        assert result.access_token == "tok-access"
        assert result.refresh_token == "tok-refresh"
        assert result.api_key == "sk-key-envelope"

    def test_checkout_nestjs_envelope_required_fields_only(self):
        """Checkout succeeds with envelope containing only required fields."""

        def handler(request):
            return _json_response(
                _nestjs_envelope({"lease_id": "lease-min", "credential_id": "cred-min"})
            )

        result = _make_client(handler).checkout(
            vendor="anthropic", consumer_id="cidx-server-01"
        )

        assert result.lease_id == "lease-min"
        assert result.credential_id == "cred-min"
        assert result.access_token is None
        assert result.refresh_token is None
        assert result.api_key is None

    def test_checkout_legacy_flat_still_works(self):
        """Legacy flat format continues to work identically after envelope support added."""

        def handler(request):
            return _json_response(
                {
                    "lease_id": "lease-flat",
                    "credential_id": "cred-flat",
                    "api_key": "sk-flat",
                }
            )

        result = _make_client(handler).checkout(
            vendor="anthropic", consumer_id="cidx-server-01"
        )

        assert result.lease_id == "lease-flat"
        assert result.credential_id == "cred-flat"
        assert result.api_key == "sk-flat"

    def test_checkout_envelope_null_payload(self):
        """Raises LlmCredsProviderError when envelope payload is null."""

        def handler(request):
            return _json_response(_nestjs_envelope(None))

        with pytest.raises(LlmCredsProviderError) as exc_info:
            _make_client(handler).checkout(vendor="anthropic", consumer_id="cidx")
        assert "null/invalid payload" in str(exc_info.value)

    def test_checkout_envelope_missing_lease_id(self):
        """Raises LlmCredsProviderError when envelope payload is missing required lease_id."""

        def handler(request):
            return _json_response(_nestjs_envelope({"credential_id": "cred-only"}))

        with pytest.raises(LlmCredsProviderError) as exc_info:
            _make_client(handler).checkout(vendor="anthropic", consumer_id="cidx")
        assert "missing required field" in str(exc_info.value)


class TestRaiseForStatusNestJsEnvelope:
    def test_raise_for_status_envelope_error_message(self):
        """Extracts errorMessage from NestJS envelope errors array on non-2xx."""

        def handler(request):
            return _json_response(
                {
                    "metadata": {
                        "timestamp": "2026-05-13T00:00:00Z",
                        "statusCode": 422,
                    },
                    "errors": [
                        {"errorMessage": "Vendor 'bad-vendor' is not supported"}
                    ],
                },
                status_code=422,
            )

        with pytest.raises(LlmCredsProviderError) as exc_info:
            _make_client(handler).checkout(vendor="bad-vendor", consumer_id="cidx")
        assert "Vendor 'bad-vendor' is not supported" in str(exc_info.value)
        assert exc_info.value.status_code == 422

    def test_raise_for_status_no_envelope_fallback(self):
        """Falls back to HTTP status-based error for non-envelope error responses."""

        def handler(request):
            return _json_response({"some": "unrelated-body"}, status_code=503)

        with pytest.raises(LlmCredsProviderError) as exc_info:
            _make_client(handler).checkout(vendor="anthropic", consumer_id="cidx")
        assert exc_info.value.status_code == 503


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class TestExceptionHierarchy:
    def test_auth_error_is_provider_error(self):
        err = LlmCredsAuthError("Unauthorized", status_code=401)
        assert isinstance(err, LlmCredsProviderError)
        assert err.status_code == 401

    def test_connection_error_is_provider_error(self):
        err = LlmCredsConnectionError("No route")
        assert isinstance(err, LlmCredsProviderError)

    def test_provider_error_carries_status_code(self):
        err = LlmCredsProviderError("Server error", status_code=500)
        assert err.status_code == 500

    def test_provider_error_status_code_defaults_to_none(self):
        err = LlmCredsProviderError("Generic error")
        assert err.status_code is None
