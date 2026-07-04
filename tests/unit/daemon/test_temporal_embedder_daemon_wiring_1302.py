"""Unit tests for Bug #1302's `--temporal-embedder` daemon-wiring gap.

`_query_temporal_via_daemon()` (src/code_indexer/cli_daemon_delegation.py) did
not accept or forward a `temporal_embedder` override, so daemon-mode temporal
queries could never honor `--temporal-embedder` even after the shard-blindness
fix -- the daemon always fell back to `config.temporal.active_embedder`. This
mirrors the standalone CLI path (cli.py's `_execute_temporal_fusion(...,
temporal_embedder=temporal_embedder)` call), which already threads it through.
"""

import inspect
from pathlib import Path
from typing import Any, Dict
from unittest.mock import Mock, patch

import pytest


def test_query_temporal_via_daemon_accepts_temporal_embedder():
    """_query_temporal_via_daemon() must accept a temporal_embedder parameter."""
    from code_indexer.cli_daemon_delegation import _query_temporal_via_daemon

    sig = inspect.signature(_query_temporal_via_daemon)
    params = list(sig.parameters.keys())

    assert "temporal_embedder" in params, (
        "temporal_embedder parameter missing from _query_temporal_via_daemon "
        f"signature. Current params: {params}"
    )


def test_query_temporal_via_daemon_forwards_temporal_embedder_to_rpc():
    """_query_temporal_via_daemon() must forward temporal_embedder to the
    exposed_query_temporal RPC call."""
    from code_indexer.cli_daemon_delegation import _query_temporal_via_daemon

    mock_conn = Mock()
    mock_result: Dict[str, Any] = {
        "results": [],
        "query": "test",
        "filter_type": None,
        "filter_value": None,
        "total_found": 0,
        "performance": {},
        "warning": None,
    }
    mock_conn.root.exposed_query_temporal.return_value = mock_result
    mock_conn.close = Mock()

    daemon_config = {"enabled": True, "retry_delays_ms": [100]}

    with (
        patch("code_indexer.cli_daemon_delegation._find_config_file") as mock_find,
        patch("code_indexer.cli_daemon_delegation._get_socket_path") as mock_socket,
        patch("code_indexer.cli_daemon_delegation._connect_to_daemon") as mock_connect,
        patch("code_indexer.utils.temporal_display.display_temporal_results"),
    ):
        mock_find.return_value = Path("/fake/.code-indexer/config.json")
        mock_socket.return_value = Path("/fake/.code-indexer/daemon.sock")
        mock_connect.return_value = mock_conn

        result = _query_temporal_via_daemon(
            query_text="test query",
            time_range="all",
            daemon_config=daemon_config,
            project_root=Path("/fake/project"),
            limit=10,
            temporal_embedder="cohere-embed-v4",
        )

        assert result == 0, "Function should return success"
        assert mock_conn.root.exposed_query_temporal.called, "RPC should be called"

        call_kwargs = mock_conn.root.exposed_query_temporal.call_args.kwargs
        assert "temporal_embedder" in call_kwargs, (
            f"temporal_embedder not passed to exposed_query_temporal. "
            f"Actual kwargs: {call_kwargs}"
        )
        assert call_kwargs["temporal_embedder"] == "cohere-embed-v4"


def test_cli_query_command_threads_temporal_embedder_to_daemon_delegation():
    """The CLI query command's daemon-delegation call site must forward the
    --temporal-embedder value to _query_temporal_via_daemon (previously
    silently dropped, meaning the flag had no effect in daemon mode)."""
    import code_indexer.cli as cli_module

    # cli_module.query is a click Command -- the actual function lives on
    # .callback.
    source = inspect.getsource(cli_module.query.callback)
    # Locate the call to _query_temporal_via_daemon inside the query command
    # and assert it forwards temporal_embedder.
    assert "cli_daemon_delegation._query_temporal_via_daemon(" in source
    call_start = source.index("cli_daemon_delegation._query_temporal_via_daemon(")
    call_end = source.index(")", call_start)
    call_block = source[call_start:call_end]
    assert "temporal_embedder=temporal_embedder" in call_block, (
        "cli.py's query command does not forward temporal_embedder to "
        "_query_temporal_via_daemon -- --temporal-embedder has no effect in "
        f"daemon mode. Call block:\n{call_block}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
