"""The SecOps service-account key is configured ONLY through the Web UI:
uploaded or pasted, validated, stored encrypted, never returned.

Every call goes through the real front door (the elevated Web Config forms
and the admin REST stats); the server never reads a key file.
"""

from __future__ import annotations

import json

from tests.e2e.siem_delivery.conftest import AttachedSidecar
from tests.e2e.siem_delivery.siem_api import (
    DELIVERY_TIMEOUT,
    SiemDelivery,
    fleet,
    validation_error,
)


def _pem_line(sidecar: AttachedSidecar) -> str:
    return str(sidecar.read_key_file()["private_key"].splitlines()[1])


def test_key_is_write_only_identity_shown_and_invalid_keys_rejected(
    delivery: SiemDelivery, sidecar: AttachedSidecar
) -> None:
    delivery.arm()  # uploads the sidecar key file through the Web form
    key = sidecar.read_key_file()
    stats = delivery.stats()
    assert stats["credential"]["client_email"] == key["client_email"]
    assert stats["credential"]["private_key_id"] == key["private_key_id"]
    assert stats["credential"]["set_by"] == delivery.config.admin_user
    page = delivery.config_page()
    assert key["client_email"] in page and key["private_key_id"] in page
    assert "service_account_key_path" not in page
    assert _pem_line(sidecar) not in page and "BEGIN PRIVATE KEY" not in page
    assert _pem_line(sidecar) not in json.dumps(stats)

    rejected = {
        "not json": "{not json",
        "wrong type": json.dumps({**key, "type": "authorized_user"}),
        "token uri": json.dumps({**key, "token_uri": "https://example.com/token"}),
        "private key": json.dumps({**key, "private_key": "not a key"}),
    }
    for why, text in rejected.items():
        resp = delivery.paste_credential(text)
        assert resp.status_code == 400, f"{why}: answered {resp.status_code}"
        message = validation_error(resp.text)
        assert message.startswith("SIEM Delivery:"), f"{why}: {message!r}"
        assert _pem_line(sidecar) not in resp.text
    oversize = delivery.upload_credential(b"{" + b" " * (600 * 1024) + b"}")
    assert oversize.status_code == 413, oversize.status_code
    assert delivery.stats()["credential"] == stats["credential"], "a reject changed it"

    replaced = delivery.paste_credential(json.dumps(key))
    assert replaced.status_code == 200, validation_error(replaced.text)
    assert "credential replaced" in replaced.text
    assert _pem_line(sidecar) not in replaced.text

    # The change is a SIEM self-report: delivered, carrying identity only.
    delivery.wait_stats(
        lambda s: fleet(s, "pending") == 0, DELIVERY_TIMEOUT, "the queue to drain"
    )
    received = json.dumps(sidecar.control.get("/_control/received").json())
    assert "siem_credential_changed" in received
    assert _pem_line(sidecar) not in received
