"""Security of the Web SIEM operator routes (SQLite AND PostgreSQL, real
auth, real scheduler with a call counter -- never a mocked service): the
admin / TOTP-elevation / CSRF matrix over all 11 routes, enforcement-OFF
parity with REST, bounded input, escaping, and the ABANDON confirmation."""

from __future__ import annotations

import time
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from html import unescape
from typing import Any, Dict, Iterator, List, Tuple
from unittest.mock import patch
from urllib.parse import urlencode

import pyotp
import pytest

from code_indexer.server.services.siem_delivery.stats import COUNT_CAP
from code_indexer.server.storage.json_column import parse_json_column

from .test_ops_pages import _destination, _seed_queue

from .web_ops_harness import ACTIONS, ADMIN, ADMIN_NO_TOTP, NORMAL, OpsEnv, twin_calls

KEY = "harness:0000000000000777"
GETS = [
    "/admin/siem-delivery/partials/arming",
    "/admin/siem-delivery/partials/recovery",
    f"/admin/siem-delivery/partials/destinations/{KEY}/abandon",
]
POSTS: List[Tuple[str, Dict[str, Any]]] = [
    ("/admin/siem-delivery/canary", {}),
    (
        "/admin/siem-delivery/canary/confirm-visible",
        {
            "canary_run_id": "run-x",
            "visible_id": ["00000000-0000-4000-8000-0000000000a1"],
        },
    ),
    ("/admin/siem-delivery/resume", {}),
    (
        "/admin/siem-delivery/quarantine/requeue",
        {"event_uuid": ["00000000-0000-4000-8000-0000000000b1"]},
    ),
    ("/admin/siem-delivery/batches/batch-x/acknowledge", {}),
    ("/admin/siem-delivery/batches/batch-x/rebatch", {}),
    (f"/admin/siem-delivery/destinations/{KEY}/retarget", {}),
    (f"/admin/siem-delivery/destinations/{KEY}/abandon", {"confirm_word": "ABANDON"}),
]


def _siem_audit_rows(ops: OpsEnv) -> int:
    return ops.backend.count(
        "SELECT COUNT(*) AS n FROM audit_logs WHERE action_type LIKE ?", ("siem_%",)
    )


