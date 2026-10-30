"""RefreshFence: one callable on the refresh's cancel hook that stops the
run only on a POSITIVE ownership-lost answer (job or alias lease), never on
a failed read."""

from __future__ import annotations

import logging
from typing import List

import pytest

from code_indexer.global_repos.refresh_fence import (
    JOB_OWNERSHIP_LOST,
    LEASE_LOST,
    LEASE_PROBE_INTERVAL_SECONDS,
    RefreshFence,
)
from code_indexer.server.services.alias_lock_store.base import (
    AliasLockOwnershipLostError,
)

ALIAS = "example-repo-global"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Probe:
    def __init__(self, error: BaseException | None = None) -> None:
        self.calls = 0
        self.error = error

    def __call__(self) -> None:
        self.calls += 1
        if self.error is not None:
            raise self.error


def test_job_ownership_lost_fences_and_stays_fenced():
    owned: List[bool] = [True]
    fence = RefreshFence(None, lambda: owned[0], alias=ALIAS)
    assert fence() is False and fence.lost_reason is None
    owned[0] = False
    assert fence() is True
    assert fence.lost_reason == JOB_OWNERSHIP_LOST
    owned[0] = True
    assert fence() is True, "a fenced run never resumes"


def test_plain_cancel_stops_without_an_ownership_reason():
    fence = RefreshFence(lambda: True, lambda: True, alias=ALIAS)
    assert fence() is True
    assert fence.lost_reason is None


def test_cancel_check_that_reports_lost_ownership_is_classified_as_lost():
    # ^ order cancel_check before job_owned in src/code_indexer/global_repos/refresh_fence.py:RefreshFence.__call__
    state = {"owned": True}

    def cancel_check() -> bool:
        state["owned"] = False
        return True

    fence = RefreshFence(cancel_check, lambda: state["owned"], alias=ALIAS)
    assert fence() is True
    assert fence.lost_reason == JOB_OWNERSHIP_LOST


def test_job_check_read_error_keeps_the_run_going(caplog):
    def unreadable() -> bool:
        raise OSError("store unreachable")

    fence = RefreshFence(None, unreadable, alias=ALIAS)
    with caplog.at_level(logging.WARNING):
        assert fence() is False
    assert fence.lost_reason is None
    assert "keeps running" in caplog.text


def test_lease_probe_runs_once_per_interval_and_fences_on_positive_loss():
    clock = Clock()
    probe = Probe()
    fence = RefreshFence(None, lambda: True, alias=ALIAS, clock=clock)
    fence.arm_lease_probe(probe)
    assert fence() is False
    assert probe.calls == 0, (
        "the lease was just acquired; first probe one interval later"
    )
    clock.now += LEASE_PROBE_INTERVAL_SECONDS
    assert fence() is False and probe.calls == 1
    assert fence() is False and probe.calls == 1, "throttled between intervals"
    probe.error = AliasLockOwnershipLostError("taken over")
    clock.now += LEASE_PROBE_INTERVAL_SECONDS
    assert fence() is True
    assert fence.lost_reason == LEASE_LOST


@pytest.mark.parametrize("error", [OSError("db down"), RuntimeError("timeout")])
def test_lease_probe_store_error_keeps_the_run_going(error, caplog):
    clock = Clock()
    probe = Probe(error)
    fence = RefreshFence(None, None, alias=ALIAS, clock=clock)
    fence.arm_lease_probe(probe)
    clock.now += LEASE_PROBE_INTERVAL_SECONDS
    with caplog.at_level(logging.WARNING):
        assert fence() is False
    assert fence.lost_reason is None and probe.calls == 1
    assert fence() is False and probe.calls == 1, "a failed probe waits an interval"


def test_disarmed_lease_probe_is_never_called():
    clock = Clock()
    probe = Probe(AliasLockOwnershipLostError("released"))
    fence = RefreshFence(None, None, alias=ALIAS, clock=clock)
    fence.arm_lease_probe(probe)
    fence.disarm_lease_probe()
    clock.now += 10 * LEASE_PROBE_INTERVAL_SECONDS
    assert fence() is False and probe.calls == 0
