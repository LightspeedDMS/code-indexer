"""The cidx-depmap Feature object, as the SDK contract sees it."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, cast
from unittest.mock import patch

import pytest

from cidx_depmap import __version__
from cidx_depmap.config import DEPMAP_CONFIG_SECTION, OWNED_KEYS, default_values
from cidx_depmap.feature import REQUIRED_CAPABILITIES, DepMapFeature, make_feature
from cidx_depmap.server import lifecycle, runtime
from cidx_depmap.server.migrations import DEPMAP_MIGRATIONS
from cidx_sdk import FeatureContext, ROUTER_AUTH_HYBRID, Feature, sdk_satisfies
from cidx_sdk.capabilities import ACCESS_PROJECTION, CONFIG
from cidx_sdk.feature import HEALTHY

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


@pytest.fixture
def no_context_left() -> Iterator[None]:
    """Leave no feature context or service behind."""
    previous = runtime.CONTEXT.get()
    previous_service = runtime.SERVICE.get()
    runtime.CONTEXT.uninstall()
    runtime.SERVICE.uninstall()
    yield
    runtime.CONTEXT.uninstall()
    runtime.SERVICE.uninstall()
    if previous is not None:
        runtime.CONTEXT.install(previous)
    if previous_service is not None:
        runtime.SERVICE.install(previous_service)


class _ContextImpl:
    def __init__(self, capabilities: Dict[str, object]) -> None:
        self._capabilities = capabilities

    def has_capability(self, name: str) -> bool:
        return name in self._capabilities

    def capability(self, name: str) -> object:
        return self._capabilities[name]


def _Context(capabilities: Dict[str, object]) -> FeatureContext:
    """A started feature's context holding *capabilities*."""
    return cast(FeatureContext, _ContextImpl(capabilities))


class _Section:
    def __init__(self, values: Dict[str, object]) -> None:
        self._values = values

    def runtime_config(self) -> object:
        return None

    def section(self) -> Dict[str, object]:
        return dict(self._values)

    def subscribe(self, callback: Any) -> Any:
        return lambda: None


def test_factory_returns_a_feature_named_depmap() -> None:
    feature = make_feature()
    assert isinstance(feature, Feature)
    assert isinstance(feature, DepMapFeature)
    assert feature.manifest.name == "depmap"
    assert feature.manifest.version == __version__
    assert sdk_satisfies(feature.manifest.requires_sdk)
    assert set(REQUIRED_CAPABILITIES) == {CONFIG, ACCESS_PROJECTION}


