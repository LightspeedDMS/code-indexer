"""The ``siem_delivery`` Web Config section: settings readout, typed updates,
candidate validation and the read-only status block.

The section holds references only (a region, path segments and a key PATH);
no field accepts key contents or a token, and nothing here renders one.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Mapping, Optional, Tuple

from code_indexer.server.services.siem_delivery.destination import validate_section
from code_indexer.server.utils.siem_delivery_config import SiemDeliveryConfig

SECTION = "siem_delivery"
_BOOL_FIELDS = frozenset({"enabled"})
_INT_FIELDS = frozenset({"max_batch_events"})
_STR_FIELDS = frozenset(
    {
        "region",
        "api_version",
        "project_id",
        "location",
        "instance_id",
        "service_account_key_path",
        "source_instance_label",
        "harness_endpoint",
    }
)
_TRUE = frozenset({"true", "1", "yes", "on"})


def _section(config: Any) -> SiemDeliveryConfig:
    section = getattr(config, "siem_delivery_config", None)
    return section if isinstance(section, SiemDeliveryConfig) else SiemDeliveryConfig()


def siem_settings(config: Any) -> Dict[str, Any]:
    return dataclasses.asdict(_section(config))


def apply_siem_setting(section: SiemDeliveryConfig, key: str, value: Any) -> None:
    """Apply one typed form value; raises ValueError for an unknown key."""
    if key in _BOOL_FIELDS:
        setattr(section, key, str(value).strip().lower() in _TRUE)
    elif key in _INT_FIELDS:
        setattr(section, key, int(str(value).strip()))
    elif key in _STR_FIELDS:
        setattr(section, key, str(value).strip())
    else:
        raise ValueError(f"Unknown siem_delivery setting: {key}")


def candidate_section(
    current: SiemDeliveryConfig, form: Mapping[str, Any]
) -> SiemDeliveryConfig:
    candidate = dataclasses.replace(current)
    for key, value in form.items():
        apply_siem_setting(candidate, key, value)
    return candidate


def status_block(scheduler: Any, startup_error: Optional[str]) -> List[Tuple[str, str]]:
    """Read-only status rows from this process's in-memory view (no I/O)."""
    if scheduler is None:
        return [("Status", f"not running in this process ({startup_error})")]
    from code_indexer.server.services.siem_delivery.admin import capture_status
    from code_indexer.server.services.siem_delivery.stats import persisted_snapshot

    inputs = scheduler.health_inputs()
    state = inputs["state"]
    snap = persisted_snapshot(state)
    liveness = scheduler.get_liveness()
    rows = [
        ("Capture state", capture_status(scheduler, state)["status"]),
        ("Halt", str(state.get("halted_class") or "none")),
        ("Pending (undelivered)", str(snap.get("pending", 0))),
        (
            "Oldest pending age (s)",
            str(int(snap.get("oldest_pending_age_seconds") or 0)),
        ),
        ("Quarantined", str(snap.get("quarantined", 0))),
        ("Delivered total", str(state.get("delivered_total") or 0)),
        ("This process", f"{liveness['process_id']} probe={liveness['probe_result']}"),
        ("Config", str(liveness["config_lkg_status"])),
    ]
    if scheduler.harness_active:
        rows.append(("Mode", "harness mode (non-production test receiver)"))
    return rows


def validate_form(
    current: SiemDeliveryConfig, form: Mapping[str, Any], *, harness_active: bool
) -> Optional[str]:
    """None when the saved section would be valid, else a message naming
    the field (never the value)."""
    from code_indexer.server.services.siem_delivery.destination import (
        SiemConfigInvalid,
    )

    try:
        validate_section(
            candidate_section(current, form), harness_active=harness_active
        )
    except SiemConfigInvalid as exc:
        return f"SIEM Delivery: invalid {exc.field} ({exc.reason})"
    except ValueError:
        return "SIEM Delivery: invalid field value"
    return None
