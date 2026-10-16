"""test_03's outage may excuse ONLY its own in-window probe warnings.

The scheduler's credential self-check can land inside test_03's scripted
connection-refused outage and log one "cannot mint SecOps tokens
(token_endpoint_unreachable)" WARNING.  That row -- and only that row -- may
be excused from the Phase 7 log audit: the same text logged at any other time
(a genuine token-endpoint failure in another scenario) must still fail it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import pytest

from tests.e2e.log_audit_gate import AuditGateResult
from tests.e2e.siem_delivery.outage_probe_window import (
    PROBE_MESSAGE,
    PROBE_SOURCE,
    OutageWindow,
    in_window_probe_failures,
    without_excused,
)

STARTED = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
WINDOW = OutageWindow(
    after_log_id=10, started=STARTED, ended=STARTED + timedelta(seconds=30)
)
THROUGH_LOG_ID = 20
IN_OUTAGE = STARTED + timedelta(seconds=5)


def _row(
    log_id: int,
    *,
    at: datetime = IN_OUTAGE,
    level: str = "WARNING",
    source: str = PROBE_SOURCE,
    message: str = PROBE_MESSAGE,
) -> Dict[str, Any]:
    return {
        "id": log_id,
        "timestamp": at.isoformat(),
        "level": level,
        "source": source,
        "message": message,
    }


def test_the_in_window_probe_warning_is_selected() -> None:
    assert in_window_probe_failures([_row(11)], WINDOW, THROUGH_LOG_ID) == [11]


def test_the_same_warning_outside_the_window_ids_is_not_selected() -> None:
    rows = [_row(WINDOW.after_log_id), _row(THROUGH_LOG_ID + 1)]

    assert in_window_probe_failures(rows, WINDOW, THROUGH_LOG_ID) == []


def test_an_in_range_probe_warning_timestamped_outside_the_outage_fails() -> None:
    after_outage = WINDOW.ended + timedelta(seconds=1)

    with pytest.raises(AssertionError, match="outside the outage"):
        in_window_probe_failures([_row(12, at=after_outage)], WINDOW, THROUGH_LOG_ID)


@pytest.mark.parametrize(
    "variant",
    [
        {"message": PROBE_MESSAGE.replace("unreachable", "rejected")},
        {
            "message": PROBE_MESSAGE.replace(
                "token_endpoint_unreachable", "token_rejected"
            )
        },
        {"message": PROBE_MESSAGE + " (again)"},
        {"message": "retry: " + PROBE_MESSAGE},
        {"source": "code_indexer.server.services.siem_delivery.sender"},
        {"level": "ERROR"},
    ],
)
def test_any_other_row_in_the_window_is_not_selected(variant: Dict[str, str]) -> None:
    rows = [_row(13, **variant)]  # type: ignore[arg-type]

    assert in_window_probe_failures(rows, WINDOW, THROUGH_LOG_ID) == []


def test_a_probe_failure_at_any_other_time_still_fails_the_audit() -> None:
    excused, elsewhere = _row(11), _row(40)
    result = AuditGateResult(
        passed=False, violations=[excused, elsewhere], phase_name="Phase 7"
    )

    filtered = without_excused(result, {11})

    assert filtered.passed is False
    assert filtered.violations == [elsewhere]
    assert filtered.phase_name == "Phase 7"


def test_the_audit_passes_when_only_excused_rows_remain() -> None:
    result = AuditGateResult(passed=False, violations=[_row(11)], phase_name="P7")

    assert without_excused(result, {11}).passed is True
