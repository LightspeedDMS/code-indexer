"""Client for the SIEM delivery front doors.

Surfaces (routers/siem_delivery_admin.py; actions need TOTP elevation when
elevation enforcement is on):
  POST /admin/config/siem_delivery                      Web Config section (admin form)
  GET  /api/admin/siem-delivery/stats                   fleet / capture / halt / local_process
  GET  /api/admin/siem-delivery/quarantine?limit=N      {"rows": [{"event_uuid", "action_type",
                                                         "destination_key", "quarantine_reason",
                                                         "quarantine_signature", "created_at"}]}
  POST /api/admin/siem-delivery/canary                  synthetic canary (one per mapping + fallback)
  POST /api/admin/siem-delivery/canary/confirm-visible  ids found via the sidecar's /_control/search
  POST /api/admin/siem-delivery/resume                  clear a systemic halt
  POST /api/admin/siem-delivery/batches/{id}/acknowledge  resolve a duplicate halt (delivered)
  POST /api/admin/siem-delivery/batches/{id}/rebatch    resolve a duplicate halt (re-send)
  POST /api/admin/siem-delivery/quarantine/requeue      {"event_uuids": [...]}
  POST /api/admin/siem-delivery/destinations/{key}/retarget | /abandon

Implemented response fields used here:
  stats["capture"]["state"]   inactive | awaiting canary | canary rejected |
                              awaiting visibility confirmation |
                              awaiting process readiness | armed
  stats["capture"]["status"]  human text ("canary accepted, N of M visible; not visible: ...")
  stats["halt"]["class"]      None or the class that halted; ["batch_id"] the held batch
  stats["fleet"]["pending"]   UNDELIVERED rows (status pending + batched), capped at 10,001;
                              ["quarantined"], ["delivered_total"],
                              ["resent_after_unknown_outcome"] (durable fleet counters)
  canary response             {"canary_run_id", "expected_product_log_ids", "result", ...}

The Phase 7 server runs with the non-production fault-injection gate ON, so
delivery uses its compressed HARNESS timing profile (1 s loop cycle, 10 s
halt probe, 30 s lease, 10 s DEGRADED backlog age).  The waits below are
upper bounds; every poll returns as soon as its condition holds.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from tests.e2e.server.conftest import AdminTokenProvider
from tests.e2e.siem_delivery.conftest import AttachedSidecar, SiemE2EConfig

POLL_SECONDS = 2.0
DELIVERY_TIMEOUT = float(os.environ.get("E2E_SIEM_DELIVERY_TIMEOUT", "180"))
# Harness profile: arming within a loop cycle, halt probe every 10 s, backlog
# DEGRADED after 10 s.  These bounded waits leave ample margin.
ARMING_TIMEOUT = float(os.environ.get("E2E_SIEM_ARMING_TIMEOUT", "120"))
DEGRADED_TIMEOUT = float(os.environ.get("E2E_SIEM_DEGRADED_TIMEOUT", "180"))
PROBE_TIMEOUT = float(os.environ.get("E2E_SIEM_PROBE_TIMEOUT", "180"))
# A crashed sender's batch lease (harness LEASE = 30 s) must expire before resend.
LEASE_TIMEOUT = float(os.environ.get("E2E_SIEM_LEASE_TIMEOUT", "240"))


Stats = Dict[str, Any]


def capture_state(stats: Stats) -> Any:
    return stats["capture"]["state"]


def halt_class(stats: Stats) -> Any:
    return stats["halt"]["class"]


def fleet(stats: Stats, key: str) -> int:
    return int(stats["fleet"][key])


def poll(check: Callable[[], Optional[Any]], timeout: float, what: str) -> Any:
    """Call *check* until it returns non-None, or fail after *timeout* s."""
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value is not None:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what}")
        time.sleep(POLL_SECONDS)


def search_sidecar(
    sidecar: AttachedSidecar, product_log_id: str
) -> List[Dict[str, Any]]:
    resp = sidecar.control.get(
        "/_control/search", params={"product_log_id": product_log_id}
    )
    assert resp.status_code == 200, resp.text
    results: List[Dict[str, Any]] = resp.json()["results"]
    return results


def request_body(sidecar: AttachedSidecar, seq: int) -> bytes:
    """The exact raw bytes the sidecar received for request *seq*."""
    resp = sidecar.control.get(f"/_control/requests/{seq}/body")
    assert resp.status_code == 200, f"no retained body for request {seq}"
    return resp.content


def requests_carrying(
    sidecar: AttachedSidecar, product_log_ids: List[str]
) -> List[Dict[str, Any]]:
    """Request-log records (arrival order) whose body holds any of the ids."""
    needles = [i.encode("utf-8") for i in product_log_ids]
    records: List[Dict[str, Any]] = sidecar.control.get("/_control/requests").json()[
        "requests"
    ]
    return [
        r for r in records if any(n in request_body(sidecar, r["seq"]) for n in needles)
    ]


def sidecar_fault(sidecar: AttachedSidecar, path: str, spec: Dict[str, Any]) -> None:
    """Script the sidecar (faults, config, outage) and require it to accept."""
    resp = sidecar.control.post(path, spec)
    assert resp.status_code == 200, f"sidecar {path} {spec} refused: {resp.text}"


def arm_fault_after_drain(
    delivery: "SiemDelivery",
    sidecar: AttachedSidecar,
    path: str,
    spec: Dict[str, Any],
) -> None:
    """Script a one-shot sidecar fault only once the server's queue is empty.

    Rows left pending by an earlier scenario (or by this scenario's own setup,
    e.g. a user_created event) would otherwise consume the fault meant for the
    scenario's own batch.
    """
    delivery.wait_stats(
        lambda s: fleet(s, "pending") == 0,
        DELIVERY_TIMEOUT,
        "the SIEM queue to drain before arming a fault",
    )
    sidecar_fault(sidecar, path, spec)


def wait_delivered(
    sidecar: AttachedSidecar, product_log_id: str, timeout: float = DELIVERY_TIMEOUT
) -> List[Dict[str, Any]]:
    """The visible sidecar events for *product_log_id* once any arrived."""
    found: List[Dict[str, Any]] = poll(
        lambda: search_sidecar(sidecar, product_log_id) or None,
        timeout,
        f"event {product_log_id} to be delivered to the sidecar",
    )
    return found


DELIVERY_ABSENT = "SIEM delivery is absent from this server"
QUARANTINE_LISTING_LIMIT = 1000
DETAIL_CHARS = 160  # how much of an unexpected response body a failure quotes
HTTP_TIMEOUT_SECONDS = 60.0
SOURCE_INSTANCE_LABEL = os.environ.get("E2E_SIEM_SOURCE_LABEL", "phase7-e2e")
CREDENTIAL_PATH = "/admin/config/siem_delivery/credential"
_CSRF = re.compile(r'name="csrf_token"\s+value="([^"]+)"')
_VALIDATION_ERROR = re.compile(r'class="validation-error">([^<]*)<')


def validation_error(page: str) -> str:
    """The section's rendered validation error, if any (never the key)."""
    found = _VALIDATION_ERROR.search(page)
    return found.group(1).strip() if found else ""


