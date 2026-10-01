"""Bug #1690: inline_repos.py's repository-health check blind-trusted
ConfigManager.create_with_backtrack(repo_path).get_config() without
verifying the resolved config genuinely describes repo_path itself --
mirroring the exact root-cause pattern Bug #1683 round 4 fixed for
AutoWatchManager.start_watch.

repo_path comes from `activated_repo_manager.get_activated_repo_path(...)`,
a real activated-repo resolution that should always carry its own
`.code-indexer/config.json`. When that invariant is violated (a dangling
registry entry, a partially-failed activation), `create_with_backtrack`
silently backtracks onto an unrelated ANCESTOR's config instead of failing
loud -- and `is_filesystem = config.vector_store.provider == "filesystem"`
would then report the health/status fields (container_status, query_ready,
recommendations) for the WRONG repository's configuration.

The health-check block is extracted into a standalone, directly-testable
module-level `_check_filesystem_backend_health(repo_path)` helper
(previously inline inside `get_repository_info`'s route-handler closure,
which is untestable without full FastAPI/dependency-injection setup).
Fix: it now routes through `ConfigManager.load_verified_config` (Bug
#1690) -- caught by the helper's own pre-existing broad `except Exception`
(matching the original fail-open "Unable to determine container status"
display semantics exactly) instead of silently reporting a WRONG
container_status/query_ready value for repo_path.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from code_indexer.config import ConfigManager
from code_indexer.server.routers.inline_repos import (
    _check_filesystem_backend_health,
)


@pytest.fixture
def isolated_tmp_root():
    """Rooted under `~/.tmp` (never bare `/tmp` -- project convention),
    immune to any real `.code-indexer/config.json` ancestor of `/tmp` on
    this dev machine."""
    base = Path.home() / ".tmp"
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=str(base), prefix="test_1690_ir_"))
    try:
        yield root
    finally:
        shutil.rmtree(root)


class TestCheckFilesystemBackendHealthRefusesAncestorOnlyConfig:
    def test_reports_unable_to_determine_instead_of_wrong_status(
        self, isolated_tmp_root
    ) -> None:
        ancestor_dir = isolated_tmp_root / "server-data"
        ancestor_dir.mkdir()
        ConfigManager(
            ancestor_dir / ".code-indexer" / "config.json"
        ).create_default_config(codebase_dir=ancestor_dir)

        # Dangling/partially-activated repo with no config of its own.
        repo_path = ancestor_dir / "activated-repos" / "user1" / "myrepo"
        repo_path.mkdir(parents=True)

        result = _check_filesystem_backend_health(str(repo_path))

        assert result["issues"] == ["Unable to determine container status"], (
            "Must fail open with the honest 'unable to determine' issue, "
            "not silently report a container_status/query_ready value "
            "derived from an unrelated ancestor's config."
        )
        # Must NOT report the ancestor's (arbitrary default) filesystem
        # status as if it genuinely described repo_path.
        assert result["container_status"] == "unknown"
        assert result["query_ready"] is False


class TestCheckFilesystemBackendHealthSucceedsForGenuineOwnConfig:
    def test_reports_filesystem_healthy_for_real_repo(self, isolated_tmp_root) -> None:
        import json

        repo_path = isolated_tmp_root / "real-repo"
        ci = repo_path / ".code-indexer"
        ci.mkdir(parents=True)
        (ci / "config.json").write_text(
            json.dumps(
                {
                    "codebase_dir": str(repo_path),
                    "vector_store": {"provider": "filesystem"},
                }
            )
        )

        result = _check_filesystem_backend_health(str(repo_path))

        assert result["container_status"] == "not_applicable"
        assert result["query_ready"] is True
        assert result["services"]["vector_store"]["type"] == "filesystem"
        assert result["issues"] == []
