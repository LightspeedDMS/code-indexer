"""The cidx-scip Feature object, as the SDK contract sees it."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, cast

import click
import pytest

from cidx_sdk import Feature, IndexBuildRequest, sdk_satisfies
from cidx_sdk.capabilities import CONFIG
from cidx_sdk.feature import (
    DEGRADED,
    HEALTHY,
    INDEX_BUILD_RUN_LOCALLY,
)
from cidx_scip import __version__
from cidx_scip.cli import scip_group
from cidx_scip.config import SCIP_CONFIG_SECTION
from cidx_scip.feature import (
    CAPABILITIES_FOR_HANDLERS,
    ScipFeature,
    make_feature,
    tool_names,
)
from cidx_scip.server import index_builder, runtime
from cidx_scip.server.migrations import (
    LEGACY_SQLITE_FILENAME,
    SCIP_MIGRATIONS,
)

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


class _Section:
    """The ``config`` capability, reduced to the feature's section."""

    def __init__(self, values: Dict[str, object]) -> None:
        self._values = values

    def section(self) -> Dict[str, object]:
        return dict(self._values)


class _Context:
    """A started feature's context holding only ``config``."""

    def __init__(self, values: Dict[str, object]) -> None:
        self._config = _Section(values)

    def has_capability(self, name: str) -> bool:
        return name == CONFIG

    def capability(self, name: str) -> object:
        return self._config


@pytest.fixture
def section_values() -> Iterator[Any]:
    """Install a feature context whose section holds the given values."""

    def install(values: Optional[Dict[str, object]]) -> None:
        if values is None:
            runtime.CONTEXT.uninstall()
        else:
            runtime.CONTEXT.install(cast(Any, _Context(values)))

    yield install
    runtime.CONTEXT.uninstall()


def test_factory_returns_a_feature_named_scip() -> None:
    feature = make_feature()
    assert isinstance(feature, Feature)
    assert isinstance(feature, ScipFeature)
    assert feature.manifest.name == "scip"
    assert feature.manifest.version == __version__
    assert sdk_satisfies(feature.manifest.requires_sdk)
    assert set(CAPABILITIES_FOR_HANDLERS) <= set(feature.manifest.required_capabilities)
    assert feature.manifest.optional_seams == ("scip",)


def test_entry_point_is_declared_in_pyproject() -> None:
    text = _PYPROJECT.read_text(encoding="utf-8")
    assert re.search(
        r'^\[project\.entry-points\."code_indexer\.features"\]\s*\n'
        r'scip = "cidx_scip\.feature:make_feature"',
        text,
        re.MULTILINE,
    )


def test_every_tool_doc_names_its_tool() -> None:
    names = tool_names()
    assert "scip_definition" in names and "scip_callchain" in names
    for name in names:
        doc = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "cidx_scip"
            / "server"
            / "tool_docs"
            / f"{name}.md"
        ).read_text(encoding="utf-8")
        assert f"name: {name}\n" in doc


def test_cli_contribution_is_the_scip_group() -> None:
    (contribution,) = make_feature().cli_commands()
    assert contribution.name == "scip"
    assert contribution.command is scip_group
    assert isinstance(scip_group, click.Group)


def test_config_section_bounds_the_query_limits() -> None:
    by_name = {key.name: key for key in SCIP_CONFIG_SECTION.keys}
    depth = by_name["scip_dependency_depth"]
    assert (depth.default, depth.minimum, depth.maximum) == (3, 1, 10)
    assert by_name["scip_backend"].bootstrap is True
    assert SCIP_CONFIG_SECTION.bootstrap_keys == {"scip_backend"}


def test_config_section_marks_the_startup_read_keys_restart_required() -> None:
    assert {k.name for k in SCIP_CONFIG_SECTION.keys if k.restart_required} == {
        "scip_reference_limit",
        "scip_dependency_depth",
        "scip_callchain_max_depth",
        "scip_callchain_limit",
        "scip_workspace_retention_days",
    }
    assert SCIP_CONFIG_SECTION.title == "SCIP Code Intelligence"
    assert all(key.label for key in SCIP_CONFIG_SECTION.keys)


def test_backend_config_reads_the_bootstrap_key(section_values: Any) -> None:
    section_values(None)
    assert runtime.backend_config().scip_backend == "local"
    section_values({"scip_backend": "delegated-plugin"})
    assert runtime.backend_config().scip_backend == "delegated-plugin"


def test_health_is_healthy_under_local(section_values: Any) -> None:
    section_values({"scip_backend": "local"})
    (report,) = make_feature().health()
    assert report.status == HEALTHY
    assert report.detail == {"scip_backend": "local"}


