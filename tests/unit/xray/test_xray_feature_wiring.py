"""The core's side of the cidx-xray feature after the handoff (phase 4c).

X-Ray lives in its own distribution and the core consumes every hook it
contributes; the core wires nothing of X-Ray itself. These tests pin the two
halves together: the handoff switch is off, the loader accepts the feature
with every contribution, the CLI finds ``cidx xray`` by entry point, and the
settings the core used to own move into the feature's section on load.
"""

from __future__ import annotations

import importlib
from typing import Any, Dict

import click

from code_indexer.kernel.feature_handoff import CORE_WIRED_FEATURES, core_wires

XRAY_TOOLS = [
    "analyze_graph",
    "store_xray_pattern",
    "xray_dump_ast",
    "xray_explore",
    "xray_search",
    "xray_search_batch",
]


def _loaded_xray() -> Any:
    from cidx_sdk import KNOWN_CAPABILITIES

    from code_indexer.server.features.loader import FeatureCandidate, load_features
    from cidx_xray.feature import make_feature

    (loaded,) = load_features(
        ["xray"],
        candidates={
            "xray": FeatureCandidate(
                name="xray",
                load=lambda: make_feature,
                source="cidx_xray.feature:make_feature",
            )
        },
        provided_capabilities=KNOWN_CAPABILITIES,
    )
    return loaded


def test_the_core_wires_nothing_of_xray() -> None:
    assert not core_wires("xray")
    assert "xray" not in CORE_WIRED_FEATURES


def test_the_core_imports_no_xray_server_module() -> None:
    """The handlers, routes and services that moved have no module left at
    their old core paths."""
    for old in (
        "code_indexer.xray",
        "code_indexer.cli_xray",
        "code_indexer.server.routes.xray_routes",
        "code_indexer.server.services.xray_pattern_service",
        "code_indexer.server.startup.xray_backend_wiring",
        "code_indexer.server.mcp.handlers.xray_batch",
    ):
        assert importlib.util.find_spec(old) is None, old


def test_the_core_loader_accepts_every_contribution() -> None:
    loaded = _loaded_xray()
    contributions = loaded.contributions
    assert loaded.manifest.name == "xray"
    assert sorted(t.name for t in contributions.tools) == XRAY_TOOLS
    assert [r.prefix for r in contributions.routers] == ["/api/xray"]
    assert [c.name for c in contributions.cli_commands] == ["xray"]
    assert contributions.config_section is not None
    assert [m.version for m in contributions.migrations] == [1]


def test_every_tool_doc_parses_with_the_cores_parser() -> None:
    from code_indexer.server.features.tools import _prepare

    for tool in _loaded_xray().contributions.tools:
        doc, definition = _prepare("xray", tool, set())
        assert doc.name == tool.name
        assert definition["name"] == tool.name


def test_cidx_xray_is_found_by_entry_point() -> None:
    from code_indexer import cli_features
    from code_indexer.cli import cli
    from cidx_xray.cli import xray_group

    cli_features.reset_feature_commands()
    try:
        assert "xray" not in cli.commands  # never registered by the core
        command = cli.get_command(click.Context(cli), "xray")
        assert command is xray_group
    finally:
        cli_features.reset_feature_commands()


def test_old_core_settings_move_into_the_feature_section() -> None:
    from code_indexer.server.utils.config_manager import ServerConfigManager

    stored: Dict[str, Any] = {
        "server_dir": "/tmp/unused",
        "xray_config": {"xray_timeout_seconds": 300, "xray_worker_threads": 6},
        "xray_backend": "delegated",
    }
    config = ServerConfigManager.__new__(ServerConfigManager)._dict_to_server_config(
        dict(stored)
    )
    assert config.feature_settings["xray"] == {
        "xray_timeout_seconds": 300,
        "xray_worker_threads": 6,
    }
    assert config.feature_bootstrap_settings["xray"] == {"xray_backend": "delegated"}
    assert not hasattr(config, "xray_config")
    assert not hasattr(config, "xray_backend")


def test_a_value_already_in_the_section_wins_over_the_old_key() -> None:
    from code_indexer.server.utils.config_manager import ServerConfigManager

    config = ServerConfigManager.__new__(ServerConfigManager)._dict_to_server_config(
        {
            "server_dir": "/tmp/unused",
            "xray_config": {"xray_timeout_seconds": 300},
            "feature_settings": {"xray": {"xray_timeout_seconds": 60}},
        }
    )
    assert config.feature_settings["xray"] == {"xray_timeout_seconds": 60}


def test_feature_section_defaults_match_the_old_core_defaults() -> None:
    """120 s timeout, 4 worker threads, local execution: what the core's own
    XRayConfig and ServerConfig.xray_backend defaulted to."""
    from cidx_xray.config import default_values

    assert default_values() == {
        "xray_timeout_seconds": 120,
        "xray_worker_threads": 4,
        "xray_backend": "local",
    }
