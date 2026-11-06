"""Every feature distribution in this repository, loaded by the core's loader.

The features are the ``code_indexer.features`` entry points the in-tree
``pyproject.toml`` files declare (cidx_testkit's registry reads them), so a
feature added to the repository is included without editing this module.
Tests that inventory what a server with every feature enabled serves (the
audit catalog's route and MCP door inventories) load them through here.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, List

from cidx_sdk import KNOWN_CAPABILITIES
from cidx_sdk.entry_points import FEATURES_ENTRY_POINT_GROUP
from cidx_testkit.registry import declared_entry_points

from code_indexer.server.features.loader import FeatureCandidate, load_features
from code_indexer.server.features.tools import install_feature_tools

REPO_ROOT = Path(__file__).resolve().parents[3]


def loaded_in_tree_features(repo_root: Path = REPO_ROOT) -> List[Any]:
    """Every in-tree feature, loaded and validated, in name order."""
    declared = declared_entry_points(repo_root).get(FEATURES_ENTRY_POINT_GROUP, [])
    candidates = {
        entry.name: FeatureCandidate(
            name=entry.name, load=entry.load, source=entry.value
        )
        for entry in declared
    }
    return list(
        load_features(
            sorted(candidates),
            candidates=candidates,
            provided_capabilities=KNOWN_CAPABILITIES,
        )
    )


@contextmanager
def in_tree_feature_tools_installed() -> Iterator[None]:
    """Every in-tree feature's MCP tools in TOOL_REGISTRY / HANDLER_REGISTRY
    for the block, as an app with every feature enabled has them."""
    install_feature_tools(loaded_in_tree_features())
    try:
        yield
    finally:
        install_feature_tools([])
