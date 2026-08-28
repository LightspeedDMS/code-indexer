"""TDD tests for Story #1676 AC6: richer resource attributes on TelemetryManager.

AC6 requires that every span/metric resource carries, in addition to the
existing service.name/deployment.environment:
  - service.version, sourced from code_indexer.__version__
  - cidx.cluster.node_id, sourced from the SAME cluster node-identity
    resolver the rest of the cluster code already treats as authoritative
    (src/code_indexer/server/utils/cluster_node_id.py's
    resolve_cluster_node_id()) -- resolved by the CALLER (lifespan.py) and
    passed in, never recomputed inside manager.py itself.

RED-phase note: before this fix, TelemetryManager.__init__ has no
cluster_node_id parameter at all, and _initialize_otel()'s Resource.create()
call only sets SERVICE_NAME/DEPLOYMENT_ENVIRONMENT. The presence assertions
below (service.version / cidx.cluster.node_id in the resource attributes)
fail against that code -- this is the discriminating RED signal proving the
gap the AC describes. After the fix those same assertions pass, which also
serves as evidence that the pre-fix Resource genuinely lacked the fields.

All tests use the REAL OpenTelemetry SDK (Resource, TracerProvider,
MeterProvider) -- no mocking of the code under test, per MESSI Rule #1.
The collector endpoint below is never actually dialed by these tests
(TelemetryManager only configures the exporter; nothing here awaits an
export cycle), and mirrors the same placeholder endpoint the pre-existing
sibling suite (test_telemetry_manager.py) already standardizes on -- pulled
into one module constant here instead of a literal repeated per-test.
"""

from __future__ import annotations

import socket
from contextlib import contextmanager

from code_indexer.server.utils.config_manager import TelemetryConfig
from code_indexer.server.utils.cluster_node_id import resolve_cluster_node_id

_TEST_COLLECTOR_ENDPOINT = "http://localhost:4317"


def _meter_provider_resource(meter_provider):
    """opentelemetry-sdk's MeterProvider (unlike TracerProvider) does not
    expose a public `.resource` attribute -- it is only reachable via the
    internal `_sdk_config.resource` (verified against the installed SDK,
    opentelemetry-sdk 1.39.1). Centralized here so every test reads it the
    same way."""
    return meter_provider._sdk_config.resource


@contextmanager
def _telemetry_manager(**config_kwargs):
    """Construct a real TelemetryManager (enabled, real OTEL SDK) and
    guarantee shutdown(), splitting out cluster_node_id (a TelemetryManager
    constructor kwarg, not a TelemetryConfig field)."""
    from code_indexer.server.telemetry import TelemetryManager

    cluster_node_id = config_kwargs.pop("cluster_node_id", None)
    config_kwargs.setdefault("enabled", True)
    config_kwargs.setdefault("collector_endpoint", _TEST_COLLECTOR_ENDPOINT)
    config = TelemetryConfig(**config_kwargs)
    manager = TelemetryManager(config, cluster_node_id=cluster_node_id)
    try:
        yield manager
    finally:
        manager.shutdown()


class TestTelemetryManagerServiceVersionAttribute:
    """AC6: service.version resource attribute sourced from __version__."""

    def test_tracer_resource_includes_service_version_from_package_version(self):
        from code_indexer import __version__

        with _telemetry_manager() as manager:
            resource = manager.tracer_provider.resource  # type: ignore[attr-defined]
            assert resource.attributes.get("service.version") == __version__

    def test_meter_provider_resource_also_includes_service_version(self):
        from code_indexer import __version__

        with _telemetry_manager(export_metrics=True) as manager:
            resource = _meter_provider_resource(manager.meter_provider)
            assert resource.attributes.get("service.version") == __version__


