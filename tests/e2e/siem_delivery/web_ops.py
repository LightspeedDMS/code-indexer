"""The Web SIEM operator panels and actions, driven like a browser.

Front door only: a real Web login (signed session cookie), the CSRF token the
config page renders (the SIEM section's ``#siem-ops-csrf`` holder carries the
same token), the htmx partial GETs and the form POSTs under
``/admin/siem-delivery``.  Parsers read only the attributes the partials
render for this purpose (``data-*`` and the hidden run id).
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import httpx

from tests.fixtures.secops_sidecar.harness import SidecarCoordinates

if TYPE_CHECKING:
    from tests.e2e.siem_delivery.conftest import AttachedSidecar
    from tests.e2e.siem_delivery.front_door import FrontDoor
    from tests.e2e.siem_delivery.siem_api import SiemDelivery

BASE = "/admin/siem-delivery"
HTTP_TIMEOUT_SECONDS = 60.0
_CSRF = re.compile(r'name="csrf_token"\s+value="([^"]+)"')
_IDS = re.compile(r'data-product-log-id="([^"]+)"')
_RUN_ID = re.compile(r'name="canary_run_id" value="([^"]+)"')
_ARMED = re.compile(r'id="siem-ops-armed" data-armed="(true|false)"')
_HALTED = re.compile(r'id="siem-ops-halted-batch" data-batch-id="([^"]+)"')
_QUARANTINED = re.compile(r'data-event-uuid="([^"]+)"')
_STRANDED = re.compile(r'<tr data-destination-key="([^"]+)"')


def localhost_endpoint(coords: SidecarCoordinates) -> str:
    """The same sidecar through ``localhost``: a NEW destination key (the key
    hashes the endpoint text) that reaches the one parent the sidecar serves."""
    return f"http://localhost:{coords.ingest_port}"


def configured_key(delivery: "SiemDelivery") -> str:
    return str(delivery.stats()["capture"]["configured_destination_key"])


CLEARED = {
    "enabled": "false",
    "region": "",
    "harness_endpoint": "",
    "project_id": "",
    "location": "",
    "instance_id": "",
    "source_instance_label": "",
}


def _clear_destination(delivery: "SiemDelivery") -> None:
    """No destination at all (so no credential probe runs) before the stored
    key's token URI is swapped."""
    from tests.e2e.siem_delivery.siem_api import DELIVERY_TIMEOUT

    delivery.save_config(CLEARED)
    delivery.wait_stats(
        lambda s: s["capture"]["configured_destination_key"] is None,
        DELIVERY_TIMEOUT,
        "the cleared configuration to be applied",
    )


def switch_destination(delivery: "SiemDelivery", endpoint: str) -> str:
    """Save the sidecar destination under *endpoint*; the new key once applied."""
    from tests.e2e.siem_delivery.siem_api import DELIVERY_TIMEOUT

    before = configured_key(delivery)
    coords = delivery.sidecar.coords
    # A harness destination mints tokens at ITS origin: store the same key
    # with that token URI (same identity; validated, audited, write-only).
    key = {**delivery.sidecar.read_key_file(), "token_uri": endpoint + "/token"}
    _clear_destination(delivery)
    stored = delivery.paste_credential(json.dumps(key))
    assert stored.status_code == 200, f"credential refused: {stored.status_code}"
    delivery.save_config(
        {
            "enabled": "true",
            "harness_endpoint": endpoint,
            "api_version": coords.api_version,
            "project_id": coords.project,
            "location": coords.location,
            "instance_id": coords.instance,
            "source_instance_label": "phase7-e2e",
        }
    )
    stats = delivery.wait_stats(
        lambda s: s["capture"]["configured_destination_key"] not in (None, before),
        DELIVERY_TIMEOUT,
        "the new destination to be applied",
    )
    return str(stats["capture"]["configured_destination_key"])


def original_state(delivery: "SiemDelivery") -> Tuple[str, str]:
    """(configured destination key, stored credential key id), recorded
    BEFORE a scenario mutates either."""
    stats = delivery.stats()
    return (
        str(stats["capture"]["configured_destination_key"]),
        str((stats.get("credential") or {}).get("private_key_id")),
    )


