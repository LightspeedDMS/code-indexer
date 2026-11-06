"""A feature add-on used only by the feature tests.

Not shipped: it lives under tests/ and is made discoverable by the tests
themselves (a generated ``*.dist-info`` with an ``entry_points.txt`` on
``sys.path``). It has its own top-level import name, as a real add-on would,
and imports ``cidx_sdk`` plus the frameworks its contributions are made of
(FastAPI routers, a Click command) -- never ``code_indexer``.

It contributes one of everything, each named after the feature so the
dependent variant can load beside it:

==================  =========================================================
hook                contribution (for feature ``sample``)
==================  =========================================================
tools()             MCP tool ``sample_ping`` (permission ``query_repos``)
routers()           ``GET /api/features/sample/notes``,
                    ``POST /api/features/sample/jobs/sweep``
ui_pages()          ``GET /admin/features/sample`` (nav "Sample Feature")
cli_commands()      ``cidx sample-hello``
config_section()    ``greeting``, ``simulate_unhealthy`` (runtime),
                    ``mode`` (bootstrap)
migrations()        1: table ``sample_notes`` (per feature name)
background_jobs()   ``sweep`` (operation ``sample_sweep``), hourly
health()            ``sample-cache``: healthy, or unhealthy when
                    ``simulate_unhealthy`` is set
==================  =========================================================
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, cast

import click
from fastapi import APIRouter, HTTPException, Request

from cidx_sdk import (
    PRINCIPAL_STATE_ATTR,
    RENDER_STATE_ATTR,
    BackgroundJob,
    BackgroundJobsCapability,
    BaseFeature,
    CliCommandContribution,
    ConfigCapability,
    ConfigKey,
    ConfigSection,
    FeatureContext,
    FeatureManifest,
    HealthReport,
    Migration,
    RouterContribution,
    StorageCapability,
    ToolContribution,
    UiPageContribution,
)
from cidx_sdk.capabilities import BACKGROUND_JOBS, CONFIG, STORAGE

ENTRY_POINTS = {
    "sample": "cidx_sample_feature:make_sample",
    "sample-dependent": "cidx_sample_feature:make_dependent",
    "sample-future-sdk": "cidx_sample_feature:make_future_sdk",
}

TEMPLATES = Path(__file__).resolve().parent / "templates"

_TOOL_DOC = """---
name: {tool}
category: guides
required_permission: query_repos
tl_dr: Answer pong from the {feature} feature add-on.
inputSchema:
  type: object
  properties: {{}}
  additionalProperties: false
---

