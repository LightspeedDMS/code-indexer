"""The cidx-xray Feature object, as the SDK contract sees it."""

from __future__ import annotations

import re
import stat
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, cast

import click
import pytest

from cidx_sdk import Feature, sdk_satisfies
from cidx_sdk.capabilities import CONFIG
from cidx_sdk.feature import DEGRADED, HEALTHY
from cidx_xray import __version__
from cidx_xray.cli import xray_group
from cidx_xray.feature import (
    CAPABILITIES_FOR_HANDLERS,
    XRAY_CONFIG_SECTION,
    XrayFeature,
    make_feature,
    tool_names,
)
from cidx_xray.server import runtime

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


class _Section:
    """The ``config`` capability, reduced to the feature's section."""

    def __init__(self, xray_backend: str) -> None:
        self._values = {"xray_backend": xray_backend}

    def section(self) -> Dict[str, object]:
        return dict(self._values)


class _Context:
    """A started feature's context holding only ``config``."""

    def __init__(self, xray_backend: str) -> None:
        self._config = _Section(xray_backend)

    def has_capability(self, name: str) -> bool:
        return name == CONFIG

    def capability(self, name: str) -> object:
        return self._config


@pytest.fixture
def runtime_config_host() -> Iterator[None]:
    """Leave no feature context behind."""
    yield
    runtime.CONTEXT.uninstall()


def _set_backend(xray_backend: Optional[str]) -> None:
    """``xray_backend`` as the started feature's config section reads it;
    None: the feature has not started (the section's default, local)."""
    if xray_backend is None:
        runtime.CONTEXT.uninstall()
    else:
        runtime.CONTEXT.install(cast(Any, _Context(xray_backend)))


def _executable(tmp_path: Path) -> Path:
    binary = tmp_path / "xray-cli"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


def test_factory_returns_a_feature_named_xray() -> None:
    feature = make_feature()
    assert isinstance(feature, Feature)
    assert isinstance(feature, XrayFeature)
    assert feature.manifest.name == "xray"
    assert feature.manifest.version == __version__
    assert sdk_satisfies(feature.manifest.requires_sdk)


def test_requires_the_capabilities_its_handlers_take() -> None:
    assert make_feature().manifest.required_capabilities == CAPABILITIES_FOR_HANDLERS


def test_entry_point_is_declared_under_the_feature_name() -> None:
    text = _PYPROJECT.read_text(encoding="utf-8")
    group = re.search(
        r'^\[project\.entry-points\."code_indexer\.features"\]\n(.*?)(?:\n\[|\Z)',
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert group is not None, "no code_indexer.features entry-point group"
    assert 'xray = "cidx_xray.feature:make_feature"' in group.group(1)


def test_cli_commands_contribute_the_xray_group() -> None:
    (contribution,) = make_feature().cli_commands()
    assert contribution.name == "xray"
    assert contribution.command is xray_group
    assert isinstance(contribution.command, click.Group)
    assert {"search", "explore"} <= set(xray_group.commands)


def test_config_section_splits_bootstrap_and_runtime_keys() -> None:
    section = make_feature().config_section()
    assert section is XRAY_CONFIG_SECTION
    assert section.name == "xray"
    assert section.bootstrap_keys == frozenset({"xray_backend"})
    assert section.runtime_keys == frozenset(
        {"xray_timeout_seconds", "xray_worker_threads"}
    )


def test_config_section_bounds_the_runtime_keys_for_the_config_screen() -> None:
    by_name = {key.name: key for key in XRAY_CONFIG_SECTION.keys}
    timeout = by_name["xray_timeout_seconds"]
    workers = by_name["xray_worker_threads"]
    assert (timeout.default, timeout.minimum, timeout.maximum) == (120, 10, 600)
    assert (workers.default, workers.minimum, workers.maximum) == (4, 1, 8)
    assert XRAY_CONFIG_SECTION.title == "X-Ray Search"
    assert all(key.label for key in XRAY_CONFIG_SECTION.keys)
    assert not any(key.restart_required for key in XRAY_CONFIG_SECTION.keys)


def test_health_is_healthy_when_the_binary_is_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime_config_host: None
) -> None:
    monkeypatch.setenv("CIDX_XRAY_CLI_PATH", str(_executable(tmp_path)))
    _set_backend(None)

    (report,) = make_feature().health()

    assert report.status == HEALTHY
    assert report.detail == {"xray_backend": "local", "xray_cli_found": True}


def test_health_is_degraded_without_the_binary_under_local_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime_config_host: None
) -> None:
    missing = tmp_path / "absent" / "xray-cli"
    monkeypatch.setenv("CIDX_XRAY_CLI_PATH", str(missing))
    _set_backend("local")

    (report,) = make_feature().health()

    assert report.status == DEGRADED
    assert report.detail["xray_cli_found"] is False
    assert report.reasons
    assert str(missing) not in " ".join(report.reasons), "no paths in health"


def test_health_does_not_need_the_binary_under_delegated_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime_config_host: None
) -> None:
    monkeypatch.setenv("CIDX_XRAY_CLI_PATH", str(tmp_path / "absent"))
    _set_backend("delegated")

    (report,) = make_feature().health()

    assert report.status == HEALTHY
    assert report.detail == {"xray_backend": "delegated", "xray_cli_found": False}


def test_contributes_tools_routes_and_migrations() -> None:
    """Phase 4c: every server piece of X-Ray is a contribution."""
    feature = make_feature()
    assert sorted(tool.name for tool in feature.tools()) == list(tool_names())
    assert len(tool_names()) == 6
    for tool in feature.tools():
        assert tool.doc_markdown.startswith("---\nname: " + tool.name)
    (router,) = feature.routers()
    assert router.prefix == "/api/xray"
    assert [m.version for m in feature.migrations()] == [1]
    assert feature.ui_pages() == ()
    assert feature.background_jobs() == ()
