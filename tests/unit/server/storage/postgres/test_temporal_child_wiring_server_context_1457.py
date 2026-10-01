"""Server-context child-env marker for temporal children (Story #1457 AC6
Finding-1 / round-23 correction).

RefreshScheduler (and the other four temporal-child spawn sites) must set an
explicit env flag when spawning a temporal `cidx index --index-commits`
child so the child can distinguish "I was spawned by the server" (any
storage mode, including a solo/SQLite local server) from "I am a genuine
standalone CLI invocation with no server process at all".

The PREVIOUS build_temporal_child_env only fired (returned non-None) in
postgres mode -- the CIDX_TEMPORAL_PG_BOOTSTRAP_DIR pattern -- which
silently dropped the server-context signal in solo/SQLite mode (that IS the
bug this round-23 correction fixes: a solo/SQLite server is genuinely
server-context, not standalone-CLI). The restructured builder is modeled on
build_embedding_stats_child_env (explicitly unconditional on storage_mode):
it ALWAYS returns a dict (never None), sets CIDX_SERVER_REFRESH_CONTEXT=1 in
ALL storage modes, and continues to set CIDX_TEMPORAL_PG_BOOTSTRAP_DIR ONLY
in postgres mode (unchanged from before).
"""

from __future__ import annotations

from code_indexer.server.utils.config_manager import ServerConfig
from code_indexer.server.storage.postgres.temporal_child_wiring import (
    build_temporal_child_env,
)
from code_indexer.storage.temporal_metadata_backend_registry import (
    TEMPORAL_PG_BOOTSTRAP_DIR_ENV,
)

_TEST_DSN = "postgresql://test-host-not-a-real-secret/testdb"


def test_postgres_mode_returns_dict_with_server_refresh_context_flag(tmp_path):
    from code_indexer.server.storage.postgres.temporal_child_wiring import (
        CIDX_SERVER_REFRESH_CONTEXT_ENV,
    )

    server_dir = str(tmp_path / "cidx-server")
    server_config = ServerConfig(
        server_dir=server_dir,
        storage_mode="postgres",
        postgres_dsn=_TEST_DSN,
    )

    result = build_temporal_child_env(server_config, base_env={})

    assert isinstance(result, dict)
    assert result[CIDX_SERVER_REFRESH_CONTEXT_ENV] == "1"
    assert result[TEMPORAL_PG_BOOTSTRAP_DIR_ENV] == server_dir


def test_sqlite_mode_returns_dict_with_server_refresh_context_flag_and_no_pg_dir(
    tmp_path,
):
    """The SOLO case: this is exactly the scenario the round-23 fix targets
    -- a solo/SQLite server IS server-context and must still get the flag,
    but must NOT get the postgres-only bootstrap-dir var."""
    from code_indexer.server.storage.postgres.temporal_child_wiring import (
        CIDX_SERVER_REFRESH_CONTEXT_ENV,
    )

    server_dir = str(tmp_path / "cidx-server")
    server_config = ServerConfig(server_dir=server_dir, storage_mode="sqlite")

    result = build_temporal_child_env(server_config, base_env={})

    assert isinstance(result, dict)
    assert result[CIDX_SERVER_REFRESH_CONTEXT_ENV] == "1"
    assert TEMPORAL_PG_BOOTSTRAP_DIR_ENV not in result
