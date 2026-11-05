"""Config key and SDK seam-group constants agree with the core."""

from __future__ import annotations

from importlib import import_module

import pytest
from cidx_sdk import SEAM_ENTRY_POINT_GROUPS

from code_indexer.server.services.config_service import BOOTSTRAP_KEYS
from code_indexer.server.utils.config_manager import ServerConfig

# seam name in the SDK -> the module that declares ENTRY_POINT_GROUP (the X-Ray
# seam's resolver moved into the cidx-xray feature in phase 4c)
_CORE_SEAM_MODULES = {
    "content": "code_indexer.server.services.content_backend_plugin",
    "content_writer": "code_indexer.server.services.content_writer_plugin",
    "git": "code_indexer.server.services.git_backend_plugin",
    "git_invoker": "code_indexer.utils.git_invoker",
    "ripgrep": "code_indexer.server.services.ripgrep_backend_plugin",
    "search": "code_indexer.server.services.search_backend_plugin",
    "index_artifacts": "code_indexer.server.services.index_artifacts_plugin",
    "scip": "cidx_scip.server.backend_plugin",
    "xray": "cidx_xray.server.backend_plugin",
    "clone": "code_indexer.server.services.clone_backend_plugin",
    "cli_invoker": "code_indexer.server.services.cli_invoker_plugin",
}


def test_every_sdk_seam_is_mapped_to_a_core_resolver() -> None:
    assert set(_CORE_SEAM_MODULES) == set(SEAM_ENTRY_POINT_GROUPS)


@pytest.mark.parametrize("seam, module", sorted(_CORE_SEAM_MODULES.items()))
def test_sdk_seam_group_matches_core(seam: str, module: str) -> None:
    assert import_module(module).ENTRY_POINT_GROUP == SEAM_ENTRY_POINT_GROUPS[seam]


def test_enabled_features_is_a_bootstrap_key_defaulting_to_none() -> None:
    assert "enabled_features" in BOOTSTRAP_KEYS
    assert ServerConfig(server_dir="/srv/example").enabled_features == []
