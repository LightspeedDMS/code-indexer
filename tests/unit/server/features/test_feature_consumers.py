"""Hook consumers in isolation: config values, migrations, tools, web
refusals, the job scheduler (modular phase 4a). The front-door proof is
``test_feature_front_door.py``; these pin the edge cases."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Sequence

import pytest
from cidx_sdk import (
    BackgroundJob,
    BaseFeature,
    ConfigKey,
    ConfigSection,
    FeatureLoadError,
    FeatureManifest,
    Migration,
    RouterContribution,
    ToolContribution,
    UiPageContribution,
)
from fastapi import APIRouter, FastAPI

from code_indexer.server.features import config as feature_config
from code_indexer.server.features.capabilities import JobAlreadyActiveError
from code_indexer.server.features.jobs import FeatureJobScheduler
from code_indexer.server.features.loader import FeatureCandidate, LoadedFeature
from code_indexer.server.features.migrations import (
    feature_schema_versions_sqlite,
    migration_plan,
    run_feature_migrations_sqlite,
)
from code_indexer.server.features.tools import (
    install_feature_tools,
    installed_feature_tools,
)
from code_indexer.server.features.web import (
    install_feature_config_routes,
    install_feature_web,
)
from code_indexer.server.startup.feature_wiring import build_feature_registry

_SECTION = ConfigSection(
    name="Example",
    keys=(
        ConfigKey("limit", 10),
        ConfigKey("ratio", 0.5),
        ConfigKey("enabled", False),
        ConfigKey("label", "x"),
        ConfigKey("root", "/srv", bootstrap=True),
    ),
)


class _Feature(BaseFeature):
    def __init__(self, name: str = "example", **hooks: Any) -> None:
        super().__init__(
            FeatureManifest(name=name, version="1.0.0", requires_sdk=">=0.1")
        )
        self._hooks = hooks

    def tools(self) -> Sequence[ToolContribution]:
        return tuple(self._hooks.get("tools", ()))

    def routers(self) -> Sequence[RouterContribution]:
        return tuple(self._hooks.get("routers", ()))

    def ui_pages(self) -> Sequence[UiPageContribution]:
        return tuple(self._hooks.get("ui_pages", ()))

    def migrations(self) -> Sequence[Migration]:
        return tuple(self._hooks.get("migrations", ()))

    def background_jobs(self) -> Sequence[BackgroundJob]:
        return tuple(self._hooks.get("jobs", ()))


def _factory_loader(feature: _Feature) -> Callable[[], object]:
    def load() -> Callable[[], _Feature]:
        return lambda: feature

    return load


def _loaded(*features: _Feature) -> List[LoadedFeature]:
    candidates = {
        f.manifest.name: FeatureCandidate(
            name=f.manifest.name, load=_factory_loader(f), source="inline"
        )
        for f in features
    }
    registry = build_feature_registry(
        SimpleNamespace(enabled_features=list(candidates)),
        discover=lambda: candidates,
    )
    return list(registry.loaded)


# ── config ───────────────────────────────────────────────────────────────────


def test_section_values_overlay_runtime_and_bootstrap_on_defaults() -> None:
    config = SimpleNamespace(
        feature_settings={"example": {"limit": 3, "root": "ignored"}},
        feature_bootstrap_settings={"example": {"root": "/data", "limit": 99}},
    )
    values = feature_config.section_values("example", _SECTION, config)
    assert values == {
        "limit": 3,
        "ratio": 0.5,
        "enabled": False,
        "label": "x",
        "root": "/data",
    }


@pytest.mark.parametrize(
    "updates,match",
    [
        ({"absent": 1}, "no setting"),
        ({"root": "/x"}, "bootstrap"),
        ({"limit": True}, "int"),
        ({"enabled": "yes"}, "bool"),
        ({"label": 3}, "str"),
    ],
)
def test_runtime_update_refusals(updates: Dict[str, Any], match: str) -> None:
    with pytest.raises(feature_config.FeatureConfigError, match=match):
        feature_config.validate_runtime_update("example", _SECTION, updates)


def test_runtime_update_mutator_merges_and_widens_int_to_float() -> None:
    candidate = SimpleNamespace(feature_settings={"other": {"a": 1}})
    feature_config.runtime_update_mutator("example", _SECTION, {"ratio": 1})(candidate)
    assert candidate.feature_settings == {"other": {"a": 1}, "example": {"ratio": 1.0}}
    assert isinstance(candidate.feature_settings["example"]["ratio"], float)


class _RecordingConfigService:
    def __init__(self) -> None:
        self.config = SimpleNamespace(feature_settings={})
        self.changes: List[Dict[str, Any]] = []

    def apply_audited_change(self, mutate: Any, *, actor: str, target_id: str) -> Any:
        mutate(self.config)
        self.changes.append({"actor": actor, "target_id": target_id})
        return self.config


def test_commit_runtime_update_is_one_audited_change() -> None:
    service = _RecordingConfigService()
    feature_config.commit_runtime_update(
        service, "example", _SECTION, {"limit": 4}, "admin"
    )
    assert service.config.feature_settings == {"example": {"limit": 4}}
    assert service.changes == [
        {"actor": "admin", "target_id": "feature_settings.example"}
    ]


def test_commit_runtime_update_refuses_before_committing() -> None:
    service = _RecordingConfigService()
    with pytest.raises(feature_config.FeatureConfigError, match="bootstrap"):
        feature_config.commit_runtime_update(
            service, "example", _SECTION, {"root": "/x"}, "admin"
        )
    assert service.changes == [] and service.config.feature_settings == {}


def test_describe_sections_carries_the_presentation_of_each_key() -> None:
    section = ConfigSection(
        name="example",
        title="Example Settings",
        description="What the example does.",
        keys=(
            ConfigKey(
                "limit",
                10,
                label="Limit",
                minimum=1,
                maximum=50,
                restart_required=True,
            ),
            ConfigKey("ratio", 0.5),
        ),
    )
    config = SimpleNamespace(feature_settings={}, feature_bootstrap_settings={})
    (described,) = feature_config.describe_sections([("example", section)], config)
    assert described["section"] == "example"
    assert described["title"] == "Example Settings"
    assert described["description"] == "What the example does."
    limit, ratio = described["keys"]
    assert (limit["label"], limit["minimum"], limit["maximum"]) == ("Limit", 1, 50)
    assert limit["restart_required"] is True
    assert (ratio["label"], ratio["minimum"], ratio["maximum"]) == ("ratio", None, None)
    assert ratio["restart_required"] is False


def test_describe_sections_titles_an_untitled_section_by_its_name() -> None:
    config = SimpleNamespace(feature_settings={}, feature_bootstrap_settings={})
    (described,) = feature_config.describe_sections([("example", _SECTION)], config)
    assert (described["title"], described["description"]) == ("Example", "")


def _render_feature_settings(sections: List[Dict[str, Any]]) -> str:
    """The config screen's Feature Settings partial over *sections*."""
    from jinja2 import Environment, FileSystemLoader

    import code_indexer.server.web as web_package

    templates = Path(web_package.__file__).parent / "templates"
    env = Environment(loader=FileSystemLoader(str(templates)), autoescape=True)
    view = {"sections": sections, "csrf_token": "tok"}
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(feature_config_view=lambda _request: view)
        )
    )
    return env.get_template("partials/feature_settings.html").render(request=request)


