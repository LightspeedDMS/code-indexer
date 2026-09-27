"""Bug #1979 (P1, round 4): a cancelled daemon `--clear` must not exit 0.

`_index_via_daemon` (cli_daemon_delegation.py) treated any daemon result
with `status == "completed"` as exit 0, even when `stats["cancelled"]` was
True. Combined with the shared rebuild check accepting a fresh-but-not-
`completed` progress metadata file, a `--clear` cancelled partway through
could report success with a partial index -- the exact case the maintainer's
"clear=true never yields a blank/partial index as normal operation" rule
forbids.

`clear=true` (force_reindex=True here) is the only case where cancellation
must be loud: an interrupted incremental run just processes fewer files,
which is not the "blank index" hazard clear=true guards against.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

from code_indexer.cli_daemon_delegation import _index_via_daemon


def _patched_index_via_daemon(*, force_reindex: bool, cancelled: bool) -> int:
    daemon_config = {"enabled": True, "retry_delays_ms": [100, 500, 1000, 2000]}

    with (
        patch("code_indexer.cli_daemon_delegation._find_config_file") as mock_find,
        patch("code_indexer.cli_daemon_delegation._get_socket_path") as mock_socket,
        patch("code_indexer.cli_daemon_delegation._connect_to_daemon") as mock_connect,
        patch("code_indexer.progress.progress_display.RichLiveProgressManager"),
        patch("code_indexer.progress.MultiThreadedProgressManager"),
        patch("rich.console.Console.print"),
    ):
        mock_find.return_value = Path("/project/.code-indexer/config.json")
        mock_socket.return_value = Path("/tmp/cidx/test.sock")

        mock_conn = Mock()
        mock_conn.root.exposed_index_blocking.return_value = {
            "status": "completed",
            "stats": {
                "files_processed": 3,
                "chunks_created": 1,
                "failed_files": 0,
                "duration_seconds": 0.5,
                "cancelled": cancelled,
            },
        }
        mock_connect.return_value = mock_conn

        return _index_via_daemon(
            force_reindex=force_reindex, daemon_config=daemon_config
        )


def test_cancelled_daemon_clear_exits_nonzero():
    result = _patched_index_via_daemon(force_reindex=True, cancelled=True)
    assert result != 0, "a cancelled --clear must not report success"


def test_cancelled_daemon_incremental_run_still_exits_zero():
    """Only clear=true is the blank-index hazard; a cancelled incremental
    run keeps its existing exit-0 (resumable) behavior -- scope is
    deliberately limited to force_reindex=True (clear)."""
    result = _patched_index_via_daemon(force_reindex=False, cancelled=True)
    assert result == 0


def test_uncancelled_daemon_clear_still_exits_zero():
    result = _patched_index_via_daemon(force_reindex=True, cancelled=False)
    assert result == 0
