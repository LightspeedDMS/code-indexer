"""Destination-enforcing transport and the google-auth sender, against the
real SecOps sidecar."""

from __future__ import annotations

import dataclasses
import json
import os
import socket
from pathlib import Path
from typing import Iterator, Tuple

import pytest

from code_indexer.server.fault_injection.http_client_factory import HttpClientFactory
from code_indexer.server.services.siem_delivery.destination import (
    SECOPS_TOKEN_URI,
    resolve_destination,
)
from code_indexer.server.services.siem_delivery.sender import (
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

from .conftest import harness_destination, harness_section


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


def _write_key(path: Path, sidecar: SidecarHandle, token_uri: str) -> str:
    doc = sidecar.read_key_file()
    doc["token_uri"] = token_uri
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def test_probe_ok_mints_a_token_from_the_sidecar(
    siem_sidecar: SidecarHandle, http_factory: HttpClientFactory
) -> None:
    provider = CredentialProvider(http_factory, token_timeout=10.0)
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
    scratch_dir: Path,
    idle_listener: Tuple[socket.socket, int],
    variant: str,
) -> None:
    sock, other = idle_listener
    uri = variant.format(port=siem_sidecar.coords.ingest_port, other=other)
    key_path = _write_key(scratch_dir / "key.json", siem_sidecar, uri)
    dest = harness_destination(siem_sidecar, service_account_key_path=key_path)
    provider = CredentialProvider(http_factory, token_timeout=5.0)
    assert provider.probe(dest) is ProbeResult.TOKEN_URI_NOT_ALLOWED
    _assert_never_connected(sock)


def test_deployed_mode_never_contacts_a_non_google_token_uri(
    siem_sidecar: SidecarHandle,
    http_factory: HttpClientFactory,
    scratch_dir: Path,
    idle_listener: Tuple[socket.socket, int],
) -> None:
    sock, port = idle_listener
    key_path = _write_key(
        scratch_dir / "key.json", siem_sidecar, f"http://127.0.0.1:{port}/token"
    )
    cfg = dataclasses.replace(
        harness_section(siem_sidecar, service_account_key_path=key_path),
        harness_endpoint="",
        region="us",
    )
    dest = resolve_destination(cfg, harness_active=False)
    assert dest is not None and dest.token_uri == SECOPS_TOKEN_URI
    assert CredentialProvider(http_factory).probe(dest) is (
        ProbeResult.TOKEN_URI_NOT_ALLOWED
    )
    _assert_never_connected(sock)


def test_key_file_problems_have_distinct_probe_results(
    siem_sidecar: SidecarHandle, http_factory: HttpClientFactory, scratch_dir: Path
) -> None:
    provider = CredentialProvider(http_factory)
    missing = harness_destination(
        siem_sidecar, service_account_key_path=str(scratch_dir / "absent.json")
    )
    assert provider.probe(missing) is ProbeResult.KEY_FILE_MISSING
    unreadable_path = scratch_dir / "unreadable.json"
    unreadable_path.write_text("{}", encoding="utf-8")
    os.chmod(unreadable_path, 0)
    unreadable = harness_destination(
        siem_sidecar, service_account_key_path=str(unreadable_path)
    )
    if os.geteuid() != 0:
        assert provider.probe(unreadable) is ProbeResult.KEY_FILE_UNREADABLE
    bad = scratch_dir / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    invalid = harness_destination(siem_sidecar, service_account_key_path=str(bad))
    assert provider.probe(invalid) is ProbeResult.KEY_FILE_INVALID


def test_rejected_and_unavailable_token_endpoint(
    siem_sidecar: SidecarHandle, http_factory: HttpClientFactory
) -> None:
    dest = harness_destination(siem_sidecar)
    control = siem_sidecar.control
    assert control.post("/_control/token-faults", {"mode": "reject"}).status_code == 200
    assert CredentialProvider(http_factory).probe(dest) is ProbeResult.TOKEN_REJECTED
    assert control.post("/_control/outage", {"mode": "refuse"}).status_code == 200
    try:
        assert CredentialProvider(http_factory, token_timeout=2.0).probe(dest) is (
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
    token = CredentialProvider(http_factory).token(dest)
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
    token = CredentialProvider(http_factory).token(dest)
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