def test_feature_settings_partial_renders_labels_bounds_and_restart_notes() -> None:
    section = ConfigSection(
        name="example",
        title="Example Settings",
        description="What the example does.",
        keys=(
            ConfigKey("limit", 10, label="Limit", minimum=1, maximum=50),
            ConfigKey("ratio", 0.5, minimum=0.0, maximum=1.0),
            ConfigKey("enabled", True, restart_required=True),
            ConfigKey("label", "x"),
            ConfigKey("root", "/srv", bootstrap=True),
        ),
    )
    config = SimpleNamespace(feature_settings={}, feature_bootstrap_settings={})
    html = _render_feature_settings(
        feature_config.describe_sections([("example", section)], config)
    )
    assert 'id="feature-settings-example"' in html
    assert "Example Settings" in html and "What the example does." in html
    assert 'action="/admin/features/config/example"' in html
    assert 'value="tok"' in html
    assert (
        '<input type="number" name="setting__limit" value="10" min="1" max="50" '
        'step="1">' in html
    )
    assert 'name="setting__ratio" value="0.5" min="0.0" max="1.0" step="any"' in html
    assert '<input type="checkbox" name="setting__enabled" checked>' in html
    assert '<input type="text" name="setting__label" value="x">' in html
    assert "<code>/srv</code>" in html and "setting__root" not in html
    assert html.count("Requires server restart") == 1
    assert "Limit<br><code>limit</code>" in html


