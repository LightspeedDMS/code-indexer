"""Delegated pods skip filesystem checks during alias/path resolution.

When ``content_backend: delegated`` is active, cidx pods do not mount the
golden-repo NFS volume.  The ``index_path`` from the registry is still a
valid routing key for workspace-mcp / git-mcp, but the directory does not
exist locally.  All resolve-* functions must return the path without probing
the local filesystem.

Covers issue-64616 code-fix sites:
- ``resolve_alias_or_index_path(..., skip_fs_check=True)``
- ``_resolve_repo_path()`` alias-step bypass
- ``_registry_index_path_dir()`` delegation bypass
- ``_resolve_global_repo_target()`` ``exists()`` bypass
- ``_resolve_global_repo_path_or_index()`` delegation passthrough
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from code_indexer.global_repos.alias_manager import (
    AliasManager,
    resolve_alias_or_index_path,
)
from code_indexer.server.auth.user_manager import User, UserRole


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


def _test_user() -> User:
    from datetime import datetime, timezone

    return User(
        username="testuser",
        password_hash="h",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


class TestResolveGlobalRepoTargetDelegated:
    """_resolve_global_repo_target skips exists() when delegated."""

    _REPO_ENTRY = {
        "alias_name": "agent-router-global",
        "index_path": "/data/data/golden-repos/agent-router",
        "repo_url": "https://gitlab.com/example/agent-router.git",
    }
    _SEARCH_MOD = "code_indexer.server.mcp.handlers.search.repo_search"
    _WIRING_MOD = "code_indexer.server.startup.content_backend_wiring"

    def test_returns_path_when_delegated_and_missing_locally(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.server.mcp.handlers.search.repo_search import (
            _resolve_global_repo_target,
        )

        golden_dir = str(tmp_path / "golden-repos")
        Path(golden_dir).mkdir(parents=True)
        (tmp_path / "golden-repos" / "aliases").mkdir()

        with (
            patch(
                f"{self._WIRING_MOD}.is_content_seam_delegated",
                return_value=True,
            ),
            patch(
                f"{self._SEARCH_MOD}._get_golden_repos_dir",
                return_value=golden_dir,
            ),
            patch(
                f"{self._SEARCH_MOD}._list_global_repos",
                return_value=[self._REPO_ENTRY],
            ),
        ):
            repo_entry, target_path, err = _resolve_global_repo_target(
                "agent-router-global", _test_user()
            )

        assert err is None
        assert target_path == "/data/data/golden-repos/agent-router"
        assert repo_entry["alias_name"] == "agent-router-global"

    def test_raises_repository_not_found_when_not_delegated_and_missing(
        self, tmp_path: Path, bind_server_app_attr
    ) -> None:
        """A local target missing on disk is the caller's unknown repository
        (SearchRepositoryNotFoundError, whose text reaches the client), never a
        bare FileNotFoundError naming the on-disk path."""
        from code_indexer.server.mcp.handlers.search.repo_search import (
            _resolve_global_repo_target,
        )
        from code_indexer.server.query.semantic_query_manager import (
            SearchRepositoryNotFoundError,
        )

        # Pre-bind `app` so _is_search_seam_delegated() doesn't trigger
        # PEP 562 __getattr__ -> create_app() (server app boot guard).
        bind_server_app_attr("app", SimpleNamespace(state=SimpleNamespace()))

        golden_dir = str(tmp_path / "golden-repos")
        Path(golden_dir).mkdir(parents=True)
        (tmp_path / "golden-repos" / "aliases").mkdir()

        with (
            patch(
                f"{self._WIRING_MOD}.is_content_seam_delegated",
                return_value=False,
            ),
            patch(
                f"{self._SEARCH_MOD}._get_golden_repos_dir",
                return_value=golden_dir,
            ),
            patch(
                f"{self._SEARCH_MOD}._list_global_repos",
                return_value=[self._REPO_ENTRY],
            ),
            patch(
                "code_indexer.global_repos.alias_manager.resolve_alias_or_index_path",
                return_value="/nonexistent/delegated/path",
            ),
            pytest.raises(SearchRepositoryNotFoundError),
        ):
            _resolve_global_repo_target("agent-router-global", _test_user())


class TestResolveGlobalRepoPathOrIndexDelegated:
    """_resolve_global_repo_path_or_index forwards skip_fs_check."""

    _UTILS_MOD = "code_indexer.server.mcp.handlers._utils"
    _WIRING_MOD = "code_indexer.server.startup.content_backend_wiring"

    def test_passes_skip_fs_check_true_when_delegated(self, tmp_path: Path) -> None:
        from code_indexer.server.mcp.handlers._utils import (
            _resolve_global_repo_path_or_index,
        )

        golden_dir = str(tmp_path / "golden-repos")
        Path(golden_dir).mkdir(parents=True)
        (tmp_path / "golden-repos" / "aliases").mkdir()

        fake_entry = {
            "alias_name": "my-repo-global",
            "index_path": "/data/data/golden-repos/my-repo",
        }

        with (
            patch(
                f"{self._WIRING_MOD}.is_content_seam_delegated",
                return_value=True,
            ),
            patch(
                f"{self._UTILS_MOD}._get_golden_repos_dir",
                return_value=golden_dir,
            ),
            patch(
                f"{self._UTILS_MOD}._list_global_repos",
                return_value=[fake_entry],
            ),
        ):
            result = _resolve_global_repo_path_or_index("my-repo-global")

        assert result == "/data/data/golden-repos/my-repo"

    def test_returns_none_when_not_delegated_and_missing(self, tmp_path: Path) -> None:
        from code_indexer.server.mcp.handlers._utils import (
            _resolve_global_repo_path_or_index,
        )

        golden_dir = str(tmp_path / "golden-repos")
        Path(golden_dir).mkdir(parents=True)
        (tmp_path / "golden-repos" / "aliases").mkdir()

        fake_entry = {
            "alias_name": "my-repo-global",
            "index_path": "/nonexistent/path",
        }

        with (
            patch(
                f"{self._WIRING_MOD}.is_content_seam_delegated",
                return_value=False,
            ),
            patch(
                f"{self._UTILS_MOD}._get_golden_repos_dir",
                return_value=golden_dir,
            ),
            patch(
                f"{self._UTILS_MOD}._list_global_repos",
                return_value=[fake_entry],
            ),
        ):
            result = _resolve_global_repo_path_or_index("my-repo-global")

        assert result is None


class TestHasGitDirDelegatedProbe:
    """``_has_git_dir`` asks the git seam with an argv git-mcp permits.

    git-mcp's git_exec policy refuses ``rev-parse --git-dir``; probing with it
    failed git_status / git_diff / git_log on every activated repository of a
    mount-less pod. The probe is ``rev-parse --is-inside-work-tree``.
    """

    _LEGACY_MOD = "code_indexer.server.mcp.handlers._legacy"
    _SEAM_MOD = "code_indexer.server.services.git_tools_seam"

    class _FakeTools:
        def __init__(self, returncode: int, stdout: str) -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.calls: list = []

        def run(self, cmd, cwd, check=True, timeout=None, env=None):
            import subprocess

            self.calls.append((list(cmd), str(cwd), check))
            return subprocess.CompletedProcess(
                list(cmd), self.returncode, stdout=self.stdout, stderr=""
            )

    def _probe(self, tools: "TestHasGitDirDelegatedProbe._FakeTools") -> bool:
        from code_indexer.server.mcp.handlers._legacy import _has_git_dir

        with (
            patch(f"{self._LEGACY_MOD}.is_content_seam_delegated", return_value=True),
            patch(f"{self._SEAM_MOD}.delegated_git_tools", return_value=tools),
        ):
            return _has_git_dir("/data/data/activated-repos/admin/repo")

    def test_probe_uses_is_inside_work_tree_never_git_dir(self) -> None:
        tools = self._FakeTools(0, "true\n")
        assert self._probe(tools) is True
        assert tools.calls == [
            (
                ["git", "rev-parse", "--is-inside-work-tree"],
                "/data/data/activated-repos/admin/repo",
                False,
            )
        ]
        assert all("--git-dir" not in argv for argv, _, _ in tools.calls)

    def test_not_a_repository_is_false(self) -> None:
        assert self._probe(self._FakeTools(128, "")) is False

    def test_inside_git_dir_but_not_a_work_tree_is_false(self) -> None:
        assert self._probe(self._FakeTools(0, "false\n")) is False

    def test_no_delegated_tools_is_false(self) -> None:
        from code_indexer.server.mcp.handlers._legacy import _has_git_dir

        with (
            patch(f"{self._LEGACY_MOD}.is_content_seam_delegated", return_value=True),
            patch(f"{self._SEAM_MOD}.delegated_git_tools", return_value=None),
        ):
            assert _has_git_dir("/data/data/activated-repos/admin/repo") is False
