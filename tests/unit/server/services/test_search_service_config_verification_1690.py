"""Bug #1690: search_service._load_repo_config(repo_path) blind-trusted
ConfigManager.create_with_backtrack(repo_path).get_config() without
verifying the resolved config genuinely describes repo_path itself --
mirroring the exact root-cause pattern Bug #1683 round 4 fixed for
AutoWatchManager.start_watch.

repo_path always comes from a real activated/golden repo resolution
upstream (search_similar's caller), so it is expected to carry its own
`.code-indexer/config.json`. When that invariant is violated (a dangling
registry entry, a partially-failed activation, corrupted state --
documented failure modes elsewhere in this codebase, e.g. Bug #1317's
registry-orphan guard), `create_with_backtrack` silently backtracks onto an
unrelated ANCESTOR's config instead of failing loud. The returned Config's
embedding-provider/vector_store settings would then be silently wrong for
`repo_path`, even though `BackendFactory.create(project_root=repo_path)`
still targets the correct physical directory -- the embedding/provider
settings used to search that directory would be wrong.

Fix: `_load_repo_config` now routes through `ConfigManager.load_verified_config`
(Bug #1690), which raises `ConfigVerificationError` (a `ValueError`
subclass) instead of returning the ancestor's config.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from code_indexer.config import ConfigManager, ConfigVerificationError
from code_indexer.server.services import search_service as ss


@pytest.fixture
def isolated_tmp_root():
    """Rooted under `~/.tmp` (never bare `/tmp` -- project convention),
    immune to any real `.code-indexer/config.json` ancestor of `/tmp` on
    this dev machine."""
    base = Path.home() / ".tmp"
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=str(base), prefix="test_1690_ss_"))
    try:
        yield root
    finally:
        shutil.rmtree(root)


@pytest.fixture
def _no_registry(monkeypatch):
    """Force the direct-load branch of _load_repo_config (no RepoConfigCache
    wired) so create_with_backtrack is exercised directly, matching
    CLI-in-process / unit-test conditions."""
    monkeypatch.setattr(ss, "_get_repo_config_cache", lambda: None)


class TestLoadRepoConfigRefusesAncestorOnlyConfig:
    def test_raises_config_verification_error(
        self, isolated_tmp_root, _no_registry
    ) -> None:
        ancestor_dir = isolated_tmp_root / "server-data"
        ancestor_dir.mkdir()
        ConfigManager(
            ancestor_dir / ".code-indexer" / "config.json"
        ).create_default_config(codebase_dir=ancestor_dir)

        # A dangling/orphaned repo_path with no config of its own, nested
        # under the ancestor.
        repo_path = ancestor_dir / "activated-repos" / "user1" / "myrepo"
        repo_path.mkdir(parents=True)

        with pytest.raises(ConfigVerificationError):
            ss._load_repo_config(str(repo_path))


class TestLoadRepoConfigSucceedsForGenuineOwnConfig:
    def test_returns_config_for_real_repo(
        self, isolated_tmp_root, _no_registry
    ) -> None:
        repo_path = isolated_tmp_root / "real-repo"
        ci = repo_path / ".code-indexer"
        ci.mkdir(parents=True)
        (ci / "config.json").write_text(
            json.dumps(
                {"codebase_dir": str(repo_path), "embedding_provider": "voyage-ai"}
            )
        )

        config = ss._load_repo_config(str(repo_path))

        assert Path(config.codebase_dir).resolve() == repo_path.resolve()
