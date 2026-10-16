"""Live delivery over TLS with an additional trusted CA, through the front door.

The mock receiver is put behind a loopback TLS front whose certificate is
issued by a test CA generated here.  The key (token URI on the TLS origin),
the CA and the destination are configured through the elevated Web forms;
the canary, arming and a real login then travel over TLS on BOTH legs (the
token exchange and ``events:import``), verified against the configured CA.
The scenario then decommissions what it configured (last of the phase).
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.e2e.siem_delivery.conftest import AttachedSidecar
from tests.e2e.siem_delivery.front_door import FrontDoor, unique_name
from tests.e2e.siem_delivery.siem_api import (
    ARMING_TIMEOUT,
    DEGRADED_TIMEOUT,
    DELIVERY_TIMEOUT,
    SOURCE_INSTANCE_LABEL,
    SiemDelivery,
    capture_state,
    poll,
    search_sidecar,
    validation_error,
    wait_delivered,
)
from tests.unit.server.siem.tls_fixtures import TlsTerminator, make_ca, make_leaf

CA_PATH = "/admin/config/siem_delivery/trusted_ca"
CLEARED = {
    "enabled": "false",
    "region": "",
    "harness_endpoint": "",
    "project_id": "",
    "location": "",
    "instance_id": "",
    "source_instance_label": "",
}


def test_delivery_over_tls_with_the_configured_ca(
    delivery: SiemDelivery,
    door: FrontDoor,
    sidecar: AttachedSidecar,
    tmp_path: Path,
) -> None:
    ca = make_ca("Example Receiver CA")
    with TlsTerminator(make_leaf(ca), tmp_path, sidecar.coords.ingest_port) as front:
        key = {**sidecar.read_key_file(), "token_uri": front.origin + "/token"}
        resp = delivery.paste_credential(json.dumps(key))
        assert resp.status_code == 200, validation_error(resp.text)
        resp = delivery.web_post(CA_PATH, {"trusted_ca_pem": ca.pem})
        assert resp.status_code == 200, validation_error(resp.text)
        coords = sidecar.coords
        delivery.save_config(
            {
                "enabled": "true",
                "harness_endpoint": front.origin,
                "api_version": coords.api_version,
                "project_id": coords.project,
                "location": coords.location,
                "instance_id": coords.instance,
                "source_instance_label": SOURCE_INSTANCE_LABEL,
            }
        )
        run_id, expected = delivery.run_canary()
        visible = [i for i in expected if search_sidecar(sidecar, i)]
        assert visible == expected
        delivery.confirm_visible(run_id, visible)
        delivery.wait_stats(
            lambda s: capture_state(s) == "armed", ARMING_TIMEOUT, "armed over TLS"
        )
        user = unique_name("siem-tls")
        door.create_user(user)
        login = door.rest_login_success(user)
        assert len(wait_delivered(sidecar, login.event_uuid)) == 1
        assert front.connections >= 2, "token and import must both use the TLS front"

        # decommission what this scenario configured
        tls_key = delivery.stats()["capture"]["configured_destination_key"]
        delivery.save_config(CLEARED)
        delivery.wait_stats(
            lambda s: s["capture"]["configured_destination_key"] is None,
            DELIVERY_TIMEOUT,
            "the cleared configuration",
        )
        assert delivery.abandon(tls_key).status_code == 200
        poll(
            lambda: True if delivery.siem_health_reasons() == [] else None,
            DEGRADED_TIMEOUT,
            "every SIEM health reason to clear",
        )
        assert delivery.web_post(CA_PATH + "/remove", {}).status_code == 200
        assert delivery.remove_credential().status_code == 200
