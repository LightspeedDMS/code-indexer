"""Tests for require_localhost FastAPI dependency (Story #924 AC1).

Verifies that:
- 127.0.0.1 passes (IPv4 loopback)
- ::1 passes (IPv6 loopback)
- ::ffff:127.0.0.1 passes (dual-stack loopback)
- 127.5.5.5 passes (127.0.0.0/8 subnet)
- External IPv4 (192.168.1.10) is rejected with 403
- Public IP (8.8.8.8) is rejected with 403
- None client is rejected with 403
"""

import pytest
from unittest.mock import MagicMock
from fastapi import HTTPException


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_request(host):
    """Build a minimal Request-like mock with the given client.host."""
    request = MagicMock()
    if host is None:
        request.client = None
    else:
        request.client = MagicMock()
        request.client.host = host
    return request


def _call_require_localhost(host):
    """Call require_localhost with a mocked request for the given host."""
    from code_indexer.server.auth.dependencies import require_localhost

    request = _make_request(host)
    return require_localhost(request)


# ---------------------------------------------------------------------------
# Parametrized: loopback addresses must pass
# ---------------------------------------------------------------------------

_LOOPBACK_HOSTS = [
    "127.0.0.1",  # IPv4 loopback
    "::1",  # IPv6 loopback
    "::ffff:127.0.0.1",  # dual-stack loopback
    "127.5.5.5",  # 127.0.0.0/8 subnet
]


@pytest.mark.parametrize("host", _LOOPBACK_HOSTS)
def test_passes_for_loopback(host):
    """require_localhost must not raise for any loopback address."""
    _call_require_localhost(host)  # must not raise


# ---------------------------------------------------------------------------
# Parametrized: non-loopback addresses must be rejected with 403
# ---------------------------------------------------------------------------

_REJECTED_HOSTS = [
    "192.168.1.10",  # private LAN
    "8.8.8.8",  # public internet
    None,  # no client peer
]


@pytest.mark.parametrize("host", _REJECTED_HOSTS)
def test_rejects_non_loopback(host):
    """require_localhost must raise HTTPException 403 for non-loopback origins."""
    with pytest.raises(HTTPException) as exc_info:
        _call_require_localhost(host)
    assert exc_info.value.status_code == 403