Returns ``{{"pong": true}}`` with the caller and the feature's greeting.
"""


def _snake(name: str) -> str:
    return name.replace("-", "_")


def _mcp_text(payload: Mapping[str, Any]) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


class SampleFeature(BaseFeature):
    """Records every lifecycle call so tests can count them."""

    def __init__(self, manifest: FeatureManifest) -> None:
        super().__init__(manifest)
        self.calls: List[str] = []
        self.contexts: List[FeatureContext] = []
        self._ctx: Optional[FeatureContext] = None

    # ── names ────────────────────────────────────────────────────────────

    @property
    def table(self) -> str:
        return f"{_snake(self.manifest.name)}_notes"

    @property
    def page_path(self) -> str:
        return f"/admin/features/{self.manifest.name}"

    @property
    def api_prefix(self) -> str:
        return f"/api/features/{self.manifest.name}"

    # ── capabilities (available after on_startup) ────────────────────────

    def _require_ctx(self) -> FeatureContext:
        if self._ctx is None:
            raise HTTPException(status_code=503, detail="feature not started")
        return self._ctx

    def _config(self) -> ConfigCapability:
        return cast(ConfigCapability, self._require_ctx().capability(CONFIG))

    def _storage(self) -> StorageCapability:
        return cast(StorageCapability, self._require_ctx().capability(STORAGE))

    def _jobs(self) -> BackgroundJobsCapability:
        return cast(
            BackgroundJobsCapability, self._require_ctx().capability(BACKGROUND_JOBS)
        )

    def _setting(self, key: str) -> Optional[object]:
        """A config value, or None before startup or without the capability
        (a registry built with no capabilities, as some tests do)."""
        if self._ctx is None or not self._ctx.has_capability(CONFIG):
            return None
        return self._config().section().get(key)

    def _greeting(self) -> Optional[object]:
        return self._setting("greeting")

    # ── hooks ────────────────────────────────────────────────────────────

    def tools(self) -> Sequence[ToolContribution]:
        tool = f"{_snake(self.manifest.name)}_ping"

        def ping(params: Dict[str, Any], user: Any) -> Dict[str, Any]:
            return _mcp_text(
                {"pong": True, "user": user.username, "greeting": self._greeting()}
            )

        return (
            ToolContribution(
                name=tool,
                handler=ping,
                doc_markdown=_TOOL_DOC.format(tool=tool, feature=self.manifest.name),
            ),
        )

    def routers(self) -> Sequence[RouterContribution]:
        router = APIRouter()

        @router.get("/notes")
        def list_notes(request: Request) -> Dict[str, Any]:
            principal = getattr(request.state, PRINCIPAL_STATE_ATTR)
            with self._storage().connection() as conn:
                rows = conn.execute(
                    f"SELECT note FROM {self.table} ORDER BY id"
                ).fetchall()
            return {"viewer": principal.username, "notes": [row[0] for row in rows]}

        @router.post("/jobs/sweep")
        def run_sweep(request: Request) -> Dict[str, Any]:
            principal = getattr(request.state, PRINCIPAL_STATE_ATTR)
            job_id = self._jobs().run_registered("sweep", submitter=principal.username)
            return {"job_id": job_id}

        return (
            RouterContribution(
                router=router,
                prefix=self.api_prefix,
                required_permission="query_repos",
            ),
        )

    def ui_pages(self) -> Sequence[UiPageContribution]:
        router = APIRouter()
        title = f"{self.manifest.name.title()} Feature"

        @router.get(self.page_path)
        def page(request: Request) -> object:
            render = getattr(request.state, RENDER_STATE_ATTR)
            return render("sample_page.html", {"greeting": self._greeting()})

        return (
            UiPageContribution(
                path=self.page_path,
                title=title,
                router=router,
                nav_label=title,
                required_permission="query_repos",
                template_dirs=(str(TEMPLATES),),
            ),
        )

    def cli_commands(self) -> Sequence[CliCommandContribution]:
        name = f"{self.manifest.name}-hello"

        @click.command(
            name=name, help=f"Say hello from the {self.manifest.name} add-on."
        )
        def hello() -> None:
            click.echo(f"hello from {self.manifest.name}")

        return (CliCommandContribution(name=name, command=hello),)

    def config_section(self) -> Optional[ConfigSection]:
        return ConfigSection(
            name=f"{self.manifest.name.title()} Feature",
            keys=(
                ConfigKey("greeting", "hello", description="Greeting shown"),
                ConfigKey("simulate_unhealthy", False, description="Report unhealthy"),
                ConfigKey("mode", "standard", bootstrap=True, description="Mode"),
            ),
        )

    def migrations(self) -> Sequence[Migration]:
        table = self.table

        def create(connection: Any) -> None:
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS {table} "
                "(id INTEGER PRIMARY KEY, note TEXT NOT NULL)"
            )

        return (Migration(1, f"create {table}", create),)

    def background_jobs(self) -> Sequence[BackgroundJob]:
        table = self.table

        def sweep(ctx: FeatureContext) -> None:
            storage = cast(StorageCapability, ctx.capability(STORAGE))
            with storage.connection() as conn:
                conn.execute(f"INSERT INTO {table} (note) VALUES ('swept')")

        return (
            BackgroundJob(
                name="sweep",
                operation_type=f"{_snake(self.manifest.name)}_sweep",
                run=sweep,
                interval_seconds=3600.0,
            ),
        )

    def health(self) -> Sequence[HealthReport]:
        unhealthy = self._setting("simulate_unhealthy") is True
        return (
            HealthReport(
                component=f"{self.manifest.name}-cache",
                status="unhealthy" if unhealthy else "healthy",
                detail={"entries": 0},
                reasons=("simulated by configuration",) if unhealthy else (),
            ),
        )

    def on_startup(self, ctx: FeatureContext) -> None:
        self.calls.append("startup")
        self.contexts.append(ctx)
        self._ctx = ctx

    def on_shutdown(self, ctx: FeatureContext) -> None:
        self.calls.append("shutdown")
        self._ctx = None


def make_sample() -> SampleFeature:
    return SampleFeature(
        FeatureManifest(name="sample", version="1.2.0", requires_sdk=">=0.1,<1")
    )


def make_dependent() -> SampleFeature:
    return SampleFeature(
        FeatureManifest(
            name="sample-dependent",
            version="0.3.0",
            requires_sdk=">=0.1,<1",
            depends_on=("sample",),
        )
    )


def make_future_sdk() -> SampleFeature:
    return SampleFeature(
        FeatureManifest(
            name="sample-future-sdk", version="1.0.0", requires_sdk=">=99.0"
        )
    )
