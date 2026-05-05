"""
Tests for anyio threadpool sizing at server startup.

Coverage:
  1. ServerConfig.server_threadpool_size defaults to 256.
  2. lifespan.py imports anyio.to_thread (structural).
  3. lifespan.py startup block calls current_default_thread_limiter().total_tokens (structural).
  4. lifespan.py startup block respects the zero/negative guard (structural).
  5. anyio CapacityLimiter API behaves correctly (behavioural, runs in real event loop).
"""

from __future__ import annotations

import sys
import asyncio
from pathlib import Path

import anyio.to_thread
import pytest


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)
_CONFIG_MANAGER_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "utils" / "config_manager.py"
)


def _lifespan_source() -> str:
    return _LIFESPAN_PATH.read_text()


def _split_at_yield(source: str):
    """Split lifespan source into startup and shutdown halves at the bare yield."""
    marker = "\n        yield"
    idx = source.find(marker)
    assert idx != -1, "Could not find 'yield' marker in lifespan.py"
    return source[:idx], source[idx + len(marker) :]


# ---------------------------------------------------------------------------
# Fixture: ensure src/ is on sys.path for direct ServerConfig imports
# ---------------------------------------------------------------------------


@pytest.fixture()
def with_src_on_path():
    """Add repo src/ to sys.path for the duration of the test, then remove it."""
    src = str(_REPO_ROOT / "src")
    sys.path.insert(0, src)
    yield
    sys.path.remove(src)


# ---------------------------------------------------------------------------
# 1. ServerConfig default field
# ---------------------------------------------------------------------------


class TestServerConfigThreadpoolField:
    def test_default_is_256(self, with_src_on_path):
        """ServerConfig.server_threadpool_size must default to 256."""
        from code_indexer.server.utils.config_manager import ServerConfig

        cfg = ServerConfig(server_dir="/tmp/test")
        assert cfg.server_threadpool_size == 256, (
            f"Expected server_threadpool_size=256, got {cfg.server_threadpool_size}"
        )

    def test_field_declared_in_source(self):
        """ServerConfig source must declare server_threadpool_size field."""
        source = _CONFIG_MANAGER_PATH.read_text()
        assert "server_threadpool_size: int = 256" in source, (
            "ServerConfig must declare 'server_threadpool_size: int = 256'"
        )

    def test_custom_value_accepted(self, with_src_on_path):
        """ServerConfig accepts a custom server_threadpool_size value."""
        from code_indexer.server.utils.config_manager import ServerConfig

        cfg = ServerConfig(server_dir="/tmp/test", server_threadpool_size=512)
        assert cfg.server_threadpool_size == 512

    def test_zero_accepted(self, with_src_on_path):
        """ServerConfig accepts server_threadpool_size=0 (means leave anyio default)."""
        from code_indexer.server.utils.config_manager import ServerConfig

        cfg = ServerConfig(server_dir="/tmp/test", server_threadpool_size=0)
        assert cfg.server_threadpool_size == 0


# ---------------------------------------------------------------------------
# 2. Structural: lifespan.py imports anyio.to_thread
# ---------------------------------------------------------------------------


class TestLifespanImportsAnyio:
    def test_lifespan_imports_anyio_to_thread(self):
        """lifespan.py must import anyio.to_thread at module level."""
        source = _lifespan_source()
        assert "import anyio.to_thread" in source, (
            "lifespan.py must have 'import anyio.to_thread' at module level"
        )

    def test_lifespan_calls_current_default_thread_limiter(self):
        """lifespan.py startup block must call current_default_thread_limiter()."""
        source = _lifespan_source()
        startup, _ = _split_at_yield(source)
        assert "current_default_thread_limiter()" in startup, (
            "lifespan.py startup region must call anyio.to_thread.current_default_thread_limiter()"
        )

    def test_lifespan_sets_total_tokens(self):
        """lifespan.py startup block must assign .total_tokens on the limiter."""
        source = _lifespan_source()
        startup, _ = _split_at_yield(source)
        assert ".total_tokens = " in startup, (
            "lifespan.py startup region must assign .total_tokens on the thread limiter"
        )


# ---------------------------------------------------------------------------
# 3. Structural: guard against zero / negative
# ---------------------------------------------------------------------------


class TestLifespanThreadpoolGuard:
    def test_startup_has_positive_guard(self):
        """lifespan.py startup block must guard with 'if _threadpool_size > 0:'."""
        source = _lifespan_source()
        startup, _ = _split_at_yield(source)
        assert "if _threadpool_size > 0:" in startup, (
            "lifespan.py must skip limiter assignment when server_threadpool_size <= 0"
        )

    def test_startup_reads_threadpool_size_from_startup_config(self):
        """lifespan.py startup block must read server_threadpool_size from startup_config."""
        source = _lifespan_source()
        startup, _ = _split_at_yield(source)
        assert "server_threadpool_size" in startup, (
            "lifespan.py startup region must reference 'server_threadpool_size'"
        )


# ---------------------------------------------------------------------------
# 4. Behavioural: anyio limiter API responds to total_tokens assignment.
#    Runs inside a real asyncio event loop so current_default_thread_limiter()
#    resolves correctly.
# ---------------------------------------------------------------------------


class TestAnyioLimiterBehaviour:
    def test_total_tokens_assignment_takes_effect(self):
        """Confirm anyio CapacityLimiter.total_tokens setter works as expected."""

        async def _inner():
            limiter = anyio.to_thread.current_default_thread_limiter()
            original = limiter.total_tokens
            limiter.total_tokens = 512
            assert limiter.total_tokens == 512, (
                f"Expected 512 after assignment, got {limiter.total_tokens}"
            )
            limiter.total_tokens = original  # restore

        asyncio.run(_inner())

    def test_total_tokens_zero_guard_leaves_default_unchanged(self):
        """When size <= 0 the default limiter total_tokens is not modified."""

        async def _inner():
            limiter = anyio.to_thread.current_default_thread_limiter()
            before = limiter.total_tokens
            threadpool_size = 0
            if threadpool_size > 0:
                limiter.total_tokens = threadpool_size
            assert limiter.total_tokens == before, (
                "Limiter must not be modified when server_threadpool_size=0"
            )

        asyncio.run(_inner())
