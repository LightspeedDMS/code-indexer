"""Read-only documents behind the Web SIEM operator panels.

The arming checklist, the recovery page and the destination dialog: each is
assembled from the COMMITTED configuration and the database (fleet state),
never from a process's cached view, so every node renders the same answer.
The actions themselves live in :mod:`admin` (shared with REST).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from code_indexer.server.services.siem_delivery import state_store, stats
from code_indexer.server.services.siem_delivery.admin import (
    SiemAdminError,
    _ts,
    capture_status,
)
from code_indexer.server.services.siem_delivery.db import SiemTx
from code_indexer.server.services.siem_delivery.udm import UDM_MAPPING

MAX_QUEUE_ID = 2**63 - 1  # queue ids are signed 64-bit on both backends
_CURSOR_DIGITS = re.compile(r"[0-9]{1,19}")  # ASCII only: str.isdigit() is not


def _quarantine_cursor(q_after: str) -> int:
    """The quarantine page cursor: '' is the first page; otherwise 1-19 ASCII
    digits within the queue id range.  Anything else is a 400, never a 500."""
    if not q_after:
        return 0
    if _CURSOR_DIGITS.fullmatch(q_after) is None or int(q_after) > MAX_QUEUE_ID:
        raise SiemAdminError(400, "invalid quarantine cursor")
    return int(q_after)


def _json_list(raw: Any, field_name: str) -> List[Any]:
    from code_indexer.server.storage.json_column import parse_json_column

    return [] if raw is None else parse_json_column(raw, list, field_name) or []


def _canary_view(state: Mapping[str, Any]) -> Dict[str, Any]:
    """The persisted canary record: each expected id with its confirmation."""
    confirmed_ids = [
        str(i)
        for i in _json_list(state.get("canary_confirmed_ids"), "canary_confirmed_ids")
    ]
    seen = set(confirmed_ids)
    expected = []
    for entry in _json_list(state.get("canary_expected"), "canary_expected"):
        product_log_id = str(entry.get("product_log_id"))
        expected.append(
            {
                "product_log_id": product_log_id,
                "action_type": str(entry.get("action_type")),
                "event_type": str(entry.get("event_type")),
                "confirmed": product_log_id in seen,
            }
        )
    return {
        "run_id": state.get("canary_run_id"),
        "destination_key": state.get("canary_destination_key"),
        "mapping_version": state.get("canary_mapping_version"),
        "result": state.get("canary_result"),
        "result_signature": state.get("canary_result_signature"),
        "sent_at": _ts(state, "canary_sent_at"),
        "actor": state.get("canary_actor"),
        "visible_confirmed_at": _ts(state, "canary_visible_confirmed_at"),
        "visible_confirmed_by": state.get("canary_visible_confirmed_by"),
        "expected": expected,
        "confirmed_ids": confirmed_ids,
        "missing_action_types": sorted(
            {e["action_type"] for e in expected if not e["confirmed"]}
        ),
    }


def arming_document(scheduler: Any) -> Dict[str, Any]:
    """Why capture is (not) armed, from the COMMITTED configuration and the
    database (fleet state); ``local_process`` is this process only."""
    from code_indexer.server.services.siem_delivery.destination import (
        SiemConfigInvalid,
    )

    try:
        view, config_error = scheduler.committed_view(), None
    except SiemConfigInvalid as exc:
        view, config_error = None, exc.field
    state = state_store.read_state(scheduler.db)
    key = view.destination.key if view is not None and view.destination else None
    canary = _canary_view(state)
    armed = view is not None and state_store.capture_active(
        state, version=view.version, enabled=view.section.enabled, destination_key=key
    )
    committed = None
    if view is not None:
        committed = {
            "config_version": view.version,
            "enabled": view.section.enabled,
            "destination_key": key,
            "mapping_version": scheduler.mapping_version,
        }
    readiness = None
    if key is not None:
        readiness = state_store.readiness_view(
            scheduler.db, key, scheduler.timings.probe_fresh_seconds
        )
    return {
        "committed": committed,
        "config_error": config_error,
        "credential": scheduler.credential_store.identity(),
        "canary": canary,
        # one canary event per mapping entry, plus the unmapped one
        "canary_event_count": len(UDM_MAPPING) + 1,
        "canary_matches": key is not None
        and canary["destination_key"] == key
        and canary["mapping_version"] == scheduler.mapping_version,
        "readiness": readiness,
        # node coverage (checklist row 6) exists only in PostgreSQL mode
        "cluster": scheduler.db.dialect.name == "postgres",
        "armed": armed,
        "armed_at": _ts(state, "armed_at") if armed else None,
        "local_process": {
            "status": capture_status(scheduler, state),
            "liveness": scheduler.get_liveness(),
        },
    }


def _committed_key(scheduler: Any) -> Tuple[Optional[str], Optional[str]]:
    """(committed destination key or None, invalid field or None): the
    recovery views stay usable when the stored configuration is invalid."""
    from code_indexer.server.services.siem_delivery.destination import (
        SiemConfigInvalid,
    )

    try:
        view = scheduler.committed_view()
    except SiemConfigInvalid as exc:
        return None, exc.field
    return (view.destination.key if view.destination else None), None


def recovery_document(
    scheduler: Any, q_after: str, d_after: str, b_after: str
) -> Dict[str, Any]:
    """Halt, the halted batch (by primary key, never paged away), and one
    keyset page each of open batches, quarantined rows and stranded keys."""
    after_id = _quarantine_cursor(q_after)
    configured_key, config_error = _committed_key(scheduler)
    state = state_store.read_state(scheduler.db)
    halted_id = state.get("halted_batch_id")

    def _q(tx: SiemTx) -> Dict[str, Any]:
        return {
            "halted_batch": stats.batch_by_id(tx, halted_id) if halted_id else None,
            "open_batches": stats.open_batches_page(tx, b_after or None),
            "quarantine": stats.quarantine_page(tx, after_id),
            "stranded": stats.stranded_page(tx, configured_key, d_after),
        }

    try:
        pages = scheduler.db.read(_q)
    except ValueError as exc:
        raise SiemAdminError(400, str(exc)) from None
    return {
        "configured_key": configured_key,
        "config_error": config_error,
        "halt": {
            "class": state.get("halted_class"),
            "signature": state.get("halted_signature"),
            "since": _ts(state, "halted_since"),
            "next_probe_at": _ts(state, "next_probe_at"),
            "batch_id": halted_id,
        },
        **pages,
    }


def destination_document(scheduler: Any, key: str) -> Dict[str, Any]:
    """Any destination by key, for the abandon / retarget dialog."""
    configured_key, config_error = _committed_key(scheduler)
    summary = scheduler.db.read(lambda tx: stats.destination_summary(tx, key))
    return {
        "summary": summary,
        "configured_key": configured_key,
        "config_error": config_error,
    }