class SiemDelivery:
    """The SIEM delivery admin surfaces, via REST and the Web Config form."""

    def __init__(
        self,
        http: httpx.Client,
        admin: AdminTokenProvider,
        config: SiemE2EConfig,
        sidecar: AttachedSidecar,
    ) -> None:
        self.http, self.admin, self.config, self.sidecar = http, admin, config, sidecar

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": "Bearer " + self.admin.get_token()}

    def _require(self, resp: httpx.Response, surface: str) -> Dict[str, Any]:
        """The JSON body of a 200, else the explicit 'delivery is absent' failure."""
        if resp.status_code != 200:
            detail = re.sub(r"\s+", " ", resp.text)[:DETAIL_CHARS]
            raise AssertionError(
                f"{DELIVERY_ABSENT}: {surface} answered {resp.status_code} ({detail})"
            )
        body: Dict[str, Any] = resp.json()
        return body

    def web_post(
        self,
        path: str,
        fields: Dict[str, str],
        files: Optional[Dict[str, Tuple[str, bytes, str]]] = None,
    ) -> httpx.Response:
        """POST an admin Web Config form (fresh admin web session + CSRF)."""
        with httpx.Client(
            base_url=self.config.server_url, timeout=HTTP_TIMEOUT_SECONDS
        ) as web:
            login_csrf = _CSRF.search(web.get("/login").text)
            assert login_csrf, "login page has no csrf token"
            login = web.post(
                "/login",
                data={
                    "username": self.config.admin_user,
                    "password": self.config.admin_pass,
                    "csrf_token": login_csrf.group(1),
                },
            )
            assert login.status_code == 303, (
                f"admin web login failed: {login.status_code}"
            )
            page_csrf = _CSRF.search(web.get("/admin/config").text)
            assert page_csrf, "config page has no csrf token"
            return web.post(
                path,
                data={**fields, "csrf_token": page_csrf.group(1)},
                files=files,
            )

    def config_page(self) -> str:
        """The admin Web Config page HTML (fresh admin web session)."""
        with httpx.Client(
            base_url=self.config.server_url, timeout=HTTP_TIMEOUT_SECONDS
        ) as web:
            login_csrf = _CSRF.search(web.get("/login").text)
            assert login_csrf, "login page has no csrf token"
            web.post(
                "/login",
                data={
                    "username": self.config.admin_user,
                    "password": self.config.admin_pass,
                    "csrf_token": login_csrf.group(1),
                },
            )
            return web.get("/admin/config").text

    def upload_credential(self, key_bytes: bytes) -> httpx.Response:
        """Upload a service-account key FILE through the Web Config form."""
        return self.web_post(
            CREDENTIAL_PATH,
            {},
            files={
                "service_account_file": ("sa-key.json", key_bytes, "application/json")
            },
        )

    def paste_credential(self, key_text: str) -> httpx.Response:
        """Paste a service-account key through the Web Config form."""
        return self.web_post(CREDENTIAL_PATH, {"service_account_json": key_text})

    def remove_credential(self) -> httpx.Response:
        return self.web_post(CREDENTIAL_PATH + "/remove", {})

    def ensure_credential(self) -> None:
        """The sidecar's key is the stored credential (uploaded only when the
        stored identity differs, so scenarios do not churn it)."""
        key_bytes = self.sidecar.key_file_path.read_bytes()
        wanted = json.loads(key_bytes)["private_key_id"]
        current = self.stats().get("credential") or {}
        if current.get("private_key_id") == wanted:
            return
        resp = self.upload_credential(key_bytes)
        assert resp.status_code == 200, (
            f"credential upload refused: {resp.status_code} "
            f"{validation_error(resp.text)}"
        )
        stored = self.stats()["credential"]
        assert stored and stored["private_key_id"] == wanted

    def save_config(self, fields: Dict[str, str]) -> None:
        """Save the siem_delivery section through the elevated Web Config form."""
        resp = self.web_post("/admin/config/siem_delivery", fields)
        if resp.status_code != 200 or "Invalid section" in resp.text:
            found = re.search(r"Invalid section[^<]*", resp.text)
            detail = found.group(0) if found else resp.text[:DETAIL_CHARS]
            raise AssertionError(
                f"{DELIVERY_ABSENT}: POST /admin/config/siem_delivery answered "
                f"{resp.status_code} ({detail})"
            )

    def configure_harness_destination(self, enabled: bool = True) -> None:
        """Point delivery at the sidecar (harness mode, behind the gate).

        The key is uploaded FIRST, through the Web form, so no loop cycle
        ever probes a configured destination without a credential."""
        self.ensure_credential()
        coords = self.sidecar.coords
        self.save_config(
            {
                "enabled": "true" if enabled else "false",
                "harness_endpoint": coords.harness_endpoint,
                "api_version": coords.api_version,
                "project_id": coords.project,
                "location": coords.location,
                "instance_id": coords.instance,
                "source_instance_label": SOURCE_INSTANCE_LABEL,
            }
        )

    def stats(self) -> Stats:
        resp = self.http.get("/api/admin/siem-delivery/stats", headers=self._headers())
        return self._require(resp, "GET /api/admin/siem-delivery/stats")

    def run_canary(self) -> Tuple[str, List[str]]:
        resp = self.http.post(
            "/api/admin/siem-delivery/canary", headers=self._headers(), json={}
        )
        body = self._require(resp, "POST /api/admin/siem-delivery/canary")
        return str(body["canary_run_id"]), [
            str(i) for i in body["expected_product_log_ids"]
        ]

    def confirm_visible(self, run_id: str, visible_ids: List[str]) -> Dict[str, Any]:
        resp = self.http.post(
            "/api/admin/siem-delivery/canary/confirm-visible",
            headers=self._headers(),
            json={"canary_run_id": run_id, "visible_product_log_ids": visible_ids},
        )
        return self._require(
            resp, "POST /api/admin/siem-delivery/canary/confirm-visible"
        )

    def acknowledge_halted_batch(self) -> Dict[str, Any]:
        """Admin resolution of a duplicate_response halt (front door, audited).

        ASSUMPTION: stats["halt"]["batch_id"] names the halted batch.
        """
        batch_id = self.stats()["halt"]["batch_id"]
        resp = self.http.post(
            f"/api/admin/siem-delivery/batches/{batch_id}/acknowledge",
            headers=self._headers(),
            json={},
        )
        return self._require(
            resp, "POST /api/admin/siem-delivery/batches/{id}/acknowledge"
        )

    def resume(self) -> Dict[str, Any]:
        """Admin action: clear a systemic (non-duplicate) halt."""
        resp = self.http.post(
            "/api/admin/siem-delivery/resume", headers=self._headers(), json={}
        )
        return self._require(resp, "POST /api/admin/siem-delivery/resume")

    def quarantined_event_uuids(self) -> List[str]:
        """event_uuids of quarantined rows (ASSUMED CONTRACT, see module doc)."""
        resp = self.http.get(
            "/api/admin/siem-delivery/quarantine",
            headers=self._headers(),
            params={"limit": QUARANTINE_LISTING_LIMIT},
        )
        rows = self._require(resp, "GET /api/admin/siem-delivery/quarantine")["rows"]
        return [str(r["event_uuid"]) for r in rows]

    def abandon(self, destination_key: str) -> httpx.Response:
        """POST .../destinations/{key}/abandon (raw response: callers assert)."""
        return self.http.post(
            f"/api/admin/siem-delivery/destinations/{destination_key}/abandon",
            headers=self._headers(),
            json={},
        )

    def siem_health_reasons(self) -> List[str]:
        """The SIEM entries of /api/system/health's failure reasons."""
        resp = self.http.get("/api/system/health", headers=self._headers())
        assert resp.status_code == 200, resp.text[:DETAIL_CHARS]
        reasons = resp.json().get("failure_reasons") or []
        return [str(r) for r in reasons if "SIEM" in str(r)]

    def resolve_halt(self) -> None:
        """Clear whatever halt is present through the admin front door (bounded)."""
        current = halt_class(self.stats())
        if current is None:
            return
        if current == "duplicate_response":
            self.acknowledge_halted_batch()
        else:
            self.resume()
        self.wait_stats(
            lambda s: halt_class(s) is None, DELIVERY_TIMEOUT, "halt resolved"
        )

    def wait_stats(
        self, accept: Callable[[Stats], bool], timeout: float, what: str
    ) -> Stats:
        def _check() -> Optional[Stats]:
            current = self.stats()
            return current if accept(current) else None

        found: Stats = poll(_check, timeout, what)
        return found

    def arm(self) -> None:
        """Configure the sidecar destination, pass the canary gate, wait for armed."""
        self.configure_harness_destination()
        run_id, expected = self.run_canary()
        visible = [i for i in expected if search_sidecar(self.sidecar, i)]
        self.confirm_visible(run_id, visible)
        self.wait_stats(
            lambda s: capture_state(s) == "armed", ARMING_TIMEOUT, "capture armed"
        )