def _form_post(client: Any, path: str, data: Dict[str, Any]) -> Any:
    """POST a urlencoded form exactly as a browser does (even when empty)."""
    return client.post(
        path,
        content=urlencode(data, doseq=True),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


def _assert_no_effect(ops: OpsEnv) -> None:
    assert ops.scheduler.calls == 0, "a service function ran"
    assert _siem_audit_rows(ops) == 0, "an audit row was written"


def test_anonymous_and_normal_users_are_refused_everywhere(ops: OpsEnv) -> None:
    from fastapi.testclient import TestClient

    anonymous = TestClient(ops.app)
    normal, token = ops.web(ops.users[NORMAL])
    for path in GETS:
        assert anonymous.get(path).status_code == 401, path
        refused = normal.get(path)
        assert refused.status_code == 403, path
        assert refused.json() == {"detail": "Admin access required"}
    for path, fields in POSTS:
        assert anonymous.post(path, data=fields).status_code == 401, path
        refused = normal.post(path, data={"csrf_token": token, **fields})
        assert refused.status_code == 403, path
        assert refused.json() == {"detail": "Admin access required"}
    _assert_no_effect(ops)


def test_writes_need_totp_and_an_elevation_window(ops: OpsEnv) -> None:
    no_totp, no_totp_token = ops.web(ops.users[ADMIN_NO_TOTP])
    enrolled, token = ops.web(ops.users[ADMIN])  # TOTP enrolled, NOT elevated
    for path, fields in POSTS:
        setup = no_totp.post(path, data={"csrf_token": no_totp_token, **fields})
        assert (setup.status_code, setup.json()) == (
            403,
            {
                "detail": {
                    "error": "totp_setup_required",
                    "setup_url": "/admin/mfa/setup",
                }
            },
        ), path
        gate = enrolled.post(path, data={"csrf_token": token, **fields})
        assert gate.status_code == 403, path
        assert gate.json()["detail"]["error"] == "elevation_required"
    _assert_no_effect(ops)
    for path in GETS:  # reads need no elevation
        assert no_totp.get(path).status_code == 200, path
        assert enrolled.get(path).status_code == 200, path


def test_csrf_is_checked_after_the_elevation_gate(ops: OpsEnv) -> None:
    elevated, _token = ops.web(ops.users[ADMIN], elevated=True)
    _other, other_token = ops.web(ops.users[ADMIN])  # a token of another session
    for path, fields in POSTS:
        for csrf in (None, "wrong-token", other_token):
            data = dict(fields) if csrf is None else {"csrf_token": csrf, **fields}
            refused = _form_post(elevated, path, data)
            assert refused.status_code == 403, (path, csrf)
            assert "Invalid CSRF token" in refused.text
    _assert_no_effect(ops)


def _stranded(ops: OpsEnv, rows: int) -> None:
    _destination(ops.backend, KEY)
    _seed_queue(ops.backend, rows, dest=KEY)


def _pending(ops: OpsEnv) -> int:
    return ops.backend.count(
        "SELECT COUNT(*) AS n FROM siem_delivery_queue WHERE destination_key = ? "
        "AND status = 'pending'",
        (KEY,),
    )


ABANDON = f"/admin/siem-delivery/destinations/{KEY}/abandon"


@pytest.mark.parametrize(
    "word, status",
    [
        ("ABANDON", 200),
        ("abandon", 400),
        (" ABANDON", 400),
        ("ABANDON\n", 400),
        (None, 400),
    ],
)
def test_only_the_exact_word_abandons(ops: OpsEnv, word: Any, status: int) -> None:
    _stranded(ops, 3)
    client, token = ops.web(ops.users[ADMIN], elevated=True)
    data = {"csrf_token": token}
    if word is not None:
        data["confirm_word"] = word
    response = _form_post(client, ABANDON, data)
    assert response.status_code == status, response.text[:300]
    abandoned = ops.audit_rows("siem_destination_abandoned")
    if status == 400:
        assert "type ABANDON to confirm" in response.text
        _assert_no_effect(ops)
        assert _pending(ops) == 3 and abandoned == []
    else:
        assert _pending(ops) == 0 and len(abandoned) == 1


def test_rest_abandon_stays_body_free(ops: OpsEnv) -> None:
    _stranded(ops, 4)
    rest = ops.rest(ops.users[ADMIN], elevated=True)
    response = rest.post(f"/api/admin/siem-delivery/destinations/{KEY}/abandon")
    assert (response.status_code, response.json()) == (200, {"abandoned": 4})


def test_counts_beyond_the_cap(ops: OpsEnv) -> None:
    _stranded(ops, COUNT_CAP + 50)
    client, token = ops.web(ops.users[ADMIN], elevated=True)
    dialog = client.get(f"/admin/siem-delivery/partials/destinations/{KEY}/abandon")
    text = " ".join(unescape(dialog.text).split())
    assert "pending 10,000+" in text and "instance-777" in text
    assert "currently queued; may grow until the action runs" in text.lower()
    result = _form_post(
        client, ABANDON, {"csrf_token": token, "confirm_word": "ABANDON"}
    )
    assert "Abandoned 10,050 events" in result.text
    [row] = ops.audit_rows("siem_destination_abandoned")
    details = parse_json_column(row["details"], dict, "details")
    assert details is not None and details["count"] == COUNT_CAP + 50


def _ids(n: int) -> List[str]:
    return [str(uuid.uuid4()) for _ in range(n)]


def test_bounded_input_is_refused_before_the_service(ops: OpsEnv) -> None:
    client, token = ops.web(ops.users[ADMIN], elevated=True)
    requeue = "/admin/siem-delivery/quarantine/requeue"
    confirm = "/admin/siem-delivery/canary/confirm-visible"
    big = _form_post(client, requeue, {"csrf_token": token, "pad": "x" * (64 * 1024)})
    assert big.status_code == 413
    refusals: List[Tuple[str, Dict[str, Any]]] = [
        (requeue, {"event_uuid": _ids(501)}),
        (requeue, {"event_uuid": ["not-a-uuid"]}),
        (confirm, {"canary_run_id": "run-x", "visible_ids_text": "not-a-uuid"}),
        (confirm, {"canary_run_id": "run-x", "visible_ids_text": " ".join(_ids(1001))}),
    ]
    for path, fields in refusals:
        refused = _form_post(client, path, {"csrf_token": token, **fields})
        assert refused.status_code == 400, (path, refused.text[:200])
    _assert_no_effect(ops)


MARK = "<script>x</script>"


def test_db_derived_strings_are_escaped(ops: OpsEnv) -> None:
    from code_indexer.server.services.siem_delivery import state_store

    _seed_queue(ops.backend, 1, status="quarantined", signature=MARK)
    view = ops.scheduler.committed_view()
    assert view.destination is not None
    refused = state_store.record_canary(
        ops.backend.db,
        run_id="run-escape",
        destination_key=view.destination.key,
        mapping_version=ops.scheduler.mapping_version,
        expected=[],
        result="accepted",
        signature=None,
        actor=MARK,
        config_epoch=view.section.arming_epoch,
        credential_id=ops.scheduler.credential_store.credential_id(),
        started_at=ops.backend.db.read(lambda tx: tx.ts(tx.now())),
        committed_epoch=ops.scheduler.committed_epoch,
    )
    assert refused is None  # recorded
    ops.backend.raw(
        "INSERT INTO siem_destinations (destination_key, region, project_id, location, "
        "instance_id, first_seen_at) VALUES (?, ?, 'p', 'l', 'i', ?)",
        (KEY, MARK, ops.backend.db.dialect.ts(datetime.now(timezone.utc))),
    )
    _seed_queue(ops.backend, 1, dest=KEY)
    client, _ = ops.web(ops.users[ADMIN])
    escaped = "&lt;script&gt;x&lt;/script&gt;"
    for path in GETS:
        html = client.get(path).text
        assert MARK not in html and "<script" not in html, path
        assert escaped in html, path


def test_enforcement_off_passes_through_on_both_doors(ops: OpsEnv) -> None:
    from tests.unit.server.self_service_elevation_harness import enforcement

    admin_user = ops.users[ADMIN]
    with enforcement(False):
        browser, token = ops.web(admin_user)  # no elevation window
        rest = ops.rest(admin_user)
        for action, audit_type in ACTIONS.items():
            web_path, fields, rest_path, body = twin_calls(ops, action, f"web-{action}")
            web = _form_post(browser, web_path, {"csrf_token": token, **fields})
            assert web.status_code == 200, (action, web.text[:300])
            assert len(ops.audit_rows(audit_type)) == 1, action
            web_path, fields, rest_path, body = twin_calls(
                ops, action, f"rest-{action}"
            )
            twin = rest.post(rest_path, json=body) if body else rest.post(rest_path)
            assert twin.status_code == 200, (action, twin.text[:300])
            assert len(ops.audit_rows(audit_type)) == 2, action


RESUME = "/admin/siem-delivery/resume"


@contextmanager
def _elevation_endpoints_enforced(ops: OpsEnv) -> Iterator[None]:
    """The two elevation endpoints bind the enforcement read at import; turn
    it on there too, and mount the REST ``/auth/elevate`` endpoint."""
    from code_indexer.server.auth.elevation_routes import router as rest_elevation

    ops.app.include_router(rest_elevation)
    web_mod = "code_indexer.server.web.elevation_web_routes"
    rest_mod = "code_indexer.server.auth.elevation_routes"
    with ExitStack() as stack:
        for module in (web_mod, rest_mod):
            stack.enter_context(
                patch(f"{module}._is_elevation_enforcement_enabled", return_value=True)
            )
            stack.enter_context(
                patch(f"{module}.elevated_session_manager", ops.stack.esm)
            )
        yield


def test_modal_elevation_replays_the_action(ops: OpsEnv) -> None:
    totp = pyotp.TOTP(ops.totp_secret)
    wrong = totp.at(int(time.time()) - 3600)  # a real code from another window
    with _elevation_endpoints_enforced(ops):
        client, token = ops.web(ops.users[ADMIN])
        first = _form_post(client, RESUME, {"csrf_token": token})
        assert first.json()["detail"]["error"] == "elevation_required"
        bad = client.post("/auth/elevate-ajax", data={"totp_code": wrong})
        assert bad.status_code == 401
        assert _form_post(client, RESUME, {"csrf_token": token}).status_code == 403
        rest = ops.rest(ops.users[ADMIN]).post(
            "/auth/elevate", json={"totp_code": wrong}
        )
        assert rest.status_code == 401
        assert rest.json()["detail"]["error"] == "elevation_failed"
        good = client.post("/auth/elevate-ajax", data={"totp_code": totp.now()})
        assert (good.status_code, good.json()) == (200, {"success": True})
        replay = _form_post(client, RESUME, {"csrf_token": token})
        assert replay.status_code == 200 and "resumed: false" in replay.text


def test_elevation_is_per_user(ops: OpsEnv) -> None:
    from code_indexer.server.auth.user_manager import UserRole

    ops.stack.create_user("admin-other", UserRole.ADMIN)
    ops.stack.enroll_mfa("admin-other")
    elevated, token_a = ops.web(ops.users[ADMIN], elevated=True)
    other = ops.user("admin-other")
    unelevated, token_b = ops.web(other)
    blocked = _form_post(unelevated, RESUME, {"csrf_token": token_b})
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["error"] == "elevation_required"
    assert _form_post(elevated, RESUME, {"csrf_token": token_a}).status_code == 200
