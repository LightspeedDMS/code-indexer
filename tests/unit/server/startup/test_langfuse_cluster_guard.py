"""Gate Langfuse sync behind leader election in cluster mode.

Tests verify that lifespan.py:
  - In cluster mode (postgres), defers Langfuse sync start to _on_become_leader.
  - In solo mode (sqlite), starts LangfuseTraceSyncService immediately.
  - _on_become_leader starts Langfuse sync.
  - _on_lose_leadership stops Langfuse sync.

Strategy: source-text inspection of lifespan.py (same pattern as existing
lifespan wiring tests, e.g. test_dep_map_927_lifespan_wiring.py).
"""

from __future__ import annotations

from pathlib import Path

# Depth: this file -> startup -> server -> unit -> tests -> repo root
_DEPTH_TO_REPO_ROOT = 4

_SOURCE = (
    Path(__file__).resolve().parents[_DEPTH_TO_REPO_ROOT]
    / "src"
    / "code_indexer"
    / "server"
    / "startup"
    / "lifespan.py"
).read_text()


def _langfuse_startup_block() -> str:
    """Return the source block starting at the pull_enabled check through next except."""
    marker = "if config.langfuse_config and config.langfuse_config.pull_enabled:"
    pos = _SOURCE.find(marker)
    assert pos != -1, f"Could not find {marker!r} in lifespan.py"
    next_except = _SOURCE.find("\n        except Exception", pos)
    fallback_window_chars = 500
    end = (
        next_except if next_except != -1 else pos + len(marker) + fallback_window_chars
    )
    return _SOURCE[pos:end]


def _postgres_branch_slice(block: str) -> str:
    """Return slice from postgres guard line (inclusive) through else: (exclusive)."""
    guard = 'if storage_mode == "postgres":'
    guard_pos = block.find(guard)
    assert guard_pos != -1, f"{guard!r} not found in block"
    else_pos = block.find("else:", guard_pos)
    assert else_pos != -1, "'else:' not found after postgres guard"
    return block[guard_pos:else_pos]


def _on_become_leader_body() -> str:
    """Return source from def _on_become_leader() through def _on_lose_leadership()."""
    start = _SOURCE.find("def _on_become_leader():")
    assert start != -1, "_on_become_leader not found in lifespan.py"
    end = _SOURCE.find("def _on_lose_leadership():", start)
    assert end != -1, "_on_lose_leadership not found after _on_become_leader"
    return _SOURCE[start:end]


def _on_lose_leadership_body() -> str:
    """Return source from def _on_lose_leadership() through the leader assignment."""
    start = _SOURCE.find("def _on_lose_leadership():")
    assert start != -1, "_on_lose_leadership not found in lifespan.py"
    end_marker = "_leader_election._on_become_leader = _on_become_leader"
    end = _SOURCE.find(end_marker, start)
    assert end != -1, f"{end_marker!r} not found after _on_lose_leadership"
    return _SOURCE[start:end]


def test_cluster_mode_defers_langfuse_sync():
    """Postgres branch must log a deferral message and must NOT call .start() immediately."""
    pg_slice = _postgres_branch_slice(_langfuse_startup_block())

    assert "deferred" in pg_slice.lower(), (
        "Postgres branch must log a 'deferred' message. Slice:\n" + pg_slice
    )
    assert "langfuse_sync_service.start()" not in pg_slice, (
        "Postgres branch must NOT call .start() — deferred to _on_become_leader. "
        "Slice:\n" + pg_slice
    )


def test_solo_mode_starts_langfuse_sync_immediately():
    """Solo mode else: branch must call langfuse_sync_service.start() directly."""
    block = _langfuse_startup_block()

    guard_pos = block.find('storage_mode == "postgres"')
    assert guard_pos != -1, (
        "storage_mode == 'postgres' guard not found. Block:\n" + block
    )
    else_pos = block.find("else:", guard_pos)
    assert else_pos != -1, (
        "else: branch not found after storage_mode guard. Block:\n" + block
    )
    start_call_pos = block.find("langfuse_sync_service.start()", else_pos)
    assert start_call_pos != -1, (
        "langfuse_sync_service.start() not found in solo-mode else: branch. "
        "After else:\n" + block[else_pos:]
    )


def test_on_become_leader_starts_langfuse_sync():
    """_on_become_leader closure must call langfuse_sync_service.start()."""
    body = _on_become_leader_body()

    assert "langfuse_sync_service.start()" in body, (
        "_on_become_leader must call langfuse_sync_service.start(). Body:\n" + body
    )


def test_on_lose_leadership_stops_langfuse_sync():
    """_on_lose_leadership closure must call langfuse_sync_service.stop()."""
    body = _on_lose_leadership_body()

    assert "langfuse_sync_service.stop()" in body, (
        "_on_lose_leadership must call langfuse_sync_service.stop(). Body:\n" + body
    )
