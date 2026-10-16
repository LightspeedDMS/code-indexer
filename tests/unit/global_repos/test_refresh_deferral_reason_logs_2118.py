"""Story #2118 slice 1b, review P3: a system caller that logs a deferred
refresh names the deferral's real reason. A volume-alarm deferral must not
be reported as a failure backoff; a failure-backoff deferral (no reason)
keeps its original wording.

``request_cidx_meta_refresh`` takes the scheduler as an argument, so the
scheduler here is a double raising the real ``RefreshDeferredError``.
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

import pytest

from code_indexer.global_repos.meta_description_hook import (
    request_cidx_meta_refresh,
)
from code_indexer.global_repos.refresh_failure_recovery import RefreshDeferredError

HOOK_LOGGER = "code_indexer.global_repos.meta_description_hook"
VOLUME_REASON = "refresh embedding volume alarm active"
DEFERRAL_SECONDS = 60


class _DeferringScheduler:
    def __init__(self, reason: Optional[str]) -> None:
        self._reason = reason

    def trigger_refresh_for_repo(self, alias: str) -> None:
        until = time.time() + DEFERRAL_SECONDS
        if self._reason is None:
            raise RefreshDeferredError(alias, until)
        raise RefreshDeferredError(alias, until, reason=self._reason)


def _hook_lines(caplog: pytest.LogCaptureFixture) -> List[str]:
    return [r.getMessage() for r in caplog.records if r.name == HOOK_LOGGER]


def test_cidx_meta_hook_names_the_volume_alarm(caplog) -> None:
    with caplog.at_level(logging.INFO, logger=HOOK_LOGGER):
        request_cidx_meta_refresh(_DeferringScheduler(VOLUME_REASON))

    lines = _hook_lines(caplog)
    assert any(VOLUME_REASON in m for m in lines), lines
    assert not any("failure backoff" in m for m in lines), lines


def test_cidx_meta_hook_keeps_the_backoff_wording(caplog) -> None:
    with caplog.at_level(logging.INFO, logger=HOOK_LOGGER):
        request_cidx_meta_refresh(_DeferringScheduler(None))

    assert any("persisted failure backoff" in m for m in _hook_lines(caplog))


def test_deferral_without_reason_keeps_the_default_message() -> None:
    exc = RefreshDeferredError("repo-global", 1_900_000_000.0)

    assert exc.reason is None
    assert str(exc).endswith("persisted failure backoff active")
