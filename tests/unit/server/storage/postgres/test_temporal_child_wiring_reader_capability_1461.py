"""Epic #1454 Story #1461 salvage item #3 [MED, latent].

build_temporal_child_env must AND the existing
temporal_sister_relocation_enabled operator toggle with a new
fleet-reader-capability check (all_serving_nodes_reader_capable) before
transporting CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED=1 into the temporal
child's environment. Without this, an operator turning the toggle on
during a rolling deploy could have a just-upgraded node publish
sister-location temporal data (Story #1457 AC6) that an old,
not-yet-upgraded node in the same fleet cannot resolve/read.

These tests mock all_serving_nodes_reader_capable directly (its own
behavior matrix is covered by
tests/unit/server/services/test_temporal_reader_capability_1461.py) to
isolate the wiring/AND-composition logic under test here.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from code_indexer.server.storage.postgres.temporal_child_wiring import (
    build_temporal_child_env,
)
from code_indexer.services.temporal.temporal_relocation_trigger import (
    CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED_ENV,
)
from code_indexer.server.utils.config_manager import ServerConfig

_MODULE = "code_indexer.server.storage.postgres.temporal_child_wiring"


def _config_service_returning(enabled: bool) -> MagicMock:
    mock_service = MagicMock()
    mock_config = MagicMock()
    mock_config.indexing_config.temporal_sister_relocation_enabled = enabled
    mock_service.get_config.return_value = mock_config
    return mock_service


def test_relocation_enabled_and_fleet_capable_sets_env_var():
    server_config = ServerConfig(server_dir="/opt/cidx-server", storage_mode="postgres")

    with (
        patch(
            f"{_MODULE}.get_config_service",
            return_value=_config_service_returning(True),
        ),
        patch(
            f"{_MODULE}.all_serving_nodes_reader_capable", return_value=True
        ) as mock_capable,
    ):
        result = build_temporal_child_env(server_config, base_env={})

    assert result[CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED_ENV] == "1"
    mock_capable.assert_called_once()
    # storage_mode is threaded through so the capability check knows
    # whether it's protecting a real fleet or a trivially-safe solo node.
    assert mock_capable.call_args.args[1] == "postgres" or (
        mock_capable.call_args.kwargs.get("storage_mode") == "postgres"
    )


def test_relocation_enabled_but_fleet_not_capable_omits_env_var():
    server_config = ServerConfig(server_dir="/opt/cidx-server", storage_mode="postgres")

    with (
        patch(
            f"{_MODULE}.get_config_service",
            return_value=_config_service_returning(True),
        ),
        patch(f"{_MODULE}.all_serving_nodes_reader_capable", return_value=False),
    ):
        result = build_temporal_child_env(server_config, base_env={})

    assert CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED_ENV not in result, (
        "a partial-rollout fleet (not all nodes reader-capable) must "
        f"withhold sister-location publication -- got {result}"
    )


def test_relocation_disabled_short_circuits_capability_check_not_called():
    """When the operator toggle itself is off, the capability check is
    pure unreachable overhead -- must never even be invoked."""
    server_config = ServerConfig(server_dir="/opt/cidx-server", storage_mode="postgres")

    with (
        patch(
            f"{_MODULE}.get_config_service",
            return_value=_config_service_returning(False),
        ),
        patch(f"{_MODULE}.all_serving_nodes_reader_capable") as mock_capable,
    ):
        result = build_temporal_child_env(server_config, base_env={})

    assert CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED_ENV not in result
    mock_capable.assert_not_called()


def test_relocation_enabled_solo_storage_mode_sets_env_var():
    """Solo (storage_mode='sqlite') composes with the real
    all_serving_nodes_reader_capable (not mocked here) to prove the two
    modules integrate correctly end-to-end for the trivially-safe case."""
    server_config = ServerConfig(server_dir="/opt/cidx-server", storage_mode="sqlite")

    with patch(
        f"{_MODULE}.get_config_service", return_value=_config_service_returning(True)
    ):
        result = build_temporal_child_env(server_config, base_env={})

    assert result[CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED_ENV] == "1"


def test_relocation_enabled_server_config_none_treated_as_capable_check_still_runs():
    """server_config=None (bootstrap read failed) must not crash the
    capability check -- storage_mode is threaded through as an empty
    string, which all_serving_nodes_reader_capable treats as standalone."""
    with patch(
        f"{_MODULE}.get_config_service", return_value=_config_service_returning(True)
    ):
        result = build_temporal_child_env(None, base_env={})

    assert result[CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED_ENV] == "1"
