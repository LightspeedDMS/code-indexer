"""Each Web SIEM action writes the same audit row as its REST twin (SQLite
AND PostgreSQL): the shared service writes it, so the rows agree on every
invariant field and differ only in front-door attribution; refusals write
none (Q3); no secret reaches a row, a response or a log line."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

import pytest

from code_indexer.server.storage.json_column import parse_json_column

from .test_web_ops_security import _form_post
from .web_ops_harness import ACTIONS, ADMIN, OpsEnv, twin_calls

# Per-call identifiers: each door acts on its own run, batch or destination.
ID_KEYS = {"canary_run_id", "batch_id", "destination_key", "from_destination_key"}


def _door_row(ops: OpsEnv, audit_type: str, source: str) -> Dict[str, Any]:
    rows = [r for r in ops.audit_rows(audit_type) if r["source"] == source]
    assert len(rows) == 1, (audit_type, source, len(rows))
    row = dict(rows[0])
    row["details"] = parse_json_column(row["details"], dict, "details") or {}
    queued = ops.backend.db.read(
        lambda tx: tx.one(
            "SELECT destination_key FROM siem_delivery_queue WHERE event_uuid = ?",
            (row["event_uuid"],),
        )
    )
    row["siem_destination"] = queued["destination_key"] if queued else None
    return row


def _act_on_both_doors(ops: OpsEnv, action: str) -> List[Any]:
    admin_user = ops.users[ADMIN]
    browser, token = ops.web(admin_user, elevated=True)
    rest = ops.rest(admin_user, elevated=True)
    web_path, fields, _, _ = twin_calls(ops, action, f"web-{action}")
    web = _form_post(browser, web_path, {"csrf_token": token, **fields})
    _, _, rest_path, body = twin_calls(ops, action, f"rest-{action}")
    twin = rest.post(rest_path, json=body) if body else rest.post(rest_path)
    assert (web.status_code, twin.status_code) == (200, 200), (web.text, twin.text)
    return [browser, token, rest, web, twin]


@pytest.mark.parametrize("action", sorted(ACTIONS))
def test_web_and_rest_write_the_same_row(ops: OpsEnv, action: str) -> None:
    _act_on_both_doors(ops, action)
    web = _door_row(ops, ACTIONS[action], "web")
    rest = _door_row(ops, ACTIONS[action], "rest")
    for field in ("action_type", "admin_id", "target_type", "target_id", "outcome"):
        assert web[field] == rest[field], field
    assert web["admin_id"] == ADMIN
    assert set(web["details"]) == set(rest["details"])
    for key in set(web["details"]) - ID_KEYS:
        assert web["details"][key] == rest["details"][key], key
    assert web["siem_destination"] is not None
    assert web["siem_destination"] == rest["siem_destination"]
    assert (web["source"], web["auth_method"]) == ("web", "web_session")
    assert (rest["source"], rest["auth_method"]) == ("rest", "jwt")


def test_no_secret_reaches_rows_responses_or_logs(
    ops: OpsEnv, caplog: pytest.LogCaptureFixture
) -> None:
    surfaces: List[str] = []
    with caplog.at_level(logging.DEBUG):
        for action in ("abandon", "confirm-visible", "requeue"):
            browser, token, rest, web, twin = _act_on_both_doors(ops, action)
            surfaces += [web.text, twin.text]
    secrets = [
        token,
        str(browser.cookies.get("session")),
        str(rest.headers["Authorization"]).split(" ", 1)[1],
        ops.sidecar.read_key_file()["private_key"].splitlines()[1],
        "confirm_word",
    ]
    rows = ops.backend.db.read(lambda tx: tx.query("SELECT * FROM audit_logs"))
    surfaces += [json.dumps(rows, default=str), caplog.text]
    for secret in secrets:
        assert all(secret not in s for s in surfaces), secret[:12]


def test_refused_actions_write_no_row_on_either_door(ops: OpsEnv) -> None:
    admin_user = ops.users[ADMIN]
    browser, token = ops.web(admin_user, elevated=True)
    path = "batches/batch-none/acknowledge"
    web = _form_post(browser, f"/admin/siem-delivery/{path}", {"csrf_token": token})
    twin = ops.rest(admin_user, elevated=True).post(f"/api/admin/siem-delivery/{path}")
    assert (web.status_code, twin.status_code) == (409, 409)
    assert ops.audit_rows("siem_batch_acknowledged") == []
