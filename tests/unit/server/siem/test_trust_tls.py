"""Both outbound legs (the OAuth token exchange and ``events:import``) over
REAL TLS to a loopback endpoint whose certificate comes from a test CA:
verification fails without the trusted CA, succeeds with it, and is never
disabled (a hostname mismatch still fails with the CA trusted)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator, Tuple

import pytest

from code_indexer.server.fault_injection.http_client_factory import HttpClientFactory
from code_indexer.server.services.siem_delivery.destination import Destination
from code_indexer.server.services.siem_delivery.sender import (
    CredentialProvider,
    ProbeResult,
    send_batch,
)
from tests.fixtures.secops_sidecar.harness import SidecarHandle

from .conftest import sidecar_loader
from .tls_fixtures import (
    IMPORT_PATH,
    ConnectProxy,
    IssuedCert,
    TlsEndpoint,
    make_ca,
    make_leaf,
)

BODY = json.dumps({"inlineSource": {"events": []}}).encode()


@pytest.fixture()
def test_ca() -> IssuedCert:
    return make_ca("Example Emulator CA")


@pytest.fixture()
def endpoint(test_ca: IssuedCert, scratch_dir: Path) -> Iterator[TlsEndpoint]:
    with TlsEndpoint(make_leaf(test_ca), scratch_dir) as ep:
        yield ep


def _dest(ep: TlsEndpoint, ca_pem: str) -> Destination:
    return Destination(
        key="gsecops:0000000000000000",
        origin=ep.origin,
        import_path=IMPORT_PATH,
        token_uri=ep.origin + "/token",
        harness=False,
        api_version="v1",
        trusted_ca_pem=ca_pem,
    )


def _legs(
    ep: TlsEndpoint, sidecar: SidecarHandle, ca_pem: str
) -> Tuple[ProbeResult, str]:
    http = HttpClientFactory(fault_injection_service=None)
    dest = _dest(ep, ca_pem)
    provider = CredentialProvider(
        http, sidecar_loader(sidecar, token_uri=dest.token_uri), 5.0
    )
    probe = provider.probe(dest)
    cls = send_batch(http, dest, "example-token", BODY, event_count=1, timeout=5.0)
    return probe, cls.cls


def test_both_legs_fail_verification_without_the_trusted_ca(
    endpoint: TlsEndpoint, siem_sidecar: SidecarHandle
) -> None:
    probe, import_class = _legs(endpoint, siem_sidecar, "")
    assert probe is ProbeResult.TOKEN_ENDPOINT_UNREACHABLE
    assert import_class != "accepted"
    assert endpoint.paths == []  # no request ever completed the handshake


def test_both_legs_succeed_with_the_trusted_ca(
    endpoint: TlsEndpoint, siem_sidecar: SidecarHandle, test_ca: IssuedCert
) -> None:
    probe, import_class = _legs(endpoint, siem_sidecar, test_ca.pem)
    assert probe is ProbeResult.OK
    assert import_class == "accepted"
    assert endpoint.paths == ["/token", IMPORT_PATH]


def test_hostname_is_still_verified_with_the_ca_trusted(
    siem_sidecar: SidecarHandle, test_ca: IssuedCert, scratch_dir: Path
) -> None:
    wrong = make_leaf(test_ca, ip="192.0.2.10", dns="emulator.example.com")
    with TlsEndpoint(wrong, scratch_dir) as ep:
        probe, import_class = _legs(ep, siem_sidecar, test_ca.pem)
        assert probe is ProbeResult.TOKEN_ENDPOINT_UNREACHABLE
        assert import_class != "accepted"
        assert ep.paths == []


def _proxy_env(monkeypatch: pytest.MonkeyPatch, proxy_url: str, no_proxy: str) -> None:
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", proxy_url)
    if no_proxy:
        monkeypatch.setenv("NO_PROXY", no_proxy)


def test_env_proxy_is_honoured_with_a_trusted_ca(
    endpoint: TlsEndpoint,
    siem_sidecar: SidecarHandle,
    test_ca: IssuedCert,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TLS-inspecting proxy setup: the CA is trusted AND the environment
    proxy is still used for both legs."""
    with ConnectProxy() as proxy:
        _proxy_env(monkeypatch, proxy.url, "")
        probe, import_class = _legs(endpoint, siem_sidecar, test_ca.pem)
        assert probe is ProbeResult.OK and import_class == "accepted"
        assert proxy.targets == [f"127.0.0.1:{endpoint.port}"] * 2


def test_no_proxy_bypasses_the_proxy_with_a_trusted_ca(
    endpoint: TlsEndpoint,
    siem_sidecar: SidecarHandle,
    test_ca: IssuedCert,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with ConnectProxy() as proxy:
        _proxy_env(monkeypatch, proxy.url, "127.0.0.1")
        probe, import_class = _legs(endpoint, siem_sidecar, test_ca.pem)
        assert probe is ProbeResult.OK and import_class == "accepted"
        assert proxy.targets == []


def test_no_siem_module_can_disable_verification() -> None:
    import code_indexer.server.services.siem_delivery as pkg

    root = Path(pkg.__file__).parent
    for source in root.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        for needle in ("verify=False", "CERT_NONE", "check_hostname = False"):
            assert needle not in text, f"{source.name} contains {needle}"