class TestTelemetryManagerClusterNodeIdAttributeSupplied:
    """AC6: cidx.cluster.node_id resource attribute, when the caller supplies one."""

    def test_resource_includes_cluster_node_id_when_provided(self):
        with _telemetry_manager(cluster_node_id="node-42-cidx") as manager:
            resource = manager.tracer_provider.resource  # type: ignore[attr-defined]
            assert resource.attributes.get("cidx.cluster.node_id") == "node-42-cidx"

    def test_cluster_node_id_property_exposes_configured_value(self):
        from code_indexer.server.telemetry import TelemetryManager

        config = TelemetryConfig(enabled=False)
        manager = TelemetryManager(config, cluster_node_id="my-node-cidx")

        assert manager.cluster_node_id == "my-node-cidx"

    def test_solo_mode_fallback_via_shared_resolver_is_preserved(self):
        """AC6 gherkin: 'in solo (non-cluster) mode, that resolver's existing
        fallback behavior applies unchanged -- this AC does not invent a
        second/competing identity scheme'.

        Simulates the lifespan.py call site: resolve_cluster_node_id(None)
        (no cluster.node_id configured, i.e. solo mode) produces
        f"{hostname}-cidx", and that exact value must land on the resource
        when passed through to TelemetryManager.
        """
        solo_mode_node_id = resolve_cluster_node_id(None)
        assert solo_mode_node_id == f"{socket.gethostname()}-cidx"

        with _telemetry_manager(cluster_node_id=solo_mode_node_id) as manager:
            resource = manager.tracer_provider.resource  # type: ignore[attr-defined]
            assert resource.attributes.get("cidx.cluster.node_id") == (
                f"{socket.gethostname()}-cidx"
            )


class TestTelemetryManagerClusterNodeIdAttributeOmitted:
    """AC6: manager.py must NEVER invent a second identity scheme -- when the
    caller passes no cluster_node_id, the attribute is simply absent rather
    than manager.py computing its own fallback value."""

    def test_resource_omits_cluster_node_id_when_not_provided(self):
        with _telemetry_manager() as manager:
            resource = manager.tracer_provider.resource  # type: ignore[attr-defined]
            assert "cidx.cluster.node_id" not in resource.attributes


class TestTelemetryManagerResourceSharedAcrossSignals:
    """AC6: both new attributes must appear identically on the resource
    shared by the tracer provider AND the meter provider (Resource.create()
    is called once and passed to both providers)."""

    def test_tracer_and_meter_resources_carry_identical_new_attributes(self):
        with _telemetry_manager(
            export_traces=True,
            export_metrics=True,
            cluster_node_id="shared-node-cidx",
        ) as manager:
            tracer_resource = manager.tracer_provider.resource  # type: ignore[attr-defined]
            meter_resource = _meter_provider_resource(manager.meter_provider)

            for key in ("service.version", "cidx.cluster.node_id"):
                assert tracer_resource.attributes.get(
                    key
                ) == meter_resource.attributes.get(key), (
                    f"{key} differs between tracer and meter resources"
                )
                assert tracer_resource.attributes.get(key) is not None


class TestGetTelemetryManagerThreadsClusterNodeId:
    """get_telemetry_manager() must accept and thread cluster_node_id through
    to the TelemetryManager it constructs (used by lifespan.py)."""

    def test_get_telemetry_manager_passes_cluster_node_id_through(self):
        from code_indexer.server.telemetry import (
            get_telemetry_manager,
            reset_telemetry_manager,
        )

        reset_telemetry_manager()

        config = TelemetryConfig(
            enabled=True,
            collector_endpoint=_TEST_COLLECTOR_ENDPOINT,
        )
        manager = get_telemetry_manager(config, cluster_node_id="factory-node-cidx")

        try:
            assert manager.cluster_node_id == "factory-node-cidx"
            resource = manager.tracer_provider.resource  # type: ignore[attr-defined]
            assert (
                resource.attributes.get("cidx.cluster.node_id") == "factory-node-cidx"
            )
        finally:
            reset_telemetry_manager()