def restore_destination(
    delivery: "SiemDelivery",
    original: Tuple[str, str],
    temporary_key: Optional[str],
) -> None:
    """Back to the recorded destination and key (works after a half-done
    switch), verified; then the temporary destination's rows are abandoned."""
    _clear_destination(delivery)
    stored = delivery.upload_credential(delivery.sidecar.key_file_path.read_bytes())
    assert stored.status_code == 200, f"credential refused: {stored.status_code}"
    delivery.arm()
    assert original_state(delivery) == original, "the original state was not restored"
    if temporary_key is not None:
        leftover = delivery.abandon(temporary_key)
        assert leftover.status_code == 200, leftover.text


def strand_one_login(
    delivery: "SiemDelivery", door: "FrontDoor", sidecar: "AttachedSidecar"
) -> str:
    """Hold delivery (a duplicate-response halt) and capture one login for the
    configured destination; returns its event uuid (still pending)."""
    from tests.e2e.siem_delivery.front_door import unique_name
    from tests.e2e.siem_delivery.siem_api import (
        DELIVERY_TIMEOUT,
        arm_fault_after_drain,
        halt_class,
    )

    user = unique_name("siem-web-ops")
    door.create_user(user)
    arm_fault_after_drain(
        delivery, sidecar, "/_control/faults", {"mode": "status", "code": 409}
    )
    door.rest_login_failure(user)
    delivery.wait_stats(
        lambda s: halt_class(s) == "duplicate_response",
        DELIVERY_TIMEOUT,
        "the holding duplicate-response halt",
    )
    return door.rest_login_success(user).event_uuid


class WebSession:
    """One browser: a logged-in Web session on the server under test."""

    def __init__(
        self, base_url: str, username: str, password: str, *, admin: bool = True
    ) -> None:
        self.http = httpx.Client(
            base_url=base_url, timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False
        )
        login_csrf = _first(_CSRF, self.http.get("/login").text, "login csrf")
        login = self.http.post(
            "/login",
            data={"username": username, "password": password, "csrf_token": login_csrf},
        )
        assert login.status_code == 303, f"web login failed: {login.status_code}"
        self.csrf = login_csrf
        if admin:
            page = self.http.get("/admin/config")
            assert page.status_code == 200, f"config page: {page.status_code}"
            assert 'id="siem-ops-csrf"' in page.text, "no SIEM operations panel"
            self.csrf = _first(_CSRF, page.text, "config page csrf")

    def __enter__(self) -> "WebSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self.http.close()

    def get(self, path: str) -> httpx.Response:
        return self.http.get(path)

    def arming(self) -> str:
        return _ok(self.get(f"{BASE}/partials/arming"), "arming panel")

    def recovery(self, **cursors: str) -> str:
        resp = self.http.get(f"{BASE}/partials/recovery", params=cursors)
        return _ok(resp, "recovery panel")

    def dialog(self, destination_key: str) -> str:
        path = f"{BASE}/partials/destinations/{destination_key}/abandon"
        return _ok(self.get(path), "destination dialog")

    def act(
        self,
        path: str,
        fields: Optional[Dict[str, Any]] = None,
        csrf: Optional[str] = None,
    ) -> httpx.Response:
        """POST one action form (with this session's CSRF token by default)."""
        data = {"csrf_token": self.csrf if csrf is None else csrf, **(fields or {})}
        return self.http.post(f"{BASE}/{path}", data=data)

    def elevate(self, totp_code: str) -> httpx.Response:
        """The modal's call: ``/auth/elevate-ajax`` on this Web session."""
        return self.http.post("/auth/elevate-ajax", data={"totp_code": totp_code})


def canary_ids(arming_html: str) -> List[str]:
    return _IDS.findall(arming_html)


def canary_run_id(arming_html: str) -> str:
    return _first(_RUN_ID, arming_html, "canary run id")


def is_armed(arming_html: str) -> bool:
    return _first(_ARMED, arming_html, "ARMED row") == "true"


def halted_batch_id(recovery_html: str) -> Optional[str]:
    found = _HALTED.search(recovery_html)
    return found.group(1) if found else None


def quarantined_uuids(recovery_html: str) -> List[str]:
    return _QUARANTINED.findall(recovery_html)


def stranded_keys(recovery_html: str) -> List[str]:
    return _STRANDED.findall(recovery_html)


def text_of(html: str) -> str:
    """Visible text, whitespace-collapsed (for outcome-line assertions)."""
    from html import unescape

    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def _ok(resp: httpx.Response, what: str) -> str:
    assert resp.status_code == 200, f"{what}: {resp.status_code} {resp.text[:200]}"
    return resp.text


def _first(pattern: "re.Pattern[str]", text: str, what: str) -> str:
    found = pattern.search(text)
    assert found is not None, f"could not find the {what} in the page"
    return found.group(1)
