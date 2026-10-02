"""Destination-enforcing transport and the google-auth sender, against the
real SecOps sidecar."""

from __future__ import annotations

import dataclasses
import json
import socket
from typing import Iterator, List, Optional, Tuple

import pytest

from code_indexer.server.fault_injection.http_client_factory import HttpClientFactory
from code_indexer.server.services.siem_delivery.destination import (
    SECOPS_TOKEN_URI,
    resolve_destination,
)
from code_indexer.server.services.siem_delivery.credential import StoredCredential
from code_indexer.server.services.siem_delivery.sender import (
    CredentialError,
    CredentialProvider,
    ProbeResult,
    send_batch,
)
from code_indexer.server.services.siem_delivery.transport import (
    SiemDestinationNotAllowed,
    guarded_send,
    request_allowed,
)
from tests.fixtures.secops_sidecar.harness import SidecarHandle

from .conftest import (
    harness_destination,
    harness_section,
    sidecar_loader,
    sidecar_provider,
)


@pytest.fixture()
def idle_listener() -> Iterator[Tuple[socket.socket, int]]:
    """A loopback listener that must never receive a connection."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    sock.setblocking(False)
    try:
        yield sock, int(sock.getsockname()[1])
    finally:
        sock.close()


def _assert_never_connected(sock: socket.socket) -> None:
    with pytest.raises(BlockingIOError):
        sock.accept()


def test_probe_ok_mints_a_token_from_the_sidecar(
    siem_sidecar: SidecarHandle, http_factory: HttpClientFactory
) -> None:
    provider = sidecar_provider(http_factory, siem_sidecar, token_timeout=10.0)
    dest = harness_destination(siem_sidecar)
    assert provider.probe(dest) is ProbeResult.OK
    assert provider.token(dest)


@pytest.mark.parametrize(
    "variant",
    [
        "http://localhost:{port}/token",
        "http://127.0.0.1:{other}/token",
        "http://127.0.0.1:{port}/token?x=1",
        "http://127.0.0.1:{port}/oauth/token",
        "http://127.0.0.1:{port}",
    ],
)
def test_harness_token_uri_must_be_exactly_endpoint_token(
    siem_sidecar: SidecarHandle,
    http_factory: HttpClientFactory,
    idle_listener: Tuple[socket.socket, int],
    variant: str,
) -> None:
    sock, other = idle_listener
    uri = variant.format(port=siem_sidecar.coords.ingest_port, other=other)
    dest = harness_destination(siem_sidecar)
    provider = CredentialProvider(
        http_factory, sidecar_loader(siem_sidecar, token_uri=uri), 5.0
    )
    assert provider.probe(dest) is ProbeResult.TOKEN_URI_NOT_ALLOWED
    _assert_never_connected(sock)


def test_deployed_mode_never_contacts_a_non_google_token_uri(
    siem_sidecar: SidecarHandle,
    http_factory: HttpClientFactory,
    idle_listener: Tuple[socket.socket, int],
) -> None:
    sock, port = idle_listener
    cfg = dataclasses.replace(
        harness_section(siem_sidecar), harness_endpoint="", region="us"
    )
    dest = resolve_destination(cfg, harness_active=False)
    assert dest is not None and dest.token_uri == SECOPS_TOKEN_URI
    loader = sidecar_loader(siem_sidecar, token_uri=f"http://127.0.0.1:{port}/token")
    assert CredentialProvider(http_factory, loader).probe(dest) is (
        ProbeResult.TOKEN_URI_NOT_ALLOWED
    )
    _assert_never_connected(sock)


def test_stored_credential_problems_have_distinct_probe_results(
    siem_sidecar: SidecarHandle, http_factory: HttpClientFactory
) -> None:
    dest = harness_destination(siem_sidecar)
    assert CredentialProvider(http_factory, lambda: None).probe(dest) is (
        ProbeResult.CREDENTIAL_MISSING
    )

    def _undecryptable() -> StoredCredential:
        raise CredentialError(ProbeResult.CREDENTIAL_INVALID)

    assert CredentialProvider(http_factory, _undecryptable).probe(dest) is (
        ProbeResult.CREDENTIAL_INVALID
    )
    wrong_type = sidecar_loader(siem_sidecar, type="authorized_user")
    assert CredentialProvider(http_factory, wrong_type).probe(dest) is (
        ProbeResult.CREDENTIAL_INVALID
    )
    bad_key = sidecar_loader(siem_sidecar, private_key="not a key")
    assert CredentialProvider(http_factory, bad_key).probe(dest) is (
        ProbeResult.CREDENTIAL_INVALID
    )


def test_signer_failure_is_credential_invalid(
    siem_sidecar: SidecarHandle, http_factory: HttpClientFactory
) -> None:
    """A stored key the RSA signer cannot use (here an EC key) must yield a
    probe result, never an exception escaping the loop."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    pem = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode("ascii")
    )
    loader = sidecar_loader(siem_sidecar, private_key=pem)
    provider = CredentialProvider(http_factory, loader, 5.0)
    assert provider.probe(harness_destination(siem_sidecar)) is (
        ProbeResult.CREDENTIAL_INVALID
    )


