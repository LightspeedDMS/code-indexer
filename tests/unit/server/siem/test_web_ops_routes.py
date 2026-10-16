"""The Web SIEM operator routes through the real app (SQLite AND
PostgreSQL, real SecOps sidecar, real auth): the read partials, the action
pipeline, and the scheduler-absent 503 on both doors."""

from __future__ import annotations

import inspect
from html import unescape
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from code_indexer.server.services.siem_delivery import admin, ops_documents

from .conftest import harness_destination
from .test_ops_documents import _halt_on
from .test_ops_pages import _destination, _seed_batch, _seed_queue
from .web_ops_harness import ADMIN, OpsEnv, build_app, make_scheduler

ARMING = "/admin/siem-delivery/partials/arming"
RECOVERY = "/admin/siem-delivery/partials/recovery"
STRANDED = "harness:0000000000000777"
NOT_RUNNING = "SIEM delivery is not running in this process: RuntimeError"


def test_arming_partial_renders_the_checklist(ops: OpsEnv) -> None:
    client, _ = ops.web(ops.users[ADMIN])  # reads need no elevation
    page = client.get(ARMING)
    assert page.status_code == 200, page.text[:300]
    assert harness_destination(ops.sidecar).key in page.text
    assert "ARMED" in page.text
    run = admin.run_canary(ops.scheduler, ADMIN)
    page = client.get(ARMING)
    for product_log_id in run["expected_product_log_ids"]:
        assert f'data-product-log-id="{product_log_id}"' in page.text
    search = (
        'metadata.vendor_name = "CIDX" AND additional.fields["correlation_id"] = '
        f'"canary-{run["canary_run_id"]}"'
    )
    assert search in unescape(page.text)


def test_recovery_partial_and_destination_dialog_render(ops: OpsEnv) -> None:
    _destination(ops.backend, STRANDED)
    _seed_queue(ops.backend, 3, dest=STRANDED)
    client, _ = ops.web(ops.users[ADMIN])
    page = client.get(RECOVERY)
    assert page.status_code == 200, page.text[:300]
    assert STRANDED in page.text and "project-777" in page.text
    dialog = client.get(
        f"/admin/siem-delivery/partials/destinations/{STRANDED}/abandon"
    )
    assert dialog.status_code == 200, dialog.text[:300]
    text = " ".join(unescape(dialog.text).split())
    for coordinate in ("us", "project-777", "instance-777"):
        assert coordinate in text
    assert "pending 3" in text and "IRREVERSIBLE" in text
    assert "currently queued; may grow until the action runs" in text.lower()


def test_scheduler_absent_is_503_on_both_doors(ops: OpsEnv) -> None:
    ops.app = build_app(None, "RuntimeError")
    admin_user = ops.users[ADMIN]
    rest = ops.rest(admin_user, elevated=True)
    stats = rest.get("/api/admin/siem-delivery/stats")
    assert (stats.status_code, stats.json()) == (503, {"detail": NOT_RUNNING})
    resumed = rest.post("/api/admin/siem-delivery/resume")
    assert (resumed.status_code, resumed.json()) == (503, {"detail": NOT_RUNNING})
    client, token = ops.web(admin_user, elevated=True)
    read = client.get(ARMING)
    assert read.status_code == 503 and NOT_RUNNING in read.text
    write = client.post("/admin/siem-delivery/resume", data={"csrf_token": token})
    assert write.status_code == 503 and NOT_RUNNING in write.text


def _prepare(ops: OpsEnv, action: str) -> Tuple[str, Dict[str, Any]]:
    """The state an action needs, and its (path, form fields)."""
    base = "/admin/siem-delivery"
    if action == "confirm_visible":
        run = admin.run_canary(ops.scheduler, ADMIN)
        fields = {
            "canary_run_id": run["canary_run_id"],
            "visible_id": run["expected_product_log_ids"],
        }
        return f"{base}/canary/confirm-visible", fields
    if action == "requeue":
        uuids = _seed_queue(ops.backend, 2, status="quarantined")
        return f"{base}/quarantine/requeue", {"event_uuid": uuids}
    if action in ("acknowledge", "rebatch"):
        key = harness_destination(ops.sidecar).key
        _seed_batch(ops.backend, "batch-halted", seconds=0, dest=key)
        _seed_queue(ops.backend, 3, dest=key, status="batched", batch_id="batch-halted")
        _halt_on(ops.backend, "batch-halted")
        return f"{base}/batches/batch-halted/{action}", {}
    if action in ("retarget", "abandon"):
        _destination(ops.backend, STRANDED)
        _seed_queue(ops.backend, 3, dest=STRANDED)
        fields = {"confirm_word": "ABANDON"} if action == "abandon" else {}
        return f"{base}/destinations/{STRANDED}/{action}", fields
    return f"{base}/{action}", {}


