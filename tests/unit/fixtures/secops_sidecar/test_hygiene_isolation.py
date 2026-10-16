"""Hygiene (no credential leaks), network isolation, reset and restart survival."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from tests.fixtures.secops_sidecar.client import mint_token, post_import
from tests.fixtures.secops_sidecar.harness import REPO_ROOT, SidecarHandle
from tests.fixtures.secops_sidecar.samples import batch_body, user_login_udm

CLIENT_TIMEOUT_SECONDS = 30


def _accept(sidecar: SidecarHandle, token: str, *ids: str) -> None:
    body = batch_body([user_login_udm(i) for i in ids])
    assert post_import(sidecar.coords, token, body).status_code == 200


def test_counts_report_store_sizes_and_zero_outbound_connections(
    sidecar: SidecarHandle,
) -> None:
    token = mint_token(sidecar.coords, sidecar.read_key_file())
    _accept(sidecar, token, "c-1", "c-2")
    sidecar.control.post(
        "/_control/faults", {"mode": "status", "code": 500, "count": 2}
    )
    sidecar.control.post("/_control/visibility", {"hide_event_type": "USER_LOGIN"})
    counts = sidecar.control.get("/_control/counts").json()
    assert counts == {
        "received_count": 2,
        "request_count": 1,
        "issued_token_count": 1,
        "queued_fault_count": 1,
        "hide_rule_count": 1,
        "outbound_connects": 0,
    }


def test_no_control_response_or_log_line_exposes_credentials(
    sidecar: SidecarHandle,
) -> None:
    key = sidecar.read_key_file()
    token = mint_token(sidecar.coords, key)
    _accept(sidecar, token, "h-1")
    post_import(
        sidecar.coords, "Bearer-looking-but-wrong", batch_body([user_login_udm("h-2")])
    )
    seq = sidecar.control.get("/_control/requests").json()["requests"][0]["seq"]
    texts = [
        sidecar.control.get(path, params=params).text
        for path, params in [
            ("/_control/health", None),
            ("/_control/counts", None),
            ("/_control/received", None),
            ("/_control/requests", None),
            (f"/_control/requests/{seq}/body", None),
            ("/_control/search", {"product_log_id": "h-1"}),
            ("/_control/faults", None),
            ("/_control/config", None),
        ]
    ]
    texts.append(sidecar.log_path.read_text("utf-8"))
    for text in texts:
        assert token not in text
        assert "Bearer-looking-but-wrong" not in text
        assert "PRIVATE KEY" not in text
        assert key["private_key_id"] not in text


_CLIENT_POST_SCRIPT = """
import json, sys, time
from tests.fixtures.secops_sidecar.client import mint_token, post_import
from tests.fixtures.secops_sidecar.harness import SidecarCoordinates
from tests.fixtures.secops_sidecar.samples import batch_body, user_login_udm
coords = SidecarCoordinates(int(sys.argv[1]), int(sys.argv[2]))
key = json.load(open(sys.argv[3]))
token = mint_token(coords, key)
assert post_import(coords, token, batch_body([user_login_udm("crash-1")])).status_code == 200
print("POSTED", flush=True)
time.sleep(600)
"""

_CLIENT_READ_SCRIPT = """
import sys, httpx
print(httpx.get(sys.argv[1] + "/_control/received", timeout=30).text)
"""


def test_accepted_batch_survives_a_sigkilled_client_process(
    sidecar: SidecarHandle,
) -> None:
    coords = sidecar.coords
    args = [str(coords.ingest_port), str(coords.control_port)]
    args.append(str(sidecar.key_material.key_file_path))
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    client = subprocess.Popen(
        [sys.executable, "-c", _CLIENT_POST_SCRIPT, *args],
        cwd=str(REPO_ROOT), env=env, stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        assert client.stdout is not None
        assert client.stdout.readline().strip() == "POSTED"
    finally:
        client.send_signal(signal.SIGKILL)
        client.wait(timeout=CLIENT_TIMEOUT_SECONDS)
        if client.stdout is not None:
            client.stdout.close()
    reader = subprocess.run(
        [sys.executable, "-c", _CLIENT_READ_SCRIPT, coords.control_url],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
        timeout=CLIENT_TIMEOUT_SECONDS, check=True,
    )  # fmt: skip
    events = json.loads(reader.stdout)["events"]
    assert [e["udm"]["metadata"]["productLogId"] for e in events] == ["crash-1"]


def test_reset_clears_every_kind_of_state(sidecar: SidecarHandle) -> None:
    token = mint_token(sidecar.coords, sidecar.read_key_file())
    _accept(sidecar, token, "r-1", "r-2", "r-3", "r-4", "r-5")
    sidecar.control.post("/_control/faults", {"mode": "reject_request"})
    sidecar.control.post("/_control/token-faults", {"mode": "reject"})
    sidecar.control.post("/_control/visibility", {"hide_product_log_id": "r-1"})
    sidecar.control.post("/_control/config", {"duplicate_mode": "already_exists"})
    sidecar.control.reset()
    counts = sidecar.control.get("/_control/counts").json()
    assert counts["received_count"] == 0 and counts["request_count"] == 0
    assert counts["queued_fault_count"] == 0 and counts["hide_rule_count"] == 0
    assert counts["issued_token_count"] == 0
    assert sidecar.control.get("/_control/config").json() == {
        "duplicate_mode": "ok_noop"
    }
    assert sidecar.control.get("/_control/faults").json()["queued"] == []
    assert (
        post_import(
            sidecar.coords, token, batch_body([user_login_udm("x")])
        ).status_code
        == 401
    )


def test_reset_keeping_tokens_clears_state_but_issued_tokens_stay_valid(
    sidecar: SidecarHandle,
) -> None:
    token = mint_token(sidecar.coords, sidecar.read_key_file())
    _accept(sidecar, token, "k-1")
    sidecar.control.post("/_control/faults", {"mode": "reject_request"})
    sidecar.control.post("/_control/visibility", {"hide_product_log_id": "k-2"})
    sidecar.control.post("/_control/config", {"duplicate_mode": "already_exists"})
    sidecar.control.post("/_control/outage", {"mode": "refuse"})
    resp = sidecar.control.post("/_control/reset", {"keep_tokens": True})
    assert resp.status_code == 200
    counts = sidecar.control.get("/_control/counts").json()
    assert counts["received_count"] == 0 and counts["request_count"] == 0
    assert counts["queued_fault_count"] == 0 and counts["hide_rule_count"] == 0
    assert counts["issued_token_count"] == 1
    assert sidecar.control.get("/_control/config").json() == {
        "duplicate_mode": "ok_noop"
    }
    _accept(sidecar, token, "k-2")  # same token, outage ended, no fault left
    assert sidecar.control.post("/_control/reset", {"keep": 1}).status_code == 400


def test_keygen_cli_writes_key_material_for_an_ingest_port(
    sidecar_scratch_dir: Path,
) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "tests.fixtures.secops_sidecar.harness", "keygen",
         "--dir", str(sidecar_scratch_dir), "--ingest-port", "8902"],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
        timeout=CLIENT_TIMEOUT_SECONDS, check=True,
    )  # fmt: skip
    assert "PRIVATE KEY" not in result.stdout + result.stderr
    key = json.loads((sidecar_scratch_dir / "sa-key.json").read_text("utf-8"))
    assert key["token_uri"] == "http://127.0.0.1:8902/token"
    assert (
        (sidecar_scratch_dir / "pub.pem")
        .read_text("ascii")
        .startswith("-----BEGIN PUBLIC")
    )
    assert oct((sidecar_scratch_dir / "sa-key.json").stat().st_mode & 0o777) == "0o600"
