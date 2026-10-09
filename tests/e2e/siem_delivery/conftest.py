"""Fixtures for Phase 7 (SIEM delivery against the mock SecOps sidecar).

Every value comes from the environment that ``e2e-automation.sh --phase 7``
sets: a live server with the non-production fault-injection gate ON, and the
sidecar on loopback (ingest 8902, control 8903 by default) started from the
same per-run key material the tests configure the server with.

Session fixtures:
  siem_config            -- SiemE2EConfig (env)
  siem_http              -- httpx.Client bound to the SIEM server
  siem_admin             -- auto-refreshing admin JWT provider
  sidecar                -- AttachedSidecar (coordinates + control client)
Autouse:
  _loopback_only_guard   -- any connect to a non-loopback address fails the run
  _fresh_sidecar         -- POST /_control/reset before every test
  _phase7_log_audit_gate -- post-phase log audit (front door), like Phase 4
"""

from __future__ import annotations

import ipaddress
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, List, Optional, Tuple

import httpx
import pytest

from tests.e2e.server.conftest import AdminTokenProvider
from tests.e2e.siem_delivery.log_allowlist import (
    PHASE7_LOG_ALLOWLIST,
    deliberately_killed_worker_allowlist,
)
from tests.e2e.siem_delivery.outage_probe_window import (
    EXCUSED_LOG_IDS,
    without_excused,
)
from tests.fixtures.secops_sidecar.harness import SidecarControl, SidecarCoordinates

if TYPE_CHECKING:
    from tests.e2e.siem_delivery.front_door import FrontDoor
    from tests.e2e.siem_delivery.restartable_server import RestartableServer
    from tests.e2e.siem_delivery.siem_api import SiemDelivery
    from tests.e2e.siem_delivery.web_ops import DestinationRestore

HTTP_TIMEOUT_SECONDS = 60.0


def _env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is not set; run via ./e2e-automation.sh --phase 7")
    return value


@dataclass(frozen=True)
class SiemE2EConfig:
    server_url: str
    admin_user: str
    admin_pass: str


@dataclass(frozen=True)
class AttachedSidecar:
    """The shell-started sidecar: where it listens, and its control API."""

    coords: SidecarCoordinates
    control: SidecarControl
    key_file_path: Path

    def read_key_file(self) -> Any:
        return json.loads(self.key_file_path.read_text("utf-8"))


@pytest.fixture(scope="session")
def siem_config() -> SiemE2EConfig:
    host, port = _env("E2E_SIEM_SERVER_HOST"), _env("E2E_SIEM_SERVER_PORT")
    return SiemE2EConfig(
        server_url=f"http://{host}:{port}",
        admin_user=_env("E2E_ADMIN_USER"),
        admin_pass=_env("E2E_ADMIN_PASS"),
    )


@pytest.fixture(scope="session")
def sidecar() -> AttachedSidecar:
    coords = SidecarCoordinates(
        ingest_port=int(_env("E2E_SECOPS_SIDECAR_PORT")),
        control_port=int(_env("E2E_SECOPS_SIDECAR_CONTROL_PORT")),
        project=_env("E2E_SECOPS_PROJECT"),
        location=_env("E2E_SECOPS_LOCATION"),
        instance=_env("E2E_SECOPS_INSTANCE"),
        api_version=_env("E2E_SECOPS_API_VERSION"),
    )
    key_file = Path(_env("E2E_SECOPS_SIDECAR_DIR")) / "sa-key.json"
    return AttachedSidecar(coords, SidecarControl(coords.control_url), key_file)


@pytest.fixture(scope="session")
def siem_http(siem_config: SiemE2EConfig) -> Iterator[httpx.Client]:
    with httpx.Client(
        base_url=siem_config.server_url, timeout=HTTP_TIMEOUT_SECONDS
    ) as c:
        yield c