def test_feature_settings_partial_is_empty_without_sections() -> None:
    assert 'id="feature-settings"' not in _render_feature_settings([])


def test_feature_settings_fields_are_runtime_and_bootstrap() -> None:
    from code_indexer.server.services.config_service import BOOTSTRAP_KEYS
    from code_indexer.server.utils.config_manager import ServerConfig

    config = ServerConfig(server_dir="/srv/example")
    assert config.feature_settings == {} and config.feature_bootstrap_settings == {}
    assert feature_config.BOOTSTRAP_FIELD in BOOTSTRAP_KEYS
    assert feature_config.RUNTIME_FIELD not in BOOTSTRAP_KEYS


# ── migrations ───────────────────────────────────────────────────────────────


def _sql(statement: str) -> Callable[[Any], None]:
    def apply(connection: Any) -> None:
        connection.execute(statement)

    return apply


def _create(table: str) -> Migration:
    return Migration(1, f"create {table}", _sql(f"CREATE TABLE {table} (v INTEGER)"))


def test_sqlite_migrations_apply_once_with_a_version_row(tmp_path: Path) -> None:
    db = str(tmp_path / "server.db")
    plan = migration_plan(_loaded(_Feature(migrations=[_create("example_t")])))
    assert run_feature_migrations_sqlite(db, plan) == {"example": 1}
    assert run_feature_migrations_sqlite(db, plan) == {"example": 0}
    assert feature_schema_versions_sqlite(db) == {"example": 1}


def test_sqlite_failing_migration_rolls_back_and_raises(tmp_path: Path) -> None:
    db = str(tmp_path / "server.db")

    def broken(conn: Any) -> None:
        conn.execute("CREATE TABLE half (v INTEGER)")
        raise RuntimeError("boom")

    plan = migration_plan(
        _loaded(_Feature(migrations=[_create("ok_t"), Migration(2, "broken", broken)]))
    )
    with pytest.raises(RuntimeError, match="boom"):
        run_feature_migrations_sqlite(db, plan)
    assert feature_schema_versions_sqlite(db) == {"example": 1}
    with sqlite3.connect(db) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "ok_t" in tables and "half" not in tables


def test_sqlite_newer_schema_than_the_feature_applies_nothing(tmp_path: Path) -> None:
    db = str(tmp_path / "server.db")
    plan = migration_plan(_loaded(_Feature(migrations=[_create("x_t")])))
    run_feature_migrations_sqlite(db, plan)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE feature_schema_versions SET version = 5")
    assert run_feature_migrations_sqlite(db, plan) == {"example": 0}
    assert feature_schema_versions_sqlite(db) == {"example": 5}


@pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set; skipping real-PG feature migrations",
)
def test_postgres_migrations_run_under_the_runner_lock() -> None:
    from code_indexer.server.storage.postgres.migrations.runner import MigrationRunner

    table = "example_feature_pg_t"
    create = Migration(
        1,
        "create",
        _sql(f"CREATE TABLE IF NOT EXISTS {table} (v INTEGER)"),
    )
    plan = [("example-pg", [create])]
    with MigrationRunner(os.environ["TEST_POSTGRES_DSN"]) as runner:
        first = runner.run_feature_migrations(plan)
        second = runner.run_feature_migrations(plan)
    assert second == {"example-pg": 0}
    assert first in ({"example-pg": 1}, {"example-pg": 0})


