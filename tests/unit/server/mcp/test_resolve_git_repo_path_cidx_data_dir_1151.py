"""
Unit tests for Bug #1151: _resolve_git_repo_path must honor CIDX_SERVER_DATA_DIR.

The resolver at _legacy.py:321 previously constructed ActivatedRepoManager()
with no data_dir argument, which always defaulted to ~/.cidx-server/data
regardless of CIDX_SERVER_DATA_DIR.  On deployments with a non-default
server_dir (or Bug #879 CIDX_DATA_DIR / different-OS-user scenarios) this
caused spurious "not found" errors for every git-write/file MCP operation
against a user-activated repository.

Fix: derive data_dir the same way service_init.py does (lines 162-163 / 195):
    _server_data_dir = os.environ.get(
        "CIDX_SERVER_DATA_DIR", str(Path.home() / ".cidx-server")
    )
    activated_repo_manager = ActivatedRepoManager(
        data_dir=str(Path(_server_data_dir) / "data")
    )

Tests in this module:
1. Resolver finds a user-activated repo under a custom CIDX_SERVER_DATA_DIR.
2. Resolver still works when CIDX_SERVER_DATA_DIR is unset (default path).
3. Resolver returns "not found" when the env var points elsewhere (no repo there).
"""

from pathlib import Path


class TestResolveGitRepoPathHonorsCidxServerDataDir:
    """Bug #1151: _resolve_git_repo_path must read CIDX_SERVER_DATA_DIR."""

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _make_activated_repo(
        self, base_data_dir: Path, username: str, alias: str
    ) -> Path:
        """Create the expected activated-repo directory layout with a .git dir."""
        # ActivatedRepoManager stores repos under:
        #   {data_dir}/activated-repos/{username}/{alias}/
        repo_path = base_data_dir / "data" / "activated-repos" / username / alias
        repo_path.mkdir(parents=True, exist_ok=True)
        (repo_path / ".git").mkdir(exist_ok=True)
        return repo_path

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_resolver_honors_cidx_server_data_dir(self, tmp_path, monkeypatch):
        """Resolver finds user-activated repo under custom CIDX_SERVER_DATA_DIR.

        This is the core Bug #1151 regression test.

        Setup:
          - CIDX_SERVER_DATA_DIR -> tmp_path (NOT ~/.cidx-server)
          - Real activated-repo dir created at tmp_path/data/activated-repos/testuser/my-repo/
          - .git directory present so git-op check passes

        Before the fix: ActivatedRepoManager() defaults to ~/.cidx-server/data ->
          get_activated_repo_path returns None -> "not found" error.
        After the fix: ActivatedRepoManager(data_dir=tmp_path/data) -> finds the repo.
        """
        from code_indexer.server.mcp.handlers._legacy import _resolve_git_repo_path

        repo_path = self._make_activated_repo(tmp_path, "testuser", "my-repo")

        monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))

        path, error_msg = _resolve_git_repo_path("my-repo", "testuser")

        assert error_msg is None, f"Expected success but got error: {error_msg}"
        assert path == str(repo_path)

    def test_resolver_default_works_when_env_unset(self, tmp_path, monkeypatch):
        """When CIDX_SERVER_DATA_DIR is unset, default ~/.cidx-server is used.

        Production-default behavior must be unchanged by the fix.
        We cannot create repos under the real ~/.cidx-server in a unit test,
        so we verify the resolver falls back to ActivatedRepoManager's own
        default by asserting the error message is the expected "not found"
        rather than a crash/wrong error.
        """
        from code_indexer.server.mcp.handlers._legacy import _resolve_git_repo_path

        # Remove the env var so the default path is used
        monkeypatch.delenv("CIDX_SERVER_DATA_DIR", raising=False)

        # This alias almost certainly does not exist in ~/.cidx-server/data,
        # so we expect "not found" — the important thing is no exception raised.
        path, error_msg = _resolve_git_repo_path(
            "nonexistent-repo-bug1151-test", "testuser"
        )

        assert path is None
        assert error_msg is not None
        # The resolver may return "not found" (path is None) or
        # "does not support git operations" (.git absent) depending on
        # what happens to be on disk under the default path — both are
        # valid "this repo is unavailable" responses.
        assert (
            "not found" in error_msg or "does not support git operations" in error_msg
        )

    def test_resolver_returns_not_found_for_wrong_data_dir(self, tmp_path, monkeypatch):
        """When env var points to a dir with no matching repo, returns not found.

        Ensures the resolver does not fall through to ~/.cidx-server when the
        custom dir has no matching activation.
        """
        from code_indexer.server.mcp.handlers._legacy import _resolve_git_repo_path

        # Point CIDX_SERVER_DATA_DIR to an empty tmp dir (no activated repos)
        monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))

        path, error_msg = _resolve_git_repo_path("my-repo", "testuser")

        assert path is None
        assert error_msg is not None
        # Empty tmp dir -> path is None -> "not found"
        assert (
            "not found" in error_msg or "does not support git operations" in error_msg
        )
