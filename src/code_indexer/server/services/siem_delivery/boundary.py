"""Configuration boundary events: the SIEM destination of a ``config_changed``
row, computed from the COMMITTED before/after configuration (never from the
scheduler's snapshot), so enable/disable/clear/change/reset rows are
captured whatever the scheduler timing.
"""

from __future__ import annotations

import uuid
from typing import Any, List, Mapping, Optional, Tuple

from code_indexer.server.services.audit_events import ALL_TARGETS_MARKER
from code_indexer.server.services.siem_delivery.capture import SiemTarget
from code_indexer.server.services.siem_delivery.destination import destination_key
from code_indexer.server.services.siem_delivery.scope import (
    SIEM_CONFIG_KEY_PREFIX,
    SIEM_CONFIG_SECTION,
)
from code_indexer.server.utils.siem_delivery_config import SiemDeliveryConfig


def _section(config: Any) -> SiemDeliveryConfig:
    section = getattr(config, "siem_delivery_config", None)
    return section if isinstance(section, SiemDeliveryConfig) else SiemDeliveryConfig()


def _keys(details: Mapping[str, Any]) -> List[str]:
    keys: List[str] = []
    for name in ("changed_keys", "attempted_keys"):
        value = details.get(name)
        if isinstance(value, list):
            keys.extend(k for k in value if isinstance(k, str))
    return keys


def is_siem_target(target_id: str, details: Mapping[str, Any]) -> bool:
    if target_id == SIEM_CONFIG_SECTION:
        return True
    return target_id == ALL_TARGETS_MARKER and any(
        k.startswith(SIEM_CONFIG_KEY_PREFIX) for k in _keys(details)
    )


def siem_boundary_target(
    before: Any,
    after: Any,
    *,
    target_id: str,
    change_kind: str,
    details: Mapping[str, Any],
    outcome: str = "success",
) -> Tuple[bool, Optional[SiemTarget]]:
    """``(is_siem, target)``; target None = no destination before or after."""
    if not is_siem_target(target_id, details):
        return False, None
    b = _section(before)
    # A failed attempt published nothing: the configuration is unchanged.
    a = _section(after) if outcome == "success" and after is not None else b
    dest_before = destination_key(b)
    dest_after = destination_key(a)
    dest = dest_after if dest_after is not None else dest_before
    if change_kind == "reset_to_defaults":
        kind = "reset"
    elif dest_before and dest_after is None:
        kind = "clear"
    elif dest_before and dest_after and dest_before != dest_after:
        kind = "destination_change"
    elif b.enabled and not a.enabled:
        kind = "disable"
    elif a.enabled and not b.enabled:
        kind = "enable"
    else:
        kind = "other_siem_change"
    return True, (SiemTarget(dest, kind) if dest else None)


def carry_arming_epoch(before: Any, after: Any) -> None:
    """Stamp the candidate *after* with its configuration lifetime (Bug #2018).

    *before* is the COMMITTED pre-image of the change.  The epoch is carried
    over from it -- whatever the mutation or a reset-to-defaults replacement
    put there -- and renewed when the change ends the lifetime a canary
    confirmation was made for: the destination is disabled, cleared or
    changed, or the trusted CA changes.
    """
    b = _section(before)
    a = after.siem_delivery_config
    assert isinstance(a, SiemDeliveryConfig), "ServerConfig always has the section"
    ended = (
        (b.enabled and not a.enabled)
        or destination_key(b) != destination_key(a)
        or b.trusted_ca_fingerprint != a.trusted_ca_fingerprint
    )
    a.arming_epoch = uuid.uuid4().hex if ended else b.arming_epoch
