"""Sidecar process and isolation: own process, loopback-only, clean stop."""

from __future__ import annotations

import os
import socket
import time

import psutil

from tests.fixtures.secops_sidecar.harness import SidecarHandle


def _port_is_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def test_starts_as_its_own_process_with_ready_line_and_health(
    secops_sidecar: SidecarHandle,
) -> None:
    assert secops_sidecar.process.pid != os.getpid()
    assert secops_sidecar.process.poll() is None
    assert secops_sidecar.ready_line == (
        f"SIDECAR READY ingest={secops_sidecar.coords.ingest_port} "
        f"control={secops_sidecar.coords.control_port}"
    )
    health = secops_sidecar.control.get("/_control/health")
    assert health.status_code == 200
    assert health.json() == {"ingest_listening": True, "received_count": 0}


def test_both_listeners_are_bound_to_loopback_only(
    secops_sidecar: SidecarHandle,
) -> None:
    proc = psutil.Process(secops_sidecar.process.pid)
    listening = [
        c for c in proc.connections(kind="inet") if c.status == psutil.CONN_LISTEN
    ]
    ports = sorted(c.laddr.port for c in listening)
    coords = secops_sidecar.coords
    assert ports == sorted([coords.ingest_port, coords.control_port])
    assert {c.laddr.ip for c in listening} == {"127.0.0.1"}


def test_stop_exits_cleanly_within_five_seconds_and_frees_both_ports(
    secops_sidecar: SidecarHandle,
) -> None:
    started = time.monotonic()
    secops_sidecar.stop()
    elapsed = time.monotonic() - started
    assert secops_sidecar.process.returncode == 0
    assert elapsed < 5.0
    assert _port_is_free(secops_sidecar.coords.ingest_port)
    assert _port_is_free(secops_sidecar.coords.control_port)


def test_harness_endpoint_and_token_uri_come_from_one_source(
    secops_sidecar: SidecarHandle,
) -> None:
    coords = secops_sidecar.coords
    assert coords.harness_endpoint == f"http://127.0.0.1:{coords.ingest_port}"
    assert coords.token_uri == coords.harness_endpoint + "/token"
    key = secops_sidecar.read_key_file()
    assert key["token_uri"] == coords.token_uri
    assert key["type"] == "service_account"
    assert key["client_email"] == "sidecar-test@example.com"
    assert key["project_id"] == coords.project
