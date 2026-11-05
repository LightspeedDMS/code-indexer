"""Installs the server's implementations of the kernel capabilities.

X-Ray and SCIP reach configuration, telemetry, the content seam and the
server's backend selections only through ``code_indexer.kernel.capabilities``
(Phase 2 of plans/designs/modular-packaging-addons.md). This module is the
server side of that boundary: thin adapters over the functions those packages
used to import directly.

Two rules keep behaviour identical:

* Every adapter imports its server function when CALLED, never at module
  level. Importing this module therefore costs nothing (it runs from
  ``code_indexer/server/__init__.py``, so even the CLI's lightest server import
  pays for it), and a test that patches a server binding is still observed.
* The adapters answer exactly what those functions answer, including outside a
  running server, where each reads "no app" and reports the local default.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Sequence, Tuple

from code_indexer.kernel import capabilities
from code_indexer.kernel.capabilities import ApplicationMetrics, Meter
from code_indexer.kernel.seam_types import (
    ContentEntry,
    ContentSeamSource,
    RipgrepBackend,
)
from code_indexer.server.feature_platform import ServerPlatform

#: The meter the application's own counters are created on.
_APPLICATION_METER = "cidx.application"


class ServerContentSeamHost:
    """The content seam as the server's content backend wiring selects it."""

    def is_delegated(self) -> bool:
        from code_indexer.server.startup.content_backend_wiring import (
            is_content_seam_delegated,
        )

        return is_content_seam_delegated()

    def source_for(
        self, repo_path: str, *, consumer: str
    ) -> Optional[ContentSeamSource]:
        from code_indexer.server.services.file_service import content_seam_source

        return content_seam_source(repo_path, consumer=consumer)

    def iter_manifest_entries(
        self,
        source: ContentSeamSource,
        prune_dirs: Sequence[str],
        *,
        deadline: Optional[float],
    ) -> Iterator[ContentEntry]:
        from code_indexer.server.services.file_service import iter_manifest_entries

        return iter_manifest_entries(source, prune_dirs, deadline=deadline)


class ServerRegexTargetHost:
    """Regex placement as the server's ripgrep backend wiring decides it."""

    def regex_target(self, repo_path: Path) -> Tuple[Path, Optional[RipgrepBackend]]:
        from code_indexer.server.startup.ripgrep_backend_wiring import (
            server_regex_target,
        )

        return server_regex_target(repo_path)


class ServerXrayCacheStoreHost:
    """The PostgreSQL database the X-Ray evaluator cache lives in (cluster
    mode). X-Ray builds its own store over the pool, so this module never
    imports the feature."""

    def postgres_connection_pool(self, postgres_dsn: str) -> Any:
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )

        return ConnectionPool(postgres_dsn)


class ServerRuntimeConfigHost:
    """Runtime configuration from the config service."""

    def runtime_config(self) -> object:
        from code_indexer.server.services.config_service import get_config_service

        return get_config_service().get_config()


class ServerTelemetryHost:
    """Telemetry, observed with ``peek_telemetry_manager`` only: a library
    never creates the telemetry manager as a side effect."""

    def application_metrics(self) -> Optional[ApplicationMetrics]:
        from code_indexer.server.telemetry.manager import peek_telemetry_manager
        from code_indexer.server.telemetry.metrics_instrumentation import (
            get_application_metrics,
        )

        telemetry_manager = peek_telemetry_manager()
        if telemetry_manager is None:
            return None
        metrics = get_application_metrics(telemetry_manager)
        return metrics if metrics.is_active else None

    def application_meter(self) -> Optional[Meter]:
        from code_indexer.server.telemetry.manager import peek_telemetry_manager
        from code_indexer.server.telemetry.metrics_instrumentation import (
            get_application_metrics,
        )

        telemetry_manager = peek_telemetry_manager()
        if telemetry_manager is None:
            return None
        if not get_application_metrics(telemetry_manager).is_active:
            return None
        meter: Meter = telemetry_manager.get_meter(_APPLICATION_METER)
        return meter

    def span(
        self, name: str, attributes: Optional[Dict[str, Any]] = None
    ) -> AbstractContextManager[Any]:
        from code_indexer.server.telemetry.spans import create_span

        return create_span(name, attributes=attributes)


class ServerReadScopeHost:
    """Carries the served-index read scope into a worker thread."""

    def carry(self, fn: Callable[[], None]) -> Callable[[], None]:
        from code_indexer.server.services.served_index_cache import (
            carry_served_index_reads,
        )

        return carry_served_index_reads(fn)


def install_kernel_hosts() -> None:
    """Install the server's implementation of every kernel capability.

    Idempotent and cheap (no server module is imported here). Called when the
    ``code_indexer.server`` package is imported, and by CLI front doors that
    reached server functions before the kernel boundary existed.
    """
    capabilities.CONTENT_SEAM_HOST.install(ServerContentSeamHost())
    capabilities.REGEX_TARGET_HOST.install(ServerRegexTargetHost())
    capabilities.XRAY_CACHE_STORE_HOST.install(ServerXrayCacheStoreHost())
    capabilities.RUNTIME_CONFIG_HOST.install(ServerRuntimeConfigHost())
    capabilities.TELEMETRY_HOST.install(ServerTelemetryHost())
    capabilities.READ_SCOPE_HOST.install(ServerReadScopeHost())
    capabilities.SERVER_PLATFORM_HOST.install(ServerPlatform())
