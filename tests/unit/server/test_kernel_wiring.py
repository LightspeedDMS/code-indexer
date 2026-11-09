"""The server's kernel capability hosts (server/kernel_wiring.py).

Each adapter must answer exactly what the server function it wraps answers,
and must look that function up when called, so a test patching the server
binding is still observed by X-Ray and SCIP.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from code_indexer.kernel import capabilities as caps
from code_indexer.server import kernel_wiring as kw
from code_indexer.server.feature_platform import ServerPlatform


def test_importing_the_server_installs_every_host() -> None:
    kw.install_kernel_hosts()
    assert isinstance(caps.CONTENT_SEAM_HOST.get(), kw.ServerContentSeamHost)
    assert isinstance(caps.REGEX_TARGET_HOST.get(), kw.ServerRegexTargetHost)
    assert isinstance(caps.XRAY_CACHE_STORE_HOST.get(), kw.ServerXrayCacheStoreHost)
    assert isinstance(caps.SERVER_PLATFORM_HOST.get(), ServerPlatform)
    assert isinstance(caps.RUNTIME_CONFIG_HOST.get(), kw.ServerRuntimeConfigHost)
    assert isinstance(caps.TELEMETRY_HOST.get(), kw.ServerTelemetryHost)
    assert isinstance(caps.READ_SCOPE_HOST.get(), kw.ServerReadScopeHost)


class TestContentSeamHost:
    def test_follows_patched_server_bindings(self) -> None:
        host = kw.ServerContentSeamHost()
        source = MagicMock(name="source")
        with (
            patch(
                "code_indexer.server.startup.content_backend_wiring.is_content_seam_delegated",
                return_value=True,
            ),
            patch(
                "code_indexer.server.services.file_service.content_seam_source",
                return_value=source,
            ) as seam,
            patch(
                "code_indexer.server.services.file_service.iter_manifest_entries",
                return_value=iter(["entry"]),
            ) as manifest,
        ):
            assert host.is_delegated() is True
            assert host.source_for("/repo", consumer="X-Ray") is source
            assert list(
                host.iter_manifest_entries(source, (".git",), deadline=1.0)
            ) == ["entry"]
        seam.assert_called_once_with("/repo", consumer="X-Ray")
        manifest.assert_called_once_with(source, (".git",), deadline=1.0)

    def test_outside_a_running_server_is_local(self) -> None:
        host = kw.ServerContentSeamHost()
        assert host.is_delegated() is False
        # Always a source: the local implementation reads the path itself.
        source = host.source_for("/repo", consumer="X-Ray")
        assert source is not None and source.backend.is_local
        assert source.root == "/repo"


class TestPlacementHosts:
    def test_regex_target_follows_server(self) -> None:
        with patch(
            "code_indexer.server.startup.ripgrep_backend_wiring.server_regex_target",
            return_value=(Path("/base"), None),
        ) as target:
            assert kw.ServerRegexTargetHost().regex_target(Path("/repo")) == (
                Path("/base"),
                None,
            )
        target.assert_called_once_with(Path("/repo"))

    def test_xray_placement_is_the_features_not_the_cores(self) -> None:
        """X-Ray's placement host is installed by the xray feature's
        on_startup (cidx_xray.server.lifecycle), never by the core."""
        assert not hasattr(kw, "ServerXrayPlacementHost")

    def test_cache_store_opens_a_postgres_pool(self) -> None:
        """The core opens the pool; X-Ray builds its store over it."""
        with patch(
            "code_indexer.server.storage.postgres.connection_pool.ConnectionPool"
        ) as pool:
            built = kw.ServerXrayCacheStoreHost().postgres_connection_pool("dsn")
        pool.assert_called_once_with("dsn")
        assert built is pool.return_value


def test_runtime_config_reads_the_config_service() -> None:
    config = object()
    service = MagicMock()
    service.get_config.return_value = config
    with patch(
        "code_indexer.server.services.config_service.get_config_service",
        return_value=service,
    ):
        assert kw.ServerRuntimeConfigHost().runtime_config() is config


class TestTelemetryHost:
    _PEEK = "code_indexer.server.telemetry.manager.peek_telemetry_manager"
    _METRICS = (
        "code_indexer.server.telemetry.metrics_instrumentation.get_application_metrics"
    )

    def test_nothing_without_a_telemetry_manager(self) -> None:
        with patch(self._PEEK, return_value=None):
            assert kw.ServerTelemetryHost().application_metrics() is None
            assert kw.ServerTelemetryHost().application_meter() is None

    def test_nothing_while_metrics_are_inactive(self) -> None:
        metrics = MagicMock(is_active=False)
        with (
            patch(self._PEEK, return_value=MagicMock()),
            patch(self._METRICS, return_value=metrics),
        ):
            assert kw.ServerTelemetryHost().application_metrics() is None
            assert kw.ServerTelemetryHost().application_meter() is None

    def test_active_metrics_and_application_meter(self) -> None:
        manager = MagicMock()
        metrics = MagicMock(is_active=True)
        with (
            patch(self._PEEK, return_value=manager),
            patch(self._METRICS, return_value=metrics),
        ):
            assert kw.ServerTelemetryHost().application_metrics() is metrics
            assert (
                kw.ServerTelemetryHost().application_meter()
                is manager.get_meter.return_value
            )
        manager.get_meter.assert_called_once_with("cidx.application")

    def test_span_uses_create_span(self) -> None:
        seen: List[Dict[str, Any]] = []

        def fake_create_span(
            name: str, attributes: Optional[Dict[str, Any]] = None
        ) -> Any:
            seen.append({"name": name, "attributes": attributes})
            return MagicMock()

        with patch("code_indexer.server.telemetry.spans.create_span", fake_create_span):
            kw.ServerTelemetryHost().span("cidx.scip.generate", {"repo_root": "/r"})
        assert seen == [
            {"name": "cidx.scip.generate", "attributes": {"repo_root": "/r"}}
        ]


def test_read_scope_carries_served_index_reads() -> None:
    def work() -> None:
        return None

    with patch(
        "code_indexer.server.services.served_index_cache.carry_served_index_reads"
    ) as carry:
        assert kw.ServerReadScopeHost().carry(work) is carry.return_value
    carry.assert_called_once_with(work)