def test_entry_point_is_declared_under_the_feature_name() -> None:
    text = _PYPROJECT.read_text(encoding="utf-8")
    group = re.search(
        r'^\[project\.entry-points\."code_indexer\.features"\]\n(.*?)(?:\n\[|\Z)',
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert group is not None
    assert 'depmap = "cidx_depmap.feature:make_feature"' in group.group(1)


def test_tools_ship_their_docs_in_the_wheel() -> None:
    tools = make_feature().tools()
    assert [tool.name for tool in tools] == [
        "depmap_find_consumers",
        "depmap_get_cross_domain_graph",
        "depmap_get_domain_summary",
        "depmap_get_hub_domains",
        "depmap_get_repo_domains",
        "depmap_get_stale_domains",
        "trigger_dependency_analysis",
    ]
    for tool in tools:
        assert tool.doc_markdown.startswith(f"---\nname: {tool.name}\n")


def test_routers_are_the_page_and_the_admin_routes() -> None:
    page, rest = make_feature().routers()
    assert page.prefix == "/admin/dependency-map"
    assert page.login_redirect is True and page.auth == ROUTER_AUTH_HYBRID
    assert page.static_dir is not None
    assert (Path(page.static_dir) / "dependency_map.js").is_file()
    assert rest.prefix == "/admin" and rest.auth == ROUTER_AUTH_HYBRID
    assert not rest.login_redirect
    paths = {route.path for route in cast(Any, rest.router).routes}
    assert "/dependency-map/trigger" in paths
    assert "/partials/depmap-job-status" in paths


def test_config_section_is_the_former_claude_integration_keys() -> None:
    names = {key.name for key in DEPMAP_CONFIG_SECTION.keys}
    assert names == OWNED_KEYS
    assert all(not key.bootstrap for key in DEPMAP_CONFIG_SECTION.keys)
    assert default_values()["dependency_map_interval_hours"] == 168


def test_config_section_keeps_the_former_form_bounds_and_restart_note() -> None:
    by_name = {key.name: key for key in DEPMAP_CONFIG_SECTION.keys}
    fact_check = by_name["fact_check_timeout_seconds"]
    assert (fact_check.minimum, fact_check.maximum) == (60, 3600)
    domains = by_name["refinement_domains_per_run"]
    assert (domains.minimum, domains.maximum) == (1, 50)
    assert {k.name for k in DEPMAP_CONFIG_SECTION.keys if k.restart_required} == {
        "dependency_map_enabled"
    }
    assert DEPMAP_CONFIG_SECTION.title == "Dependency Map"
    assert all(key.label for key in DEPMAP_CONFIG_SECTION.keys)


def test_migration_1_adopts_the_core_tables_without_ddl() -> None:
    (migration,) = DEPMAP_MIGRATIONS
    assert migration.version == 1
    migration.apply(object())  # no DDL: any connection is fine


def test_claude_config_view_answers_owned_keys_from_the_section(
    no_context_left: None,
) -> None:
    core = SimpleNamespace(max_concurrent_claude_cli=3, dependency_map_enabled=True)
    view = runtime.DepMapClaudeConfig(core)
    # Not started: the wrapped object's own value (library tests).
    assert view.dependency_map_enabled is True
    assert view.max_concurrent_claude_cli == 3
    assert view.refinement_interval_hours == 24
    runtime.CONTEXT.install(
        _Context({CONFIG: _Section({"dependency_map_enabled": False})})
    )
    assert view.dependency_map_enabled is False
    assert view.max_concurrent_claude_cli == 3


def test_config_manager_view_wraps_the_claude_config(no_context_left: None) -> None:
    service = SimpleNamespace(
        get_claude_integration_config=lambda: SimpleNamespace(x=1),
        server_dir="/srv",
    )
    manager = runtime.DepMapConfigManager(service)
    assert manager.server_dir == "/srv"
    assert isinstance(
        manager.get_claude_integration_config(), runtime.DepMapClaudeConfig
    )


def test_description_fact_check_follows_its_setting(no_context_left: None) -> None:
    check = lifecycle.DescriptionFactCheck()
    assert check.enabled() is False
    runtime.CONTEXT.install(
        _Context({CONFIG: _Section({"dep_map_fact_check_enabled": True})})
    )
    assert check.enabled() is True


def test_startup_without_a_server_app_starts_no_service(
    no_context_left: None,
) -> None:
    from cidx_depmap._platform import capabilities

    host = SimpleNamespace(app_attr=lambda name: None)
    with patch.object(lifecycle, "server_platform", return_value=host):
        started = lifecycle.DepMapRuntime()
        started.start(_Context({}))
    assert capabilities.DESCRIPTION_FACT_CHECK_HOST.get() is not None
    started.stop()
    assert capabilities.DESCRIPTION_FACT_CHECK_HOST.get() is None
    assert runtime.CONTEXT.get() is None
    assert runtime.dependency_map_service() is None


class _StartedService:
    def __init__(self) -> None:
        self.stopped = False

    def stop_scheduler(self) -> None:
        self.stopped = True


def test_started_service_lives_in_the_feature_slot_not_on_the_app_state(
    no_context_left: None,
) -> None:
    from starlette.datastructures import State

    state = State()
    service = _StartedService()

    def start_service(given: Any) -> Any:
        assert given is state
        return service

    host = SimpleNamespace(app_attr=lambda name: SimpleNamespace(state=state))
    with (
        patch.object(lifecycle, "server_platform", return_value=host),
        patch.object(lifecycle, "_start_service", start_service),
    ):
        started = lifecycle.DepMapRuntime()
        started.start(_Context({}))
    assert runtime.dependency_map_service() is service
    assert dict(state._state) == {}
    started.stop()
    assert service.stopped is True
    assert runtime.dependency_map_service() is None


def test_a_failed_service_start_leaves_the_slot_empty(no_context_left: None) -> None:
    def start_service(given: Any) -> Any:
        raise RuntimeError("no tracking backend")

    host = SimpleNamespace(app_attr=lambda name: SimpleNamespace(state=object()))
    with (
        patch.object(lifecycle, "server_platform", return_value=host),
        patch.object(lifecycle, "_start_service", start_service),
    ):
        started = lifecycle.DepMapRuntime()
        started.start(_Context({}))
    assert runtime.dependency_map_service() is None
    started.stop()


def test_health_reports_the_packaged_assets() -> None:
    (report,) = make_feature().health()
    assert report.component == "depmap-assets"
    assert report.status == HEALTHY
