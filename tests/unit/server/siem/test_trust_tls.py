"""Both outbound legs (the OAuth token exchange and ``events:import``) over
REAL TLS to a loopback endpoint whose certificate comes from a test CA:
verification fails without the trusted CA, succeeds with it, and is never
disabled (a hostname mismatch still fails with the CA trusted)."""

from __future__ import annotations

import http.server
import json
import os
import ssl
import threading
from pathlib import Path
from typing import Any, Iterator, List, Tuple

import httpx
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
    TlsTerminator,
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


_FD_SETSIZE = 1024


@pytest.fixture()
def high_fds() -> Iterator[None]:
    """Hold enough dummy fds that every socket opened next is numbered above
    FD_SETSIZE, as in a long-running gate process (select() cannot)."""
    held: List[int] = []
    try:
        while True:
            fd = os.open(os.devnull, os.O_RDONLY)
            held.append(fd)
            if fd > _FD_SETSIZE + 64:
                break
        yield
    finally:
        for fd in held:
            os.close(fd)


def test_connect_proxy_relays_with_fds_above_fd_setsize(
    high_fds: None,
    siem_sidecar: SidecarHandle,
    test_ca: IssuedCert,
    scratch_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TlsEndpoint(make_leaf(test_ca), scratch_dir) as endpoint:
        with ConnectProxy() as proxy:
            _proxy_env(monkeypatch, proxy.url, "")
            probe, import_class = _legs(endpoint, siem_sidecar, test_ca.pem)
            assert probe is ProbeResult.OK and import_class == "accepted"
            assert len(proxy.targets) == 2


class _PlainUpstream(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server API
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = b"x" * 70_000  # several TLS records back through the relay
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        return


def test_tls_front_relays_with_fds_above_fd_setsize(
    high_fds: None, test_ca: IssuedCert, scratch_dir: Path
) -> None:
    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _PlainUpstream)
    worker = threading.Thread(target=upstream.serve_forever, daemon=True)
    worker.start()
    try:
        port = int(upstream.server_address[1])
        with TlsTerminator(make_leaf(test_ca), scratch_dir, port) as front:
            context = ssl.create_default_context()
            context.load_verify_locations(cadata=test_ca.pem)
            with httpx.Client(verify=context, timeout=5.0) as client:
                resp = client.post(front.origin + "/x", content=b"y" * 50_000)
            assert resp.status_code == 200 and len(resp.content) == 70_000
    finally:
        upstream.shutdown()
        upstream.server_close()
        worker.join(timeout=10)


def test_no_siem_module_can_disable_verification() -> None:
    import code_indexer.server.services.siem_delivery as pkg

    root = Path(pkg.__file__).parent
    for source in root.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        for needle in ("verify=False", "CERT_NONE", "check_hostname = False"):
            assert needle not in text, f"{source.name} contains {needle}"
