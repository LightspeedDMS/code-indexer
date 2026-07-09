"""Unit tests for Story #1213 Story 3: omni multi-repo temporal path governor wiring.

Verifies that _search_temporal_sync injects memory_governor=get_memory_governor()
into FilesystemVectorStore construction, mirroring the single-repo path in
search_service.py and semantic_query_manager.py.

Uses inspect.getsource() — same approach as test_temporal_cache_injection_1170.py.
"""

import inspect
import sys
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Stub missing optional dependencies (same list as test_temporal_cache_injection_1170.py).
# ---------------------------------------------------------------------------
_STUB_MODULES = [
    "google",
    "google.protobuf",
    "google.protobuf.descriptor",
    "google.protobuf.descriptor_pb2",
    "google.protobuf.descriptor_pool",
    "google.protobuf.internal",
    "google.protobuf.internal.builder",
    "google.protobuf.message",
    "google.protobuf.reflection",
    "google.protobuf.symbol_database",
    "google.protobuf.runtime_version",
    "rich",
    "rich.console",
    "rich.markup",
    "rich.table",
    "rich.panel",
    "rich.progress",
    "rich.text",
    "rich.syntax",
    "rich.traceback",
    "rich.logging",
    "pathspec",
    "code_indexer.scip.protobuf.scip_pb2",
    "code_indexer.scip.protobuf",
    "numpy",
    "msgpack",
]
for _mod in _STUB_MODULES:
    if _mod not in sys.modules:
        try:
            __import__(_mod)
        except ImportError:
            sys.modules[_mod] = MagicMock()


class TestOmniGovernorWiring:
    """Story #1213 Story 3: memory_governor injected into omni temporal FilesystemVectorStore."""

    def test_search_temporal_sync_passes_memory_governor_to_vector_store(self) -> None:
        """
        _search_temporal_sync must construct FilesystemVectorStore with
        memory_governor=get_memory_governor().

        This ensures the omni temporal path consults the MemoryGovernor on every
        per-shard eviction decision, just like the single-repo path in search_service.py.
        """
        from code_indexer.server.multi.multi_search_service import MultiSearchService

        source = inspect.getsource(MultiSearchService._search_temporal_sync)

        assert "memory_governor=get_memory_governor()" in source, (
            "Story #1213 Story 3: _search_temporal_sync must pass "
            "memory_governor=get_memory_governor() when constructing FilesystemVectorStore. "
            "Without this, the omni temporal path always evicts (safe/#1171) but never "
            "benefits from GREEN-retain."
        )

    def test_search_temporal_sync_imports_get_memory_governor(self) -> None:
        """
        _search_temporal_sync must contain a deferred import of get_memory_governor
        from ..services.memory_governor (same local-import pattern as search_service.py).
        """
        from code_indexer.server.multi.multi_search_service import MultiSearchService

        source = inspect.getsource(MultiSearchService._search_temporal_sync)

        assert "from ..services.memory_governor import get_memory_governor" in source, (
            "Story #1213 Story 3: _search_temporal_sync must contain the deferred import "
            "'from ..services.memory_governor import get_memory_governor', "
            "mirroring the pattern in search_service.py."
        )

    def test_get_memory_governor_returns_none_when_no_governor_set(self) -> None:
        """
        get_memory_governor() returns None before any governor is set.

        This confirms the CLI/solo path: memory_governor=None is passed to
        FilesystemVectorStore, which falls back to the #1171 unconditional evict
        (fail-safe, byte-identical to pre-Story-3 behavior).
        """
        from code_indexer.server.services.memory_governor import (
            clear_memory_governor,
            get_memory_governor,
        )

        clear_memory_governor()
        assert get_memory_governor() is None, (
            "get_memory_governor() must return None before set_memory_governor() "
            "is called — this is the CLI/solo fail-safe path."
        )
