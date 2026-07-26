"""Story #1457 AC1 (2026-07-24 re-review, Codex finding #4):
build_temporal_child_env must read the sister-relocation safety gate's
on/off decision from the config service, not require an operator to set a
raw OS env var manually. The env var (CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED)
still transports this parent-resolved value into the temporal child
subprocess -- same pattern as CIDX_SERVER_REFRESH_CONTEXT -- but the
config service is now the AUTHORITATIVE source, per CLAUDE.md's "No
Environment Variables for Server Settings" rule.

Mirrors test_temporal_child_wiring_1313.py's TestBuildTemporalChildEnv
test harness.
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


def _config_service_returning(enabled: bool) -> MagicMock:
    mock_service = MagicMock()
    mock_config = MagicMock()
    mock_config.indexing_config.temporal_sister_relocation_enabled = enabled
    mock_service.get_config.return_value = mock_config
    return mock_service


def test_config_service_enabled_sets_env_var():
    server_config = ServerConfig(server_dir="/opt/cidx-server", storage_mode="sqlite")

    with patch(
        "code_indexer.server.storage.postgres.temporal_child_wiring.get_config_service",
        return_value=_config_service_returning(True),
    ):
        result = build_temporal_child_env(server_config, base_env={})

    assert result[CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED_ENV] == "1", (
        "build_temporal_child_env must set the transport env var when the "
        f"config service reports the gate enabled -- got {result}"
    )


def test_config_service_disabled_omits_env_var():
    server_config = ServerConfig(server_dir="/opt/cidx-server", storage_mode="sqlite")

    with patch(
        "code_indexer.server.storage.postgres.temporal_child_wiring.get_config_service",
        return_value=_config_service_returning(False),
    ):
        result = build_temporal_child_env(server_config, base_env={})

    assert CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED_ENV not in result, (
        "build_temporal_child_env must NOT set the transport env var when "
        f"the config service reports the gate disabled (default) -- got {result}"
    )


def test_config_service_unavailable_omits_env_var_non_fatal():
    """A config-service read failure (e.g. DB unavailable) must never
    crash child-env construction -- fail-safe to the disabled default,
    matching this safety gate's own default-OFF philosophy."""
    server_config = ServerConfig(server_dir="/opt/cidx-server", storage_mode="sqlite")

    with patch(
        "code_indexer.server.storage.postgres.temporal_child_wiring.get_config_service",
        side_effect=RuntimeError("db unavailable"),
    ):
        result = build_temporal_child_env(server_config, base_env={})

    assert isinstance(result, dict)
    assert CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED_ENV not in result


def test_disabled_config_clears_stale_env_var():
    """2026-07-24 round-4 re-review (Codex): a pre-existing "1" inherited
    in base_env/os.environ (e.g. left over from a prior enabled run, or an
    operator's ambient shell) must NOT silently override a config service
    that now reports the gate disabled. The merged env is built from
    base_env, so a stale key surviving the merge would let inherited env
    state act as a fallback authority -- exactly what this gate must never
    allow."""
    server_config = ServerConfig(server_dir="/opt/cidx-server", storage_mode="sqlite")
    stale_base_env = {CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED_ENV: "1"}

    with patch(
        "code_indexer.server.storage.postgres.temporal_child_wiring.get_config_service",
        return_value=_config_service_returning(False),
    ):
        result = build_temporal_child_env(server_config, base_env=stale_base_env)

    assert CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED_ENV not in result, (
        "a stale inherited env value must be cleared when config says "
        f"disabled -- got {result}"
    )


def test_config_read_error_clears_stale_env_var():
    """2026-07-24 round-4 re-review (Codex): a config-read exception must
    resolve to the disabled default even when base_env already has a
    stale "1" set -- an exception must never leave relocation ENABLED via
    inherited env state, which is the opposite of the claimed fail-safe
    direction."""
    server_config = ServerConfig(server_dir="/opt/cidx-server", storage_mode="sqlite")
    stale_base_env = {CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED_ENV: "1"}

    with patch(
        "code_indexer.server.storage.postgres.temporal_child_wiring.get_config_service",
        side_effect=RuntimeError("db unavailable"),
    ):
        result = build_temporal_child_env(server_config, base_env=stale_base_env)

    assert CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED_ENV not in result, (
        "a stale inherited env value must be cleared even when the config "
        f"read raises -- got {result}"
    )