def test_health_degrades_without_the_delegated_plugin(
    section_values: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cidx_scip.server import backend_plugin

    section_values({"scip_backend": "delegated-plugin"})
    monkeypatch.setattr(backend_plugin, "plugin_factory_available", lambda: False)
    (report,) = make_feature().health()
    assert report.status == DEGRADED
    assert "scip_backend" in report.reasons[0] or "plugin" in report.reasons[0]


def test_background_job_only_when_the_backfill_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CIDX_SCIP_AUTO_RECONCILE_ENABLED", raising=False)
    assert make_feature().background_jobs() == ()
    monkeypatch.setenv("CIDX_SCIP_AUTO_RECONCILE_ENABLED", "true")
    monkeypatch.setenv("CIDX_SCIP_RECONCILE_INTERVAL_SECONDS", "60")
    (job,) = make_feature().background_jobs()
    assert job.interval_seconds == 60.0
    monkeypatch.setenv("CIDX_SCIP_RECONCILE_INTERVAL_SECONDS", "0")
    assert make_feature().background_jobs() == ()


def test_index_builder_contribution(section_values: Any) -> None:
    (builder,) = make_feature().index_builders()
    assert builder.index_type == "scip"
    section_values({"scip_backend": "local"})
    assert builder.builds_locally is not None and builder.builds_locally() is True
    section_values({"scip_backend": "delegated-plugin"})
    assert builder.builds_locally() is False


def test_local_golden_build_runs_cidx_scip_generate(section_values: Any) -> None:
    """Under the local backend the core runs the command (funnel answers
    NOT_DELEGATED without touching the server)."""
    section_values({"scip_backend": "local"})
    outcome = index_builder.build(
        IndexBuildRequest(alias="repo-global", repo_root="/r")
    )
    assert outcome.status == INDEX_BUILD_RUN_LOCALLY
    assert outcome.command == ("cidx", "scip", "generate")


def test_activated_build_carries_project_and_clear(section_values: Any) -> None:
    section_values({"scip_backend": "local"})
    outcome = index_builder.build(
        IndexBuildRequest(alias="mine", repo_root="/r", clear=True, activated=True)
    )
    assert outcome.command == (
        "cidx",
        "scip",
        "generate",
        "--project",
        "/r",
        "--clear",
    )


def test_index_present_ignores_dependency_databases(tmp_path: Path) -> None:
    assert index_builder.index_present(str(tmp_path)) is False
    scip_dir = tmp_path / ".code-indexer" / "scip"
    (scip_dir / "node_modules" / "dep").mkdir(parents=True)
    (scip_dir / "node_modules" / "dep" / "index.scip.db").write_bytes(b"x")
    assert index_builder.index_present(str(tmp_path)) is False
    (scip_dir / "index.scip.db").write_bytes(b"x")
    assert index_builder.index_present(str(tmp_path)) is True


def _legacy_audit(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE scip_dependency_installations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            job_id VARCHAR(36) NOT NULL,
            repo_alias VARCHAR(255) NOT NULL,
            project_path VARCHAR(255),
            project_language VARCHAR(50),
            project_build_system VARCHAR(50),
            package VARCHAR(255) NOT NULL,
            command TEXT NOT NULL,
            reasoning TEXT,
            username VARCHAR(255)
        )
        """
    )
    conn.execute(
        "INSERT INTO scip_dependency_installations (job_id, repo_alias, package, "
        "command) VALUES ('job-1', 'repo', 'pkg', 'pip install pkg')"
    )
    conn.commit()
    conn.close()


def test_migration_1_imports_the_solo_audit_file(tmp_path: Path) -> None:
    """A solo server's former ``<server dir>/scip_audit.db`` rows land in the
    server database's table, once (the migration runs in one transaction)."""
    server_dir = tmp_path / "server"
    data_dir = server_dir / "data"
    data_dir.mkdir(parents=True)
    _legacy_audit(server_dir / LEGACY_SQLITE_FILENAME)
    (migration,) = SCIP_MIGRATIONS
    conn = sqlite3.connect(str(data_dir / "cidx_server.db"))
    try:
        conn.execute("BEGIN IMMEDIATE")
        migration.apply(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        migration.apply(conn)  # a re-run imports nothing twice
        conn.commit()
        rows = conn.execute(
            "SELECT job_id, repo_alias, package, node_id "
            "FROM scip_dependency_installations"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("job-1", "repo", "pkg", None)]


def test_migration_1_without_a_legacy_file_creates_the_table(tmp_path: Path) -> None:
    (migration,) = SCIP_MIGRATIONS
    conn = sqlite3.connect(str(tmp_path / "cidx_server.db"))
    try:
        migration.apply(conn)
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM scip_dependency_installations"
        ).fetchone()
    finally:
        conn.close()
    assert count == 0