# ── tools ────────────────────────────────────────────────────────────────────

_DOC = """---
name: {name}
category: guides
required_permission: {perm}
tl_dr: Example.
inputSchema:
  type: object
  properties: {{}}
---
Example tool.
"""


def _tool(name: str, perm: str = "query_repos", **kwargs: Any) -> ToolContribution:
    return ToolContribution(
        name=name,
        handler=lambda params, user: {"content": []},
        doc_markdown=kwargs.pop("doc", _DOC.format(name=name, perm=perm)),
        **kwargs,
    )


@pytest.fixture
def clean_tools() -> Any:
    yield
    install_feature_tools([])


def test_tools_install_and_reinstall_replaces(clean_tools: Any) -> None:
    from code_indexer.server.mcp.handlers import HANDLER_REGISTRY
    from code_indexer.server.mcp.tools import TOOL_REGISTRY

    install_feature_tools(_loaded(_Feature(tools=[_tool("example_one")])))
    assert TOOL_REGISTRY["example_one"]["required_permission"] == "query_repos"
    assert "example_one" in HANDLER_REGISTRY
    install_feature_tools(_loaded(_Feature(tools=[_tool("example_two")])))
    assert installed_feature_tools() == {"example_two": "example"}
    assert "example_one" not in TOOL_REGISTRY and "example_one" not in HANDLER_REGISTRY


@pytest.mark.parametrize(
    "tool,match",
    [
        (lambda: _tool("search_code"), "collides"),
        (lambda: _tool("example_x", doc=_DOC.format(name="other", perm="p")), "named"),
        (lambda: _tool("example_x", doc="no frontmatter"), "frontmatter"),
        (
            lambda: _tool("example_x", required_permission="manage_users"),
            "differs",
        ),
    ],
)
def test_tool_refusals(clean_tools: Any, tool: Any, match: str) -> None:
    with pytest.raises(FeatureLoadError, match=match):
        install_feature_tools(_loaded(_Feature(tools=[tool()])))


# ── web refusals ─────────────────────────────────────────────────────────────


def _router(path: str) -> APIRouter:
    router = APIRouter()
    router.add_api_route(path, lambda: {}, methods=["GET"])
    return router


def test_web_refuses_a_route_colliding_with_the_core() -> None:
    app = FastAPI()
    app.add_api_route("/api/example", lambda: {}, methods=["GET"])
    feature = _Feature(
        routers=[RouterContribution(router=_router("/example"), prefix="/api")]
    )
    with pytest.raises(FeatureLoadError, match="collides"):
        install_feature_web(app, _loaded(feature))


def test_web_refuses_a_route_colliding_with_an_included_core_router() -> None:
    # The core mounts its pages as included routers; FastAPI 0.16x keeps
    # them nested, so the check must look inside them.
    core = APIRouter()
    core.include_router(_router("/example"), prefix="/api")
    app = FastAPI()
    app.include_router(core)
    nested = APIRouter()
    nested.include_router(_router("/example"))
    feature = _Feature(routers=[RouterContribution(router=nested, prefix="/api")])
    with pytest.raises(FeatureLoadError, match="collides"):
        install_feature_web(app, _loaded(feature))


def test_web_refuses_a_non_router_and_an_unserved_page() -> None:
    with pytest.raises(FeatureLoadError, match="APIRouter"):
        install_feature_web(
            FastAPI(), _loaded(_Feature(routers=[RouterContribution(router=object())]))
        )
    page = UiPageContribution(path="/admin/x", title="X", router=_router("/admin/y"))
    with pytest.raises(FeatureLoadError, match="not served"):
        install_feature_web(FastAPI(), _loaded(_Feature(ui_pages=[page])))


