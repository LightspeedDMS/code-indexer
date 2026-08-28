"""Bug #1690: reconstruct_temporal_backend() blind-trusted
ConfigManager.create_with_backtrack(repo_path).get_config() without
verifying the resolved config genuinely describes repo_path itself --
mirroring the exact root-cause pattern Bug #1683 round 4 fixed for
AutoWatchManager.start_watch.

repo_path is a real activated/golden repo clone (per this function's own
docstring: "activation's CoW clone" or a golden repo path), expected to
carry its own .code-indexer/config.json. When that invariant is violated
(a dangling registry entry, corrupted state), create_with_backtrack
silently backtracks onto an unrelated ANCESTOR's config instead of failing
loud, and the temporal fusion dispatch downstream would then silently use
the WRONG embedder/config settings for this repo.

Fix: reconstruct_temporal_backend now routes through
ConfigManager.load_verified_config (Bug #1690), which raises
ConfigVerificationError (a ValueError subclass) instead of returning the
ancestor's config -- this must happen BEFORE BackendFactory.create is ever
touched.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.config import ConfigManager, ConfigVerificationError
from code_indexer.server.query.semantic_query_manager import (
    reconstruct_temporal_backend,
)


@pytest.fixture
def isolated_tmp_root():
    """Rooted under `~/.tmp` (never bare `/tmp` -- project convention),
    immune to any real `.code-indexer/config.json` ancestor of `/tmp` on
    this dev machine."""
    base = Path.home() / ".tmp"
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=str(base), prefix="test_1690_rtb_"))
    try:
        yield root
    finally:
        shutil.rmtree(root)


class TestReconstructTemporalBackendRefusesAncestorOnlyConfig:
    def test_raises_before_touching_backend_factory(self, isolated_tmp_root) -> None:
        ancestor_dir = isolated_tmp_root / "server-data"
        ancestor_dir.mkdir()
        ConfigManager(
            ancestor_dir / ".code-indexer" / "config.json"
        ).create_default_config(codebase_dir=ancestor_dir)

        # A dangling/orphaned repo_path with no config of its own, nested
        # under the ancestor.
        repo_path = ancestor_dir / "activated-repos" / "user1" / "myrepo"
        repo_path.mkdir(parents=True)

        with patch(
            "code_indexer.backends.backend_factory.BackendFactory.create",
            return_value=MagicMock(),
        ) as mock_create:
            with pytest.raises(ConfigVerificationError):
                reconstruct_temporal_backend(
                    repo_path=repo_path,
                    repository_alias="myrepo",
                )

        mock_create.assert_not_called()
