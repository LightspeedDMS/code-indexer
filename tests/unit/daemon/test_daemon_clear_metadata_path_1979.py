"""Bug #1979 (P1, round 4 -- REVERTED): daemon-mode semantic indexing
writes the bare legacy `metadata.json`, matching its pre-round-3 behavior.

Round 3 changed the daemon's index-write path to the per-provider filename
`metadata-<provider>.json` so `find_providers_not_rebuilt_since`
(provider_rebuild_check.py) could see a daemon-mode clear's real completion
timestamp. Round 4's coordinator ruling reverted this instead: propagating
the new filename to every OTHER reader (`cidx status`, foreground `cidx
watch`, config_fixer) turned out to regress a fresh daemon-only project's
`cidx status` to "Not Found". The smallest-blast-radius fix keeps the
daemon writing the SAME bare filename it always did, and gives
`find_providers_not_rebuilt_since` a `daemon_mode` flag (see
`tests/unit/services/test_provider_rebuild_check_daemon_mode_1979.py`) so
the shared check can read the daemon's bare file instead of requiring a
per-provider one.

This test captures the `SmartIndexer` constructor call (mirrors the
pattern already used by
`tests/unit/daemon/test_service_config_verification_1718.py`) and asserts
the metadata_path argument uses the bare legacy filename.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Mock rpyc before import if not available (matches sibling daemon test files).
try:
    import rpyc  # noqa: F401
except ImportError:
    sys.modules["rpyc"] = MagicMock()
    sys.modules["rpyc.utils.server"] = MagicMock()

from code_indexer.config import ConfigManager
from code_indexer.daemon.service import CIDXDaemonService


@pytest.fixture
def real_project():
    """A genuine project directory with its own `.code-indexer/config.json`,
    rooted under the project's gitignored `.tmp` directory."""
    base = Path(__file__).resolve().parents[3] / ".tmp"
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=str(base), prefix="test_1979_metadata_"))
    ConfigManager(root / ".code-indexer" / "config.json").create_default_config(
        codebase_dir=root
    )
    yield root
    shutil.rmtree(root, ignore_errors=True)


def _fake_backend() -> MagicMock:
    backend = MagicMock()
    backend.get_vector_store_client.return_value = MagicMock()
    return backend


class TestExposedIndexBlockingWritesBareMetadata:
    """`exposed_index_blocking`'s standard semantic-indexing branch must
    construct SmartIndexer with the bare legacy `metadata.json` -- the
    daemon's pre-round-3 behavior, restored by round 4's revert."""

    def test_clear_writes_bare_metadata_path(self, real_project):
        service = CIDXDaemonService()

        with (
            patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create",
                return_value=MagicMock(),
            ),
            patch(
                "code_indexer.backends.backend_factory.BackendFactory.create",
                return_value=_fake_backend(),
            ),
            patch(
                "code_indexer.services.smart_indexer.SmartIndexer"
            ) as mock_smart_indexer,
        ):
            service.exposed_index_blocking(str(real_project), force_full=True)

        assert mock_smart_indexer.called, "SmartIndexer must be constructed"
        constructed_metadata_path = mock_smart_indexer.call_args[0][3]

        # Round 4 revert: the daemon writes the SAME bare filename it wrote
        # before round 3 -- other readers (cidx status, foreground watch,
        # config_fixer) still expect this file, unchanged.
        assert constructed_metadata_path.name == "metadata.json", (
            f"expected the bare legacy metadata filename (daemon-write "
            f"revert), got: {constructed_metadata_path.name}"
        )
