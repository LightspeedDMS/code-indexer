"""The optional additional trusted CA, through the real front door (the
elevated Web Config forms and the audit-log REST read-back): shown by its
public identity, audited with its fingerprint, invalid bundles rejected, and
removable.  Independent of the destination state (runs after decommission).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from tests.e2e.siem_delivery.front_door import FrontDoor
from tests.e2e.siem_delivery.siem_api import SiemDelivery, validation_error
from tests.unit.server.siem.tls_fixtures import make_ca, make_leaf

CA_PATH = "/admin/config/siem_delivery/trusted_ca"
FP_KEY = "siem_delivery_config.trusted_ca_fingerprint"


def _fingerprint_changes(door: FrontDoor, after_id: int) -> List[Any]:
    found = []
    for row in door._audit_rows("config_changed"):
        if int(row["id"]) <= after_id:
            continue
        details: Dict[str, Any] = row.get("details") or {}
        if isinstance(details, str):
            details = json.loads(details)
        if FP_KEY in (details.get("values") or {}):
            found.append((int(row["id"]), details["values"][FP_KEY]))
    return [value for _id, value in sorted(found)]  # oldest first


def test_trusted_ca_is_validated_shown_audited_and_removable(
    delivery: SiemDelivery, door: FrontDoor
) -> None:
    ca = make_ca("Example Emulator CA")
    before = door.max_audit_id()
    for why, bad in {
        "a leaf": make_leaf(ca).pem,
        "expired": make_ca("Example Old CA", expired=True).pem,
        "not a CA": make_ca("Example Non CA", is_ca=False).pem,
    }.items():
        resp = delivery.web_post(CA_PATH, {"trusted_ca_pem": bad})
        assert resp.status_code == 400, f"{why}: {resp.status_code}"
        assert validation_error(resp.text).startswith("SIEM Delivery:"), why

    resp = delivery.web_post(
        CA_PATH,
        {},
        files={"trusted_ca_file": ("ca.pem", ca.pem.encode(), "application/x-pem")},
    )
    assert resp.status_code == 200, validation_error(resp.text)
    assert "trusted CA set" in resp.text
    page = delivery.config_page()
    assert "subject CN=Example Emulator CA" in page
    assert "issuer CN=Example Emulator CA" in page

    removed = delivery.web_post(CA_PATH + "/remove", {})
    assert removed.status_code == 200 and "trusted CA removed" in removed.text
    assert "none (default trust only)" in delivery.config_page()

    changes = _fingerprint_changes(door, before)
    assert len(changes) == 2, changes
    assert changes[0][0] == "" and len(changes[0][1]) == 64
    assert changes[1] == [changes[0][1], ""]
