"""Tests for governor injection onto FilesystemBackend and FilesystemVectorStore (Story #1213 Story 3).

Covers:
- FilesystemBackend defaults memory_governor to None (CLI mode unchanged).
- FilesystemBackend stores the injected governor.
- FilesystemVectorStore accepts and stores memory_governor kwarg.
"""

from unittest.mock import MagicMock

from code_indexer.backends.filesystem_backend import FilesystemBackend
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.server.services.memory_governor import MemoryGovernor


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_governor() -> MemoryGovernor:
    readers = MagicMock()
    total = 8 * 1024 * 1024 * 1024
    vm = MagicMock()
    vm.total = total
    vm.used = int(total * 0.1)
    readers.read_host_memory.return_value = vm
    readers.read_cgroup_v2_max.side_effect = FileNotFoundError
    readers.read_cgroup_v1_limit.side_effect = FileNotFoundError
    readers.read_pswpin.return_value = 0
    return MemoryGovernor(
        readers=readers,
        enabled=True,
        start_sampler=False,
        yellow_pct=70.0,
        red_pct=85.0,
        hysteresis_pct=10.0,
        red_min_dwell_seconds=0.0,
    )


# ---------------------------------------------------------------------------
# Test class: FilesystemBackend + FilesystemVectorStore wiring
# ---------------------------------------------------------------------------


class TestGovernorInjectionWiring:
    """Governor must be injectable via FilesystemBackend and FilesystemVectorStore."""

    def test_filesystem_backend_defaults_governor_to_none(self, tmp_path):
        """FilesystemBackend without memory_governor -> attribute is None (CLI unchanged)."""
        backend = FilesystemBackend(project_root=tmp_path)
        assert backend.memory_governor is None

    def test_filesystem_backend_stores_injected_governor(self, tmp_path):
        """FilesystemBackend stores the injected governor on the attribute."""
        gov = _make_governor()
        backend = FilesystemBackend(project_root=tmp_path, memory_governor=gov)
        assert backend.memory_governor is gov

    def test_filesystem_vector_store_stores_injected_governor(self, tmp_path):
        """FilesystemVectorStore stores the injected governor as memory_governor attr."""
        gov = _make_governor()
        vectors_dir = tmp_path / ".code-indexer" / "index"
        vs = FilesystemVectorStore(
            base_path=vectors_dir,
            project_root=tmp_path,
            memory_governor=gov,
        )
        assert vs.memory_governor is gov
