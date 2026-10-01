"""Bug #1690: stats_service.py's Bug #1691 fix left a round-3-vs-round-4
gap identical to the one Bug #1683 round 4 fixed for
AutoWatchManager.start_watch.

Bug #1691's `_build_vector_store_client` guard
(`if not config_manager.config_path.exists(): raise RuntimeError(...)`)
only catches the "no config found anywhere" case (`create_with_backtrack()`
defaulting `config_path` to a nonexistent `{cwd}/.code-indexer/config.json`).
It does NOT catch the "found only an ANCESTOR's config" case:
`ConfigManager.find_config_path()` walks UP from the server process's CWD,
and a REAL ancestor `.code-indexer/config.json` (confirmed present on this
dev machine at several ancestors of `/tmp`, per
test_stats_service_lazy_construction_1691.py's own docstring) makes
`config_path.exists()` return True even though that config's
`codebase_dir` is the ANCESTOR directory, not the server's actual CWD.
`get_embedding_count`'s single shared `vector_store_client` would then
silently report stats from the WRONG vector store base directory.

Fix: extend the guard with the SAME strict-equality verification Bug #1683
round 4 established -- compare the resolved `config.codebase_dir` against
the resolved CWD (the implicit target of a start_dir-less
`create_with_backtrack()` call), fail loud on mismatch.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from code_indexer.config import ConfigManager


@pytest.fixture
def isolated_tmp_root():
    """Rooted under `~/.tmp` (never bare `/tmp` -- project convention),
    immune to any real `.code-indexer/config.json` ancestor of `/tmp` on
    this dev machine."""
    base = Path.home() / ".tmp"
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=str(base), prefix="test_1690_stats_"))
    try:
        yield root
    finally:
        shutil.rmtree(root)


@pytest.fixture
def ancestor_config_cwd(isolated_tmp_root, monkeypatch):
    """A real ancestor `.code-indexer/config.json` under isolated_tmp_root,
    with the server CWD chdir'd into a NESTED subdirectory that has no
    config of its own -- find_config_path will walk up and find the
    ancestor's, exactly like Bug #1683 round 4's discriminating scenario.
    """
    ancestor_dir = isolated_tmp_root / "server-data"
    ancestor_dir.mkdir()
    ConfigManager(ancestor_dir / ".code-indexer" / "config.json").create_default_config(
        codebase_dir=ancestor_dir
    )

    cwd = ancestor_dir / "run" / "cidx-server"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    return cwd


class TestBuildVectorStoreClientRefusesAncestorOnlyConfig:
    def test_raises_instead_of_using_ancestor_config(self, ancestor_config_cwd) -> None:
        from code_indexer.server.services.stats_service import (
            RepositoryStatsService,
        )

        service = RepositoryStatsService()

        with pytest.raises(RuntimeError):
            _ = service.vector_store_client

        # Must not leave a stray .code-indexer/index directory behind at
        # the CWD (mirrors Bug #1691's own no-side-effect assertion).
        assert not (ancestor_config_cwd / ".code-indexer").exists()
