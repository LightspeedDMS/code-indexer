"""Feature registry, lifespan wiring and health surfaces (modular phase 3).

Covers: startup/shutdown called once per process in dependency order, a
failing on_startup stopping startup, the zero-feature default, and the
``features`` block on HealthCheckService, REST ``GET /health`` and MCP
``check_health``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List

import pytest
from cidx_sdk import (
    SDK_VERSION,
    BaseFeature,
    FeatureContext,
    FeatureManifest,
    FeatureNotInstalledError,
    FeatureStartupError,
)
from cidx_sdk.errors import CapabilityUnavailableError

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.features.loader import (
    FeatureCandidate,
    discover_installed_features,
)
from code_indexer.server.features.registry import (
    FeatureRegistry,
    feature_health_payload,
    get_active_registry,
)
from code_indexer.server.startup.feature_wiring import (
    build_feature_registry,
    load_configured_features,
    start_features,
    stop_features,
)

_LIFESPAN = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "code_indexer"
    / "server"
    / "startup"
    / "lifespan.py"
)


def _config(*names: str) -> SimpleNamespace:
    return SimpleNamespace(enabled_features=list(names))


def _calls(registry: FeatureRegistry) -> Dict[str, List[str]]:
    return {
        item.manifest.name: list(getattr(item.feature, "calls"))
        for item in registry.loaded
        if hasattr(item.feature, "calls")
    }


def _app() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace())


def test_zero_features_by_default_builds_an_empty_registry() -> None:
    registry = build_feature_registry(SimpleNamespace(), discover=lambda: {})
    registry.start()
    registry.stop()
    assert registry.health_payload() == {
        "sdk_version": SDK_VERSION,
        "loaded": [],
        "installed_not_enabled": [],
    }


def test_no_active_registry_means_no_features_block() -> None:
    assert get_active_registry() is None
    assert feature_health_payload() is None


def test_startup_and_shutdown_run_once_in_dependency_order(
    installed_sample_features: Dict[str, str],
) -> None:
    registry = build_feature_registry(_config("sample-dependent", "sample"))
    order: List[str] = []

    def recording(name: str, hook: Callable[[FeatureContext], None]) -> Any:
        def on_startup(ctx: FeatureContext) -> None:
            order.append(name)
            hook(ctx)

        return on_startup

    for item in registry.loaded:
        feature: Any = item.feature
        feature.on_startup = recording(item.manifest.name, feature.on_startup)
    registry.start()
    registry.start()
    registry.stop()
    registry.stop()
    assert order == ["sample", "sample-dependent"]
    assert _calls(registry) == {
        "sample": ["startup", "shutdown"],
        "sample-dependent": ["startup", "shutdown"],
    }


def test_context_without_providers_offers_no_capabilities(
    installed_sample_features: Dict[str, str],
) -> None:
    registry = build_feature_registry(_config("sample"))
    registry.start()
    ctx: FeatureContext = getattr(registry.loaded[0].feature, "contexts")[0]
    assert isinstance(ctx, FeatureContext)
    assert ctx.feature_name == "sample"
    assert ctx.sdk_version == SDK_VERSION
    assert ctx.provided_capabilities() == frozenset()
    with pytest.raises(CapabilityUnavailableError):
        ctx.capability("config")


class _FailingStartup(BaseFeature):
    def on_startup(self, ctx: FeatureContext) -> None:
        raise OSError("disk gone")


def test_failing_startup_stops_started_features_and_raises(
    installed_sample_features: Dict[str, str],
) -> None:
    candidates = discover_installed_features()
    candidates["failing"] = FeatureCandidate(
        name="failing",
        load=lambda: lambda: _FailingStartup(
            FeatureManifest(
                name="failing",
                version="1.0.0",
                requires_sdk=">=0.1",
                depends_on=("sample",),
            )
        ),
        source="inline:failing",
    )
    registry = build_feature_registry(
        _config("failing", "sample"), discover=lambda: candidates
    )
    with pytest.raises(FeatureStartupError, match="'failing'.*OSError"):
        registry.start()
    assert _calls(registry)["sample"] == ["startup", "shutdown"]
    registry.stop()
    assert _calls(registry)["sample"] == ["startup", "shutdown"]


def test_health_lists_loaded_and_installed_not_enabled(
    installed_sample_features: Dict[str, str],
) -> None:
    registry = build_feature_registry(_config("sample"))
    registry.start()
    payload = registry.health_payload()
    assert payload["sdk_version"] == SDK_VERSION
    # Every installed add-on is listed; the in-repo ones (xray) too.
    assert [
        name for name in payload["installed_not_enabled"] if name.startswith("sample")
    ] == ["sample-dependent", "sample-future-sdk"]
    (entry,) = payload["loaded"]
    assert entry["name"] == "sample"
    assert entry["version"] == "1.2.0"
    assert entry["started"] is True
    assert entry["health"] == [
        {
            "component": "sample-cache",
            "status": "healthy",
            "detail": {"entries": 0},
            "reasons": [],
        }
    ]


class _BrokenHealth(BaseFeature):
    def health(self) -> Any:
        raise RuntimeError("probe failed")


def test_a_raising_health_hook_reports_unhealthy_instead_of_failing() -> None:
    broken = FeatureCandidate(
        name="broken",
        load=lambda: lambda: _BrokenHealth(
            FeatureManifest(name="broken", version="1.0.0", requires_sdk=">=0.1")
        ),
        source="inline:broken",
    )
    registry = build_feature_registry(
        _config("broken"), discover=lambda: {"broken": broken}
    )
    (report,) = registry.health_payload()["loaded"][0]["health"]
    assert report["status"] == "unhealthy"
    assert report["reasons"] == ["health() raised RuntimeError"]


async def test_lifespan_helpers_publish_start_stop_and_clear(
    installed_sample_features: Dict[str, str],
) -> None:
    app = _app()
    registry = await load_configured_features(app, _config("sample"))
    assert app.state.feature_registry is registry
    assert get_active_registry() is registry
    await start_features(app)
    await start_features(app)
    await stop_features(app)
    await stop_features(app)
    assert _calls(registry) == {"sample": ["startup", "shutdown"]}
    assert get_active_registry() is None


async def test_lifespan_helper_refuses_an_enabled_feature_that_is_missing(
    installed_sample_features: Dict[str, str],
) -> None:
    with pytest.raises(FeatureNotInstalledError):
        await load_configured_features(_app(), _config("cidx-absent"))
    assert get_active_registry() is None


async def test_stop_without_load_is_a_no_op() -> None:
    await stop_features(_app())
    assert get_active_registry() is None


def test_lifespan_loads_before_starting_and_stops_after_yield() -> None:
    """The three helpers are wired into the real lifespan, in order."""
    source = _LIFESPAN.read_text(encoding="utf-8")
    load = source.index("await load_configured_features(app, startup_config)")
    start = source.index("await start_features(app)")
    running = source.index("yield  # Server is now running")
    stop = source.index("await stop_features(app)")
    assert load < start < running < stop
    assert len(re.findall(r"await (start|stop)_features\(app\)", source)) == 2


def _admin() -> User:
    return User(
        username="feature-admin",
        password_hash="unused-hash",
        role=UserRole.ADMIN,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def active_sample_registry(
    installed_sample_features: Dict[str, str],
) -> FeatureRegistry:
    from code_indexer.server.features.registry import set_active_registry

    registry = build_feature_registry(_config("sample"))
    registry.start()
    set_active_registry(registry)
    return registry


def test_system_health_carries_the_features_block(
    tmp_path: Path, active_sample_registry: FeatureRegistry
) -> None:
    from code_indexer.server.services.health_service import HealthCheckService

    service = HealthCheckService()
    service.database_url = f"sqlite:///{tmp_path / 'cidx_server.db'}"
    health = service.get_system_health()
    assert health.features is not None
    assert [f["name"] for f in health.features["loaded"]] == ["sample"]


def test_mcp_check_health_lists_loaded_features(
    active_sample_registry: FeatureRegistry,
) -> None:
    from code_indexer.server.mcp.handlers.repos import check_health

    body = json.loads(check_health({}, _admin())["content"][0]["text"])
    assert body["success"] is True
    features = body["health"]["features"]
    assert features["sdk_version"] == SDK_VERSION
    assert [(f["name"], f["version"]) for f in features["loaded"]] == [
        ("sample", "1.2.0")
    ]


def test_rest_health_lists_loaded_features(
    active_sample_registry: FeatureRegistry,
) -> None:
    from fastapi.testclient import TestClient

    from code_indexer.server.app import create_app
    from code_indexer.server.auth.dependencies import get_current_user

    app = create_app()
    app.dependency_overrides[get_current_user] = _admin
    try:
        client = TestClient(app, raise_server_exceptions=True)
        response = client.get("/health")
        assert response.status_code == 200
        features = response.json()["features"]
        assert [f["name"] for f in features["loaded"]] == ["sample"]
    finally:
        app.dependency_overrides.clear()