def test_web_publishes_nav_entries_without_permission_to_everyone() -> None:
    app = FastAPI()
    page = UiPageContribution(
        path="/admin/x", title="X", router=_router("/admin/x"), nav_label="X"
    )
    install_feature_web(app, _loaded(_Feature(ui_pages=[page])))
    assert app.state.feature_nav(object()) == [{"label": "X", "path": "/admin/x"}]


def test_web_nav_entries_accumulate_across_installs() -> None:
    app = FastAPI()
    install_feature_config_routes(app)
    first = RouterContribution(router=_router(""), prefix="/admin/a", nav_label="A")
    second = RouterContribution(router=_router(""), prefix="/admin/b", nav_label="B")
    install_feature_web(app, _loaded(_Feature(routers=[first])))
    install_feature_web(app, _loaded(_Feature(routers=[second])))
    install_feature_config_routes(FastAPI())  # another app: this one keeps its nav
    assert app.state.feature_nav(object()) == [
        {"label": "A", "path": "/admin/a"},
        {"label": "B", "path": "/admin/b"},
    ]


def test_web_lists_a_routers_nav_entry_at_its_prefix() -> None:
    app = FastAPI()
    router = RouterContribution(router=_router(""), prefix="/admin/x", nav_label="X")
    install_feature_web(app, _loaded(_Feature(routers=[router])))
    assert app.state.feature_nav(object()) == [{"label": "X", "path": "/admin/x"}]


# ── job scheduler ────────────────────────────────────────────────────────────


class _Jobs:
    def __init__(self, error: Optional[Exception] = None) -> None:
        self.error = error
        self.runs: List[str] = []

    def run_registered(self, name: str, *, submitter: str) -> str:
        if self.error is not None:
            raise self.error
        self.runs.append(f"{name}:{submitter}")
        return "j1"


class _Ctx:
    def __init__(self, jobs: Optional[_Jobs]) -> None:
        self.jobs = jobs

    def has_capability(self, name: str) -> bool:
        return self.jobs is not None

    def capability(self, name: str) -> Any:
        return self.jobs


def _scheduler(jobs: Optional[_Jobs]) -> FeatureJobScheduler:
    return FeatureJobScheduler(
        SimpleNamespace(loaded=[], context_for=lambda n: _Ctx(jobs))
    )


_JOB = BackgroundJob("sweep", "op", lambda ctx: None, interval_seconds=60.0)


def test_scheduler_tick_submits_and_skips_when_active_elsewhere() -> None:
    jobs = _Jobs()
    assert _scheduler(jobs).tick("example", _JOB) == "j1"
    assert jobs.runs == ["sweep:system"]
    assert (
        _scheduler(_Jobs(JobAlreadyActiveError("busy"))).tick("example", _JOB) is None
    )
    assert _scheduler(_Jobs(OSError("down"))).tick("example", _JOB) is None
    assert _scheduler(None).tick("example", _JOB) is None


def test_scheduler_lists_only_recurring_jobs_and_stops() -> None:
    on_demand = BackgroundJob("once", "op", lambda ctx: None)
    loaded = _loaded(_Feature(jobs=[_JOB, on_demand]))
    scheduler = FeatureJobScheduler(SimpleNamespace(loaded=loaded))
    assert [e["job"].name for e in scheduler.recurring()] == ["sweep"]
    scheduler.start()
    scheduler.stop()


class _SectionFeature(_Feature):
    def config_section(self) -> ConfigSection:
        return _SECTION


def test_every_loaded_feature_section_is_consumed() -> None:
    """No feature's settings are hand-coded in the core: every loaded
    section is on the generic config screen."""
    candidates = {
        name: FeatureCandidate(
            name=name, load=_factory_loader(_SectionFeature(name)), source="inline"
        )
        for name in ("first", "example")
    }
    registry = build_feature_registry(
        SimpleNamespace(enabled_features=list(candidates)),
        discover=lambda: candidates,
    )
    assert [name for name, _section in registry.config_sections()] == [
        "first",
        "example",
    ]
