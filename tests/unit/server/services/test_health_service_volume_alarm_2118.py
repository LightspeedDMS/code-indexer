"""Story #2118 slice 1b: /health reports the refresh embedding volume alarm.

lifespan sets ``app.state.index_spend_alarm`` to the node's IndexSpendAlarm
(or to None when it could not be built). The health collector reports:

- DEGRADED naming each alarming alias and its 24 h sum;
- DEGRADED "blind" when the aggregate read fails, stats recording is
  disabled, or the alarm could not be built (None) -- never healthy, never
  "0 spend";
- nothing when the attribute is absent (this process never ran lifespan).

DEGRADED only: never UNHEALTHY (that would make /healthz drain the node).
Real SQLite stats and metadata stores.
"""

from __future__ import annotations

import contextlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from unittest.mock import patch

import pytest

from code_indexer.server.models.api_models import (
    HealthStatus,
    ServiceHealthInfo,
    SystemHealthInfo,
)
from code_indexer.server.services.embedding_call_stats import (
    EmbeddingCallRecord,
    EmbeddingCallStatsSqliteBackend,
)
from code_indexer.server.services.health_service import HealthCheckService
from code_indexer.server.services.index_spend_alarm import IndexSpendAlarm
from tests.utils.golden_repo_metadata_stores import golden_repo_metadata_store

ATTR = "index_spend_alarm"
PATCH_TARGET = f"code_indexer.server.app.app.state.{ATTR}"
# Refresh stats rows carry the global alias (as production refresh children
# record it); golden_repos_metadata is keyed by the bare alias. The alarm
# maps one to the other (removesuffix("-global")).
ALIAS = "trace-repo-global"
BARE = ALIAS.removesuffix("-global")


def _status(service: HealthCheckService):
    return service._calculate_overall_status(
        {
            "database": ServiceHealthInfo(
                status=HealthStatus.HEALTHY, response_time_ms=1, error_message=None
            )
        },
        SystemHealthInfo(
            memory_usage_percent=20.0,
            cpu_usage_percent=20.0,
            active_jobs=0,
            disk_free_space_gb=200.0,
            disk_read_kb_s=0.0,
            disk_write_kb_s=0.0,
            net_rx_kb_s=0.0,
            net_tx_kb_s=0.0,
        ),
        [],
    )


@pytest.fixture
def metadata(tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store("sqlite", tmp_path) as backend:
        # Registered with no published size yet: the 10,000-item floor applies.
        backend.add_repo(
            alias=BARE,
            repo_url=f"local://{BARE}",
            default_branch="main",
            clone_path=f"/data/golden-repos/{BARE}",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        yield backend


def _alarm(tmp_path: Path, metadata: Any, items: Optional[int], enabled=True):
    stats = EmbeddingCallStatsSqliteBackend(str(tmp_path / "stats.db"))
    if items is not None:
        stats.insert_batch(
            [
                EmbeddingCallRecord(
                    provider="voyageai",
                    call_type="embed",
                    model="voyage-code-3",
                    item_count=items,
                    token_count=items,
                    batch_size=1,
                    purpose="refresh",
                    success=True,
                    latency_ms=1,
                    occurred_at=time.time() - 60,
                    golden_repo_alias=ALIAS,
                )
            ]
        )
    return stats, IndexSpendAlarm(
        stats_backend=stats, metadata_backend=metadata, stats_enabled=lambda: enabled
    )


@contextlib.contextmanager
def _attribute_absent() -> Iterator[None]:
    from code_indexer.server.app import app as app_module

    unset = object()
    original = getattr(app_module.state, ATTR, unset)
    if original is not unset:
        delattr(app_module.state, ATTR)
    try:
        yield
    finally:
        if original is not unset:
            setattr(app_module.state, ATTR, original)


def test_alarming_alias_is_degraded_with_alias_and_sum(tmp_path, metadata):
    _stats, alarm = _alarm(tmp_path, metadata, 23_456)

    with patch(PATCH_TARGET, alarm, create=True):
        status, reasons = _status(HealthCheckService())

    assert status == HealthStatus.DEGRADED
    joined = " ".join(reasons)
    assert ALIAS in joined and "23456" in joined


def test_quiet_fleet_is_healthy(tmp_path, metadata):
    _stats, alarm = _alarm(tmp_path, metadata, 500)

    with patch(PATCH_TARGET, alarm, create=True):
        status, reasons = _status(HealthCheckService())

    assert status == HealthStatus.HEALTHY
    assert not any("volume alarm" in r for r in reasons)


def test_failed_read_is_degraded_blind_not_zero_spend(tmp_path, metadata):
    stats, alarm = _alarm(tmp_path, metadata, 23_456)
    Path(stats._db_path).write_bytes(b"not a valid sqlite file")

    with patch(PATCH_TARGET, alarm, create=True):
        status, reasons = _status(HealthCheckService())

    assert status == HealthStatus.DEGRADED
    assert any("blind" in r for r in reasons)


def test_disabled_stats_recording_is_degraded_blind(tmp_path, metadata):
    _stats, alarm = _alarm(tmp_path, metadata, None, enabled=False)

    with patch(PATCH_TARGET, alarm, create=True):
        status, reasons = _status(HealthCheckService())

    assert status == HealthStatus.DEGRADED
    assert any("blind" in r and "disabled" in r for r in reasons)


def test_alarm_that_failed_to_start_is_degraded_blind():
    with patch(PATCH_TARGET, None, create=True):
        status, reasons = _status(HealthCheckService())

    assert status == HealthStatus.DEGRADED
    assert any("blind" in r for r in reasons)


def test_process_without_lifespan_reports_nothing():
    with _attribute_absent():
        status, reasons = _status(HealthCheckService())

    assert status == HealthStatus.HEALTHY
    assert not any("volume alarm" in r for r in reasons)