OUTCOMES = {
    "canary": "Canary run",
    "confirm_visible": "Confirmed 34 of 34",
    "resume": "resumed: false",
    "requeue": "Requeued 2 events",
    "acknowledge": "Acknowledged batch batch-halted (3 events",
    "rebatch": "Re-batched batch batch-halted (3 events",
    "retarget": "Retargeted 3 events",
    "abandon": "Abandoned 3 events",
}


@pytest.mark.parametrize("action", sorted(OUTCOMES))
def test_each_action_calls_the_shared_service(ops: OpsEnv, action: str) -> None:
    path, fields = _prepare(ops, action)
    client, token = ops.web(ops.users[ADMIN], elevated=True)
    ops.scheduler.calls = 0
    response = client.post(path, data={"csrf_token": token, **fields})
    assert response.status_code == 200, response.text[:400]
    assert response.headers["HX-Trigger"] == "siem-ops-changed"
    assert ops.scheduler.calls > 0
    assert OUTCOMES[action] in " ".join(unescape(response.text).split())


def test_refusals_keep_the_service_status_and_text(ops: OpsEnv) -> None:
    client, token = ops.web(ops.users[ADMIN], elevated=True)
    base = "/admin/siem-delivery"
    ack = client.post(
        f"{base}/batches/batch-none/acknowledge", data={"csrf_token": token}
    )
    assert ack.status_code == 409 and "HX-Trigger" not in ack.headers
    assert "batch is not the one halted by a duplicate response" in ack.text
    key = harness_destination(ops.sidecar).key
    own = client.post(
        f"{base}/destinations/{key}/abandon",
        data={"csrf_token": token, "confirm_word": "ABANDON"},
    )
    assert own.status_code == 409 and "HX-Trigger" not in own.headers
    assert "rows already target the configured destination" in own.text


def test_two_processes_over_one_store(ops: OpsEnv) -> None:
    other = make_scheduler(ops.backend, ops.config)
    app_b = build_app(other)
    admin_user = ops.users[ADMIN]
    client_a, token_a = ops.web(admin_user, elevated=True)
    canary = client_a.post("/admin/siem-delivery/canary", data={"csrf_token": token_a})
    assert canary.status_code == 200
    run = ops_documents.arming_document(ops.scheduler)["canary"]
    ops.app = app_b
    client_b, token_b = ops.web(admin_user, elevated=True)
    region = client_b.get(ARMING).text
    ids = [e["product_log_id"] for e in run["expected"]]
    assert all(f'data-product-log-id="{i}"' in region for i in ids)
    confirmed = client_b.post(
        "/admin/siem-delivery/canary/confirm-visible",
        data={"csrf_token": token_b, "canary_run_id": run["run_id"], "visible_id": ids},
    )
    assert (
        confirmed.status_code == 200
        and f"Confirmed {len(ids)} of {len(ids)}" in confirmed.text
    )
    ops.scheduler.run_cycle()
    other.run_cycle()
    assert 'data-armed="true"' in client_b.get(ARMING).text
    old_key = harness_destination(ops.sidecar).key
    ops.config.version = 2  # a save committed through A
    ops.config.section = {**ops.config.section, "instance_id": "instance-new"}
    region = " ".join(unescape(client_b.get(ARMING).text).split())
    assert "(config v2)" in region and old_key not in region
    assert "Awaiting canary" in region and 'data-armed="false"' in region
    assert other.view is not None and other.view.destination is not None
    assert other.view.destination.key == old_key  # B has not cycled


