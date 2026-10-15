"""Story #2126: /health reports every repository whose refresh is failing
in a way no automatic repair can fix (e.g. its chunk store is missing and no
usable published snapshot exists to restore it from).

The reason lives in the shared golden-repo metadata store (SQLite solo,
PostgreSQL cluster), so every node reports the same thing. DEGRADED only --
never UNHEALTHY (that would drain the node from the load balancer for a
per-repository condition). A verified publish clears it.

Real metadata stores; ``app.state.backend_registry`` is a stand-in container
exposing the real store under its production attribute name.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Tuple
from unittest.mock import patch

import pytest

from code_indexer.global_repos.refresh_failure_recovery import (
    begin_refresh_cycle,
    record_failure_backoff,
    resolve_after_publish,
)
from code_indexer.server.models.api_models import (
    HealthStatus,
    ServiceHealthInfo,
    SystemHealthInfo,
)
from code_indexer.server.services.health_service import HealthCheckService
from tests.utils.golden_repo_metadata_stores import (
    STORE_KINDS,
    golden_repo_metadata_store,
)

PATCH_TARGET = "code_indexer.server.app.app.state.backend_registry"


class _NoStoredCredentialMarks:
    """A stored-credential backend with nothing needing re-entry."""

    def list_needs_reentry(self) -> List[Dict[str, Any]]:
        return []


def _registry(**fields: Any) -> SimpleNamespace:
    return SimpleNamespace(
        git_credentials=_NoStoredCredentialMarks(),
        ci_tokens=_NoStoredCredentialMarks(),
        **fields,
    )


ALIAS = "example-repo-global"
REASON = (
    "chunk store missing and not restorable (voyage-code-3: no published "
    "snapshot to restore it from)"
)


@pytest.fixture(params=STORE_KINDS)
def metadata(request, tmp_path: Path) -> Iterator[Any]:
    with golden_repo_metadata_store(request.param, tmp_path) as backend:
        yield backend


def _status(registry: Any) -> Tuple[HealthStatus, List[str]]:
    service = HealthCheckService()
    with patch(PATCH_TARGET, registry, create=True):
        return service._calculate_overall_status(
            {
                "database": ServiceHealthInfo(
                    status=HealthStatus.HEALTHY,
                    response_time_ms=1,
                    error_message=None,
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


def _refresh_reasons(reasons: List[str]) -> List[str]:
    return [r for r in reasons if ALIAS in r]


def test_unrepairable_refresh_degrades_naming_repository(metadata: Any) -> None:
    record_failure_backoff(
        metadata, ALIAS, "fatal chunk-store failure", unrepairable_reason=REASON
    )

    status, reasons = _status(_registry(golden_repo_metadata=metadata))

    assert status == HealthStatus.DEGRADED, reasons
    named = _refresh_reasons(reasons)
    assert len(named) == 1 and REASON in named[0], reasons


def test_verified_publish_clears_the_reason(metadata: Any) -> None:
    record_failure_backoff(
        metadata, ALIAS, "fatal chunk-store failure", unrepairable_reason=REASON
    )
    _regate, covered = begin_refresh_cycle(metadata, ALIAS)

    resolve_after_publish(metadata, ALIAS, covered)

    status, reasons = _status(_registry(golden_repo_metadata=metadata))
    assert status == HealthStatus.HEALTHY, reasons
    assert _refresh_reasons(reasons) == []


def test_ordinary_failure_after_it_clears_the_reason(metadata: Any) -> None:
    """A later failure the self-heal treats as ordinary (retried, e.g. a
    transient I/O error) replaces the operator reason."""
    record_failure_backoff(
        metadata, ALIAS, "fatal chunk-store failure", unrepairable_reason=REASON
    )
    record_failure_backoff(metadata, ALIAS, "disk I/O error")

    status, reasons = _status(_registry(golden_repo_metadata=metadata))

    assert status == HealthStatus.HEALTHY, reasons
    assert _refresh_reasons(reasons) == []


def test_process_without_registry_reports_nothing() -> None:
    status, reasons = _status(None)

    assert status == HealthStatus.HEALTHY, reasons


class _UnreadableStore:
    """A metadata store whose read fails, as when the database is down."""

    def list_unrepairable_refresh_failures(self) -> List[Any]:
        raise OSError("database unavailable")


def test_unreadable_store_is_degraded_never_silently_healthy() -> None:
    status, reasons = _status(_registry(golden_repo_metadata=_UnreadableStore()))

    assert status == HealthStatus.DEGRADED, reasons
    assert any("unreadable" in r for r in reasons), reasons
