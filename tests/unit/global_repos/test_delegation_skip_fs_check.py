"""Delegated pods skip filesystem checks during alias/path resolution.

When ``content_backend: delegated`` is active, cidx pods do not mount the
golden-repo NFS volume.  The ``index_path`` from the registry is still a
valid routing key for workspace-mcp / git-mcp, but the directory does not
exist locally.  All resolve-* functions must return the path without probing
the local filesystem.

Covers EVO-64616 code-fix sites:
- ``resolve_alias_or_index_path(..., skip_fs_check=True)``
- ``_resolve_repo_path()`` alias-step bypass
- ``_registry_index_path_dir()`` delegation bypass
- ``_resolve_global_repo_target()`` ``exists()`` bypass
- ``_resolve_global_repo_path_or_index()`` delegation passthrough
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

from code_indexer.global_repos.alias_manager import (
    AliasManager,
    resolve_alias_or_index_path,
)


class TestResolveAliasOrIndexPathSkipFsCheck:
    """resolve_alias_or_index_path with skip_fs_check."""

    def test_returns_index_path_when_skip_fs_check_true(self, tmp_path: Path) -> None:
        aliases_dir = tmp_path / "aliases"
        aliases_dir.mkdir()
        am = AliasManager(str(aliases_dir))

        repo_entry = {"index_path": "/data/data/golden-repos/code-indexer-internal"}
        result = resolve_alias_or_index_path(
            am,
            alias_name="code-indexer-internal-global",
            repo_entry=repo_entry,
            skip_fs_check=True,
        )
        assert result == "/data/data/golden-repos/code-indexer-internal"

    def test_returns_none_without_skip_fs_check_when_missing(
        self, tmp_path: Path
    ) -> None:
        aliases_dir = tmp_path / "aliases"
        aliases_dir.mkdir()
        am = AliasManager(str(aliases_dir))

        repo_entry = {"index_path": "/nonexistent/path"}
        result = resolve_alias_or_index_path(
            am,
            alias_name="test-repo-global",
            repo_entry=repo_entry,
            skip_fs_check=False,
        )
        assert result is None

    def test_alias_pointer_still_wins_over_index_path(self, tmp_path: Path) -> None:
        aliases_dir = tmp_path / "aliases"
        aliases_dir.mkdir()
        am = AliasManager(str(aliases_dir))

        alias_target = tmp_path / "versioned" / "v_20261015"
        alias_target.mkdir(parents=True)
        am.create_alias("my-repo-global", str(alias_target))

        repo_entry = {"index_path": "/data/data/golden-repos/my-repo"}
        result = resolve_alias_or_index_path(
            am,
            alias_name="my-repo-global",
            repo_entry=repo_entry,
            skip_fs_check=True,
        )
        assert result == str(alias_target)

    def test_returns_none_when_no_repo_entry(self, tmp_path: Path) -> None:
        aliases_dir = tmp_path / "aliases"
        aliases_dir.mkdir()
        am = AliasManager(str(aliases_dir))

        result = resolve_alias_or_index_path(
            am,
            alias_name="unknown-global",
            repo_entry=None,
            skip_fs_check=True,
        )
        assert result is None


class TestResolveRepoPathDelegated:
    """_resolve_repo_path returns index_path for delegated pods."""

    def test_returns_index_path_when_delegated(self, tmp_path: Path) -> None:
        from code_indexer.server.mcp.handlers._legacy import _resolve_repo_path

        golden_dir = str(tmp_path / "golden-repos")
        Path(golden_dir).mkdir(parents=True)
        (tmp_path / "golden-repos" / "aliases").mkdir()

        fake_entry = {
            "alias_name": "agent-router-global",
            "index_path": "/data/data/golden-repos/agent-router",
            "repo_url": "https://gitlab.com/example/agent-router.git",
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers._legacy.is_content_seam_delegated",
                return_value=True,
            ),
            patch(
                "code_indexer.server.mcp.handlers._legacy._get_global_repo",
                return_value=fake_entry,
            ),
        ):
            result = _resolve_repo_path("agent-router-global", golden_dir)

        assert result == "/data/data/golden-repos/agent-router"

    def test_returns_none_when_not_delegated_and_missing(self, tmp_path: Path) -> None:
        from code_indexer.server.mcp.handlers._legacy import _resolve_repo_path

        golden_dir = str(tmp_path / "golden-repos")
        Path(golden_dir).mkdir(parents=True)
        (tmp_path / "golden-repos" / "aliases").mkdir()

        fake_entry = {
            "alias_name": "missing-repo-global",
            "index_path": "/nonexistent/path",
        }

        with (
            patch(
                "code_indexer.server.mcp.handlers._legacy.is_content_seam_delegated",
                return_value=False,
            ),
            patch(
                "code_indexer.server.mcp.handlers._legacy._get_global_repo",
                return_value=fake_entry,
            ),
        ):
            result = _resolve_repo_path("missing-repo-global", golden_dir)

        assert result is None


class TestRegistryIndexPathDirDelegated:
    """_registry_index_path_dir bypasses is_dir() when delegated."""

    def test_returns_index_path_when_delegated(self) -> None:
        from code_indexer.server.mcp.handlers._legacy import _registry_index_path_dir

        fake_entry = {"index_path": "/data/data/golden-repos/my-repo"}
        with (
            patch(
                "code_indexer.server.mcp.handlers._legacy.is_content_seam_delegated",
                return_value=True,
            ),
            patch(
                "code_indexer.server.mcp.handlers._legacy._get_global_repo",
                return_value=fake_entry,
            ),
        ):
            result = _registry_index_path_dir("my-repo-global")

        assert result == "/data/data/golden-repos/my-repo"

    def test_returns_none_when_not_delegated_and_not_dir(self) -> None:
        from code_indexer.server.mcp.handlers._legacy import _registry_index_path_dir

        fake_entry = {"index_path": "/nonexistent"}
        with (
            patch(
                "code_indexer.server.mcp.handlers._legacy.is_content_seam_delegated",
                return_value=False,
            ),
            patch(
                "code_indexer.server.mcp.handlers._legacy._get_global_repo",
                return_value=fake_entry,
            ),
        ):
            result = _registry_index_path_dir("test-global")

        assert result is None
