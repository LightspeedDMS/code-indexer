"""
Tests for the shared server-host validator.

src/code_indexer/server/utils/host_validation.py is the sole authority for
"is this a legal server host value" across every config write path. Accepted
forms: an IPv4 literal, an IPv6 literal, or an RFC 1123 hostname (including
"localhost"); 0.0.0.0 and :: must be accepted. Rejected: whitespace of any
kind (including newline/carriage-return/tab), control characters, quotes,
"=", ";", and "%" (IPv6 zone-id scoping has no meaning for this config key).
"""

import pytest

from code_indexer.server.utils.host_validation import (
    is_valid_server_host,
    validate_server_host,
)

ACCEPTED_HOSTS = [
    "0.0.0.0",
    "127.0.0.1",
    "::",
    "2001:db8::1",
    "localhost",
    "example.com",
    "db.internal.example.com",
    "192.0.2.10",
]

REJECTED_HOSTS = [
    "example.com\nExecStartPre=+/bin/true",  # embedded newline + fake directive
    "example.com value",  # space
    "example.com\ttab",  # tab
    "example.com\rcr",  # carriage return
    'example."com',  # double quote
    "example.'com",  # single quote
    "example=com",  # equals
    "example;com",  # semicolon
    "fe80::1%eth0",  # IPv6 zone id -- rejected, no interface scope concept
    "",  # empty string
    "\x00example.com",  # NUL control char
    "\x1bexample.com",  # ESC control char
]

NON_STRING_HOSTS = [None, 123, 1.5, ["example.com"], {}]


class TestAcceptedHosts:
    """Legitimate values already in production use must keep validating."""

    @pytest.mark.parametrize("host", ACCEPTED_HOSTS)
    def test_is_valid_server_host_accepts(self, host):
        assert is_valid_server_host(host) is True, f"expected {host!r} to be valid"

    @pytest.mark.parametrize("host", ACCEPTED_HOSTS)
    def test_validate_server_host_does_not_raise(self, host):
        validate_server_host(host)  # must not raise


class TestRejectedHosts:
    """Character classes that would corrupt a privileged systemd unit file."""

    @pytest.mark.parametrize("host", REJECTED_HOSTS)
    def test_is_valid_server_host_rejects(self, host):
        assert is_valid_server_host(host) is False, f"expected {host!r} to be invalid"

    @pytest.mark.parametrize("host", REJECTED_HOSTS)
    def test_validate_server_host_raises(self, host):
        with pytest.raises(ValueError):
            validate_server_host(host)

    @pytest.mark.parametrize("host", NON_STRING_HOSTS)
    def test_is_valid_server_host_rejects_non_string(self, host):
        assert is_valid_server_host(host) is False

    @pytest.mark.parametrize("host", NON_STRING_HOSTS)
    def test_validate_server_host_raises_on_non_string(self, host):
        with pytest.raises(ValueError):
            validate_server_host(host)

    def test_rejects_over_length_hostname(self):
        # RFC 1123 hostname max length is 253 chars.
        too_long = "a" * 64 + "." + "b" * 190
        assert len(too_long) > 253
        assert is_valid_server_host(too_long) is False
