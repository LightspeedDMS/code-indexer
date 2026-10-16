"""The stored SecOps credential wired through the scheduler and the admin
actions (SQLite AND PostgreSQL, real SecOps sidecar): readiness, the legacy
key-path field, and write-only set / replace / remove with identity-only
audit rows."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest

from code_indexer.server.services import audit_capture
from code_indexer.server.services.siem_delivery import admin, capture
from code_indexer.server.services.siem_delivery.admin import SiemAdminError
from code_indexer.server.services.siem_delivery.destination import SECOPS_TOKEN_URI
from code_indexer.server.storage.json_column import parse_json_column
from tests.fixtures.secops_sidecar.harness import SidecarHandle

from .backends import SiemBackendHarness
from .conftest import harness_section
from .test_admin_parity import _arm, _CommittedConfig, _scheduler


@pytest.fixture()
def wired(
    siem_backend: SiemBackendHarness, siem_sidecar: SidecarHandle
) -> Iterator[Tuple[SiemBackendHarness, _CommittedConfig, SidecarHandle]]:
    capture.reset_capture_state_for_tests()
    audit_capture.mark_server_process()
    audit_capture.bind_audit_service(siem_backend.audit, node_id=None)
    config = _CommittedConfig(dataclasses.asdict(harness_section(siem_sidecar)))
    try:
        yield siem_backend, config, siem_sidecar
    finally:
        audit_capture.clear_audit_service()
        audit_capture.reset_server_process_mark()
        capture.reset_capture_state_for_tests()


def _key_text(sidecar: SidecarHandle, **overrides: Any) -> str:
    return json.dumps({**sidecar.read_key_file(), **overrides})


def _pem_line(sidecar: SidecarHandle) -> str:
    return str(sidecar.read_key_file()["private_key"].splitlines()[1])


def _audit_rows(b: SiemBackendHarness, action_type: str) -> List[Any]:
    return b.db.read(
        lambda tx: tx.query(
            "SELECT details FROM audit_logs WHERE action_type = ? ORDER BY id",
            (action_type,),
        )
    )


def _all_audit_text(b: SiemBackendHarness) -> str:
    rows = b.db.read(lambda tx: tx.query("SELECT * FROM audit_logs"))
    return json.dumps(rows, default=str)


def test_no_stored_credential_is_not_ready(wired: Tuple[Any, ...]) -> None:
    b, config, _sidecar = wired
    scheduler = _scheduler(b, config)
    scheduler.register_process()
    scheduler.run_cycle()
    assert scheduler.get_liveness()["probe_result"] == "credential_missing"
    with pytest.raises(SiemAdminError) as exc:
        admin.run_canary(scheduler, "alice")
    assert exc.value.status == 503 and "credential_missing" in exc.value.message
    assert not capture.capture_state().active


_OPENED: List[str] = []
_WATCHED: List[str] = []


def _open_hook(event: str, args: Any) -> None:
    if event == "open" and args and _WATCHED:
        path = os.fsdecode(args[0]) if isinstance(args[0], (str, bytes)) else ""
        if path in _WATCHED:
            _OPENED.append(path)


sys.addaudithook(_open_hook)


def test_legacy_key_path_field_is_ignored_loudly_and_never_read(
    wired: Tuple[Any, ...], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    b, config, sidecar = wired
    legacy = tmp_path / "legacy-sa-key.json"
    legacy.write_text(_key_text(sidecar), encoding="utf-8")
    config.section = {**config.section, "service_account_key_path": str(legacy)}
    _WATCHED[:] = [str(legacy)]
    _OPENED.clear()
    try:
        scheduler = _scheduler(b, config)
        scheduler.register_process()
        with caplog.at_level(logging.WARNING):
            scheduler.run_cycle()
            scheduler.run_cycle()
            admin.stats_document(scheduler)
    finally:
        _WATCHED.clear()
    assert _OPENED == []
    legacy_warnings = [
        r for r in caplog.records if "service_account_key_path" in r.getMessage()
    ]
    assert len(legacy_warnings) == 1
    assert legacy_warnings[0].levelno == logging.WARNING
    assert str(legacy) not in caplog.text
    assert scheduler.get_liveness()["probe_result"] == "credential_missing"


def test_set_replace_remove_are_write_only_and_audit_identity_only(
    wired: Tuple[Any, ...],
) -> None:
    b, config, sidecar = wired
    scheduler = _scheduler(b, config)
    key = sidecar.read_key_file()
    result = admin.set_credential(scheduler, "alice", _key_text(sidecar))
    assert result["change"] == "set"
    assert result["credential"]["client_email"] == key["client_email"]
    assert result["credential"]["private_key_id"] == key["private_key_id"]
    _arm(scheduler)  # the stored key mints tokens: capture arms
    doc = admin.stats_document(scheduler)
    assert doc["credential"] == {**result["credential"]}
    assert admin.set_credential(scheduler, "bob", _key_text(sidecar))["change"] == (
        "replaced"
    )
    removed = admin.remove_credential(scheduler, "carol")
    assert removed["change"] == "removed"
    assert admin.stats_document(scheduler)["credential"] is None
    with pytest.raises(SiemAdminError) as exc:
        admin.remove_credential(scheduler, "carol")
    assert exc.value.status == 404

    details: List[Dict[str, Any]] = []
    for row in _audit_rows(b, "siem_credential_changed"):
        parsed = parse_json_column(row["details"], dict, "details")
        assert parsed is not None
        details.append(parsed)
    assert [d["change"] for d in details] == ["set", "replaced", "removed"]
    for d in details:
        assert set(d) == {"change", "client_email", "private_key_id"}
        assert d["private_key_id"] == key["private_key_id"]
    surfaces = [_all_audit_text(b), json.dumps(result), json.dumps(doc, default=str)]
    assert all(_pem_line(sidecar) not in s for s in surfaces)


class _ReadsOnce(_CommittedConfig):
    """The committed config reads once, then the database fails."""

    def __init__(self, section: Any) -> None:
        super().__init__(section)
        self.reads = 0

    def read_committed_section(self, name: str) -> Tuple[int, Any]:
        self.reads += 1
        if self.reads > 1:
            raise RuntimeError("runtime database unavailable")
        return super().read_committed_section(name)


def test_credential_change_is_never_stored_without_its_audit_row(
    wired: Tuple[Any, ...],
) -> None:
    b, config, sidecar = wired
    for action in ("set", "remove"):
        flaky = _ReadsOnce(config.section)
        scheduler = _scheduler(b, flaky)
        if action == "set":
            admin.set_credential(scheduler, "alice", _key_text(sidecar))
        else:
            admin.remove_credential(scheduler, "alice")
    changes = [
        parse_json_column(r["details"], dict, "details")
        for r in _audit_rows(b, "siem_credential_changed")
    ]
    assert [c["change"] for c in changes if c] == ["set", "removed"]


def test_config_page_status_shows_identity_only(wired: Tuple[Any, ...]) -> None:
    from code_indexer.server.services.siem_delivery.config_view import status_block

    b, config, sidecar = wired
    scheduler = _scheduler(b, config)
    scheduler.register_process()
    scheduler.run_cycle()
    rows = dict(status_block(scheduler, None))
    assert rows["Service account credential"] == "not configured"
    key = sidecar.read_key_file()
    admin.set_credential(scheduler, "alice", _key_text(sidecar))
    rows = dict(status_block(scheduler, None))  # this process: no wait
    shown = rows["Service account credential"]
    assert key["client_email"] in shown and key["private_key_id"] in shown
    assert "alice" in shown
    assert _pem_line(sidecar) not in json.dumps(rows)


def test_invalid_key_is_rejected_and_nothing_is_stored_or_audited(
    wired: Tuple[Any, ...], caplog: pytest.LogCaptureFixture
) -> None:
    b, config, sidecar = wired
    scheduler = _scheduler(b, config)
    with caplog.at_level(logging.DEBUG), pytest.raises(SiemAdminError) as exc:
        admin.set_credential(
            scheduler, "alice", _key_text(sidecar, token_uri="https://example.com/t")
        )
    assert exc.value.status == 400 and "token_uri" in exc.value.message
    assert admin.stats_document(scheduler)["credential"] is None
    assert _audit_rows(b, "siem_credential_changed") == []
    assert _pem_line(sidecar) not in caplog.text


def test_deployed_process_accepts_only_the_google_token_uri(
    wired: Tuple[Any, ...],
) -> None:
    b, config, sidecar = wired
    config.section = {**config.section, "harness_endpoint": "", "region": "us"}
    scheduler = _scheduler(b, config)
    scheduler.harness_active = False
    with pytest.raises(SiemAdminError):
        admin.set_credential(scheduler, "alice", _key_text(sidecar))
    ok = admin.set_credential(
        scheduler, "alice", _key_text(sidecar, token_uri=SECOPS_TOKEN_URI)
    )
    assert ok["change"] == "set"