def test_removed_credential_stops_minting_at_once(
    siem_sidecar: SidecarHandle, http_factory: HttpClientFactory
) -> None:
    """The stored key is read on every token request: a removal through
    another process takes effect here with no restart."""
    stored: List[Optional[StoredCredential]] = [
        StoredCredential("one", dict(siem_sidecar.read_key_file()))
    ]
    provider = CredentialProvider(http_factory, lambda: stored[0], 5.0)
    dest = harness_destination(siem_sidecar)
    assert provider.token(dest)
    stored[0] = None
    with pytest.raises(CredentialError) as exc:
        provider.token(dest)
    assert exc.value.result is ProbeResult.CREDENTIAL_MISSING


def test_rejected_and_unavailable_token_endpoint(
    siem_sidecar: SidecarHandle, http_factory: HttpClientFactory
) -> None:
    dest = harness_destination(siem_sidecar)
    control = siem_sidecar.control
    assert control.post("/_control/token-faults", {"mode": "reject"}).status_code == 200
    assert sidecar_provider(http_factory, siem_sidecar).probe(dest) is (
        ProbeResult.TOKEN_REJECTED
    )
    assert control.post("/_control/outage", {"mode": "refuse"}).status_code == 200
    try:
        assert sidecar_provider(http_factory, siem_sidecar, 2.0).probe(dest) is (
            ProbeResult.TOKEN_ENDPOINT_UNREACHABLE
        )
    finally:
        siem_sidecar.control.post("/_control/outage", {"mode": "end"})


def test_send_is_byte_exact_and_classified(
    siem_sidecar: SidecarHandle, http_factory: HttpClientFactory
) -> None:
    from code_indexer.server.services.siem_delivery.canary import synthetic_events
    from code_indexer.server.services.siem_delivery.projection import project
    from code_indexer.server.services.siem_delivery.udm import (
        UdmRow,
        build_udm,
        canonical_json,
        envelope,
    )

    event = synthetic_events("r")[0]
    payload, _ = project(event)
    assert payload is not None
    udm = build_udm(
        UdmRow(event.event_uuid, event.action_type, json.loads(payload), "lbl")
    )
    body = envelope([canonical_json(udm)])
    dest = harness_destination(siem_sidecar)
    token = sidecar_provider(http_factory, siem_sidecar).token(dest)
    result = send_batch(http_factory, dest, token, body, event_count=1, timeout=10.0)
    assert result.cls == "accepted"
    requests = siem_sidecar.control.get("/_control/requests").json()["requests"]
    assert len(requests) == 1
    seq = requests[0]["seq"]
    assert siem_sidecar.control.get(f"/_control/requests/{seq}/body").content == body


def test_redirect_is_never_followed(
    siem_sidecar: SidecarHandle,
    http_factory: HttpClientFactory,
    idle_listener: Tuple[socket.socket, int],
) -> None:
    sock, port = idle_listener
    siem_sidecar.control.post(
        "/_control/faults",
        {"mode": "redirect", "code": 307, "location": f"http://127.0.0.1:{port}/x"},
    )
    dest = harness_destination(siem_sidecar)
    token = sidecar_provider(http_factory, siem_sidecar).token(dest)
    result = send_batch(
        http_factory,
        dest,
        token,
        b'{"inlineSource":{"events":[]}}',
        event_count=1,
        timeout=5.0,
    )
    assert result.cls == "unclassified"
    _assert_never_connected(sock)


def test_transport_allowlist(siem_sidecar: SidecarHandle) -> None:
    dest = harness_destination(siem_sidecar)
    assert request_allowed("POST", dest.token_uri, dest)
    assert request_allowed("POST", dest.origin + dest.import_path, dest)
    assert not request_allowed("GET", dest.token_uri, dest)
    assert not request_allowed("POST", dest.origin + dest.import_path + "?x=1", dest)
    assert not request_allowed("POST", dest.origin + "/v1/other", dest)


def test_send_to_unlisted_url_raises_before_connecting(
    siem_sidecar: SidecarHandle,
    http_factory: HttpClientFactory,
    idle_listener: Tuple[socket.socket, int],
) -> None:
    sock, port = idle_listener
    dest = harness_destination(siem_sidecar)
    for url in (
        f"http://127.0.0.1:{port}{dest.import_path}",
        f"http://u:p@127.0.0.1:{siem_sidecar.coords.ingest_port}{dest.import_path}",
    ):
        with pytest.raises(SiemDestinationNotAllowed) as exc:
            guarded_send(http_factory, dest, "POST", url, b"{}", {}, 2.0)
        assert str(port) not in str(exc.value)
    _assert_never_connected(sock)