@pytest.mark.parametrize(
    "cursor",
    [
        "²",  # superscript two: str.isdigit() is True, int() refuses it
        "٣٤",  # Arabic-Indic digits
        "9" * 5000,  # beyond the int() conversion limit
        str(2**63),  # past the signed 64-bit id range
        "+5",
        "-5",
        " 5",
        "5 ",
    ],
)
def test_recovery_partial_refuses_every_malformed_quarantine_cursor(
    ops: OpsEnv, cursor: str
) -> None:
    client, _ = ops.web(ops.users[ADMIN])
    response = client.get(RECOVERY, params={"q_after": cursor})
    assert response.status_code == 400, response.text[:200]
    assert "invalid quarantine cursor" in response.text


def test_checklist_counts_only_unlisted_failing_processes(ops: OpsEnv) -> None:
    from code_indexer.server.services.siem_delivery import state_store as ss

    view = ops.scheduler.committed_view()
    assert view.destination is not None
    key, db = view.destination.key, ops.backend.db

    def _process(pid: str, result: str) -> None:
        ss.register_process(db, pid, node_id="solo", ttl_seconds=180)
        ss.record_probe(db, pid, destination_key=key, result=result)

    for i in range(200):  # 201 live with the scheduler's own; ONE failing
        _process(f"solo:{i:03d}:x", "credential_missing" if i == 199 else "ok")
    client, _ = ops.web(ops.users[ADMIN])
    text = " ".join(unescape(client.get(ARMING).text).split())
    assert "Processes ready 200 of 201" in text
    assert " more" not in text  # the failing one is listed: nothing hidden
    for i in range(205):  # now 205 failing: 200 listed, 5 not
        _process(f"solo:{i:03d}:x", "credential_missing")
    text = " ".join(unescape(client.get(ARMING).text).split())
    assert "and 5 more failing" in text
    on_pg = ops.backend.name == "postgres"
    assert ("Every node has a process" in text) is on_pg


GETS = {
    "/admin/siem-delivery/partials/arming",
    "/admin/siem-delivery/partials/recovery",
    "/admin/siem-delivery/partials/destinations/{destination_key}/abandon",
}
POSTS = {
    "/admin/siem-delivery/canary",
    "/admin/siem-delivery/canary/confirm-visible",
    "/admin/siem-delivery/resume",
    "/admin/siem-delivery/quarantine/requeue",
    "/admin/siem-delivery/batches/{batch_id}/acknowledge",
    "/admin/siem-delivery/batches/{batch_id}/rebatch",
    "/admin/siem-delivery/destinations/{destination_key}/retarget",
    "/admin/siem-delivery/destinations/{destination_key}/abandon",
}


def test_routes_are_mounted_in_the_real_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.routing import APIRoute

    from code_indexer.server.app import create_app
    from code_indexer.server.services.config_service import reset_config_service

    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    reset_config_service()
    try:
        app = create_app()
    finally:
        reset_config_service()
    routes = {
        (method, r.path): r.endpoint
        for r in app.routes
        if isinstance(r, APIRoute)
        for method in r.methods
    }
    for path in GETS:
        assert not inspect.iscoroutinefunction(routes[("GET", path)]), path
    for path in POSTS:
        assert inspect.iscoroutinefunction(routes[("POST", path)]), path


def test_config_section_loads_the_panels() -> None:
    from code_indexer.server.web.routes import templates

    html = templates.get_template("partials/siem_delivery_config.html").render(
        config={
            "siem_delivery": {},
            "siem_delivery_status": [],
            "siem_delivery_trusted_ca": [],
        },
        csrf_token="csrf-example",
        validation_errors={},
    )
    assert 'id="siem-ops-csrf"' in html and 'value="csrf-example"' in html
    for region in ("arming", "recovery"):
        assert f'hx-get="/admin/siem-delivery/partials/{region}"' in html
    assert html.count('hx-trigger="load, siem-ops-changed from:body"') == 2
    assert 'id="siem-ops-result"' in html and 'id="siem-ops-dialog"' in html
    assert "/admin/static/js/siem_delivery_ops.js" in html


def test_ops_script_contract() -> None:
    import code_indexer.server.web as web_pkg

    script = (
        Path(web_pkg.__file__).parent / "static" / "js" / "siem_delivery_ops.js"
    ).read_text(encoding="utf-8")
    assert "htmx:beforeSwap" in script and "'siem-ops-'" in script
    assert "xhr.status !== 401" in script and "text/html" in script
    assert "siem-ops-open-destination" in script
    assert "encodeURIComponent(key)" in script and "'/abandon'" in script