def admin_provider_for(url: str, user: str, password: str) -> AdminTokenProvider:
    """An auto-refreshing admin JWT provider for the server at *url*."""

    def _login() -> Tuple[str, Any]:
        resp = httpx.post(
            f"{url}/auth/login",
            json={"username": user, "password": password},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        body = resp.json()
        return str(body["access_token"]), body.get("refresh_token")

    access, refresh = _login()
    return AdminTokenProvider(
        login_fn=_login, initial_access_token=access, initial_refresh_token=refresh
    )


@pytest.fixture(scope="session")
def siem_admin(siem_config: SiemE2EConfig) -> AdminTokenProvider:
    return admin_provider_for(
        siem_config.server_url, siem_config.admin_user, siem_config.admin_pass
    )


def _private_server_log_audit(
    server: "RestartableServer", watermark: int
) -> Optional[str]:
    """The Phase 7 log-audit gate, applied to a server the test owns.

    Its log store survives the SIGKILL/restart (same data dir), so one
    watermark taken after the first boot covers the whole scenario,
    including the restart.  Returns the failure message, or None.
    """
    from tests.e2e.log_audit_gate import (
        poll_until_stable_count,
        query_logs_via_mcp,
        run_log_audit_gate,
    )

    if not server.running:
        server.start()  # a failed scenario may leave it down: audit anyway
    admin = admin_provider_for(server.url, server.admin_user, server.admin_pass)
    with httpx.Client(base_url=server.url, timeout=HTTP_TIMEOUT_SECONDS) as http:
        poll_until_stable_count(
            count_fn=lambda: len(query_logs_via_mcp(http, admin.get_token())),
            max_attempts=10,
            sleep_seconds=0.3,
        )
        result = run_log_audit_gate(
            http,
            admin.get_token(),
            watermark_id=watermark,
            phase_name="Phase 7 (SIEM Delivery, restartable server)",
            # The watchdog reports a deliberate SIGKILL on restart: excused
            # only for the PIDs this test killed itself.
            extra_allowlist=PHASE7_LOG_ALLOWLIST
            + deliberately_killed_worker_allowlist(server.killed_pids),
        )
    return None if result.passed else result.failure_message()


@pytest.fixture()
def restartable_server(siem_config: SiemE2EConfig) -> Iterator["RestartableServer"]:
    """A started server this test owns (SIGKILL + restart); bounded close.

    Brought under the same post-phase log audit as the shared Phase 7
    server: new non-allowlisted ERROR/WARNING entries fail the fixture.
    """
    from tests.e2e.log_audit_gate import get_log_watermark
    from tests.e2e.siem_delivery.restartable_server import RestartableServer

    server = RestartableServer(siem_config.admin_user, siem_config.admin_pass)
    audit_failure: Optional[str] = None
    try:
        server.start()
        admin = admin_provider_for(server.url, server.admin_user, server.admin_pass)
        with httpx.Client(base_url=server.url, timeout=HTTP_TIMEOUT_SECONDS) as http:
            watermark = get_log_watermark(http, admin.get_token())
        yield server
        audit_failure = _private_server_log_audit(server, watermark)
    finally:
        server.close()
    if audit_failure is not None:
        raise AssertionError(audit_failure)


def _non_loopback_connect_hook(event: str, args: Tuple[Any, ...]) -> None:
    """Fail loudly if any SIEM test connects to a non-loopback address.

    The ``socket.connect`` audit event's args are ``(socket, address)``
    (verified on this interpreter); an AF_INET/AF_INET6 address is a tuple
    whose first item is the host, an AF_UNIX address is a path (local IPC).
    """
    if event != "socket.connect" or len(args) < 2 or not isinstance(args[1], tuple):
        return
    host = args[1][0]
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise PermissionError(f"SIEM E2E tests must only contact loopback, not {host!r}")


_GUARD_INSTALLED: List[bool] = []  # audit hooks are permanent: install exactly once


@pytest.fixture(scope="session", autouse=True)
def _loopback_only_guard() -> None:
    if not _GUARD_INSTALLED:
        sys.addaudithook(_non_loopback_connect_hook)
        _GUARD_INSTALLED.append(True)


@pytest.fixture(autouse=True)
def _fresh_sidecar(sidecar: AttachedSidecar) -> None:
    """Clear the sidecar's events, requests, faults, hide rules and outage.

    Issued tokens are KEPT: the server under test caches its SecOps access
    token across scenarios (as it would against a real tenant), so wiping
    tokens would make a later scenario fail on a stale credential instead of
    on what it tests.  Server-side delivery state is NOT reset between
    scenarios, so every scenario asserts only on its OWN event uuids, its own
    requests, and counter deltas it measured itself; one-shot faults are armed
    only after the queue drains (siem_api.arm_fault_after_drain), and the
    ``delivery`` fixture's teardown resolves any halt a scenario leaves.
    """
    sidecar.control.reset(keep_tokens=True)


@pytest.fixture()
def door(siem_http: httpx.Client, siem_admin: AdminTokenProvider) -> "FrontDoor":
    """Pilot-action drivers (REST / MCP / Web) for the scenarios."""
    from tests.e2e.siem_delivery.front_door import FrontDoor  # it imports this module

    return FrontDoor(siem_http, siem_admin)


@pytest.fixture()
def delivery(
    siem_http: httpx.Client,
    siem_admin: AdminTokenProvider,
    siem_config: SiemE2EConfig,
    sidecar: AttachedSidecar,
) -> Iterator["SiemDelivery"]:
    """The SIEM delivery admin surfaces; teardown resolves any halt left behind.

    Teardown runs on pass AND fail, so a scenario that dies after creating a
    halt cannot leave the shared server halted for later scenarios.  Every
    scenario starts unhalted (the previous teardown saw to it), so any halt
    found here is this scenario's.  A teardown problem is reported as its own
    pytest ERROR; it never replaces the test's own failure.
    """
    from tests.e2e.siem_delivery.siem_api import DELIVERY_ABSENT, SiemDelivery

    delivery = SiemDelivery(siem_http, siem_admin, siem_config, sidecar)
    yield delivery
    try:
        delivery.resolve_halt()
    except AssertionError as exc:
        if not str(exc).startswith(DELIVERY_ABSENT):
            raise
        # The capability is absent: no halt can exist, so nothing to resolve.


@pytest.fixture()
def destination_restore(delivery: "SiemDelivery") -> Iterator["DestinationRestore"]:
    """The destination and credential this scenario INHERITED, recorded
    before it runs (so before its first ``arm()``); teardown restores them on
    pass AND fail and abandons the destinations the scenario appended to
    ``created``.  A restore failure is its own pytest ERROR; it never
    replaces the test's own failure."""
    from tests.e2e.siem_delivery.web_ops import inherited_state, restore_destination

    inherited = inherited_state(delivery)
    yield inherited
    restore_destination(delivery, inherited)


@pytest.fixture(scope="session", autouse=True)
def _phase7_log_audit_gate(
    siem_http: httpx.Client, siem_admin: AdminTokenProvider
) -> Iterator[None]:
    """Watermark at phase start; fail on new non-allowlisted ERROR/WARNING."""
    from tests.e2e.log_audit_gate import (
        get_log_watermark,
        poll_until_stable_count,
        query_logs_via_mcp,
        run_log_audit_gate,
    )

    def _count() -> int:
        return len(query_logs_via_mcp(siem_http, siem_admin.get_token()))

    poll_until_stable_count(count_fn=_count, max_attempts=10, sleep_seconds=0.3)
    watermark = get_log_watermark(siem_http, siem_admin.get_token())
    yield
    poll_until_stable_count(count_fn=_count, max_attempts=10, sleep_seconds=0.3)
    result = run_log_audit_gate(
        siem_http,
        siem_admin.get_token(),
        watermark_id=watermark,
        phase_name="Phase 7 (SIEM Delivery)",
        extra_allowlist=PHASE7_LOG_ALLOWLIST,
    )
    # Only the exact rows test_03 proved were logged inside its own scripted
    # outage (outage_probe_window); never excused by text.
    result = without_excused(result, EXCUSED_LOG_IDS)
    if not result.passed:
        raise AssertionError(result.failure_message())
