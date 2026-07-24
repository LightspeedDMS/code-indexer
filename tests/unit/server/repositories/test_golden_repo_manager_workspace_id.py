"""EVO-65021: GoldenRepoManager workspace_id accessors.

The delegated SCIP indexer (stream-01) must hand sandbox-mcp the workspace-mcp
``workspace_id`` (``sb-{workspace_id}``) of a golden repo's workspace. These
tests cover the accessor surface stream-01 consumes:

- resolve_workspace_id(alias) -> str            (the alias -> workspace_id map)
- resolve_workspace_id missing-alias / unset -> WorkspaceIdNotRetainedError
- get_workspace_id(alias) -> Optional[str]      (non-raising probe)
- set_workspace_id(alias, id)                    (capture / backfill)

Uses the REAL GoldenRepoMetadataSqliteBackend via GoldenRepoManager(data_dir=…)
(no mocked DB layer; memory: feedback_faithful_db_mocks).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.server.repositories.golden_repo_manager import (
    GoldenRepoManager,
    WorkspaceIdNotRetainedError,
)


def _register(manager: GoldenRepoManager, alias: str, clone_path: Path, **overrides):
    """Register a golden repo straight into the shared backend + cache."""
    clone_path.mkdir(parents=True, exist_ok=True)
    kwargs = dict(
        alias=alias,
        repo_url="https://example.com/repo.git",
        default_branch="main",
        clone_path=str(clone_path),
        created_at="2026-01-01T00:00:00+00:00",
    )
    kwargs.update(overrides)
    manager._sqlite_backend.add_repo(**kwargs)


@pytest.fixture
def manager(tmp_path) -> GoldenRepoManager:
    return GoldenRepoManager(data_dir=str(tmp_path))


class TestResolveWorkspaceId:
    def test_resolves_retained_workspace_id(self, manager, tmp_path):
        _register(
            manager,
            "svc-foo",
            tmp_path / "golden-repos" / "svc-foo",
            workspace_id="ws-abcd1234",
        )
        assert manager.resolve_workspace_id("svc-foo") == "ws-abcd1234"

    def test_unknown_alias_raises(self, manager):
        with pytest.raises(WorkspaceIdNotRetainedError, match="not found"):
            manager.resolve_workspace_id("does-not-exist")

    def test_unset_workspace_id_raises(self, manager, tmp_path):
        # A repo registered before workspace_id retention (NULL id).
        _register(manager, "legacy-repo", tmp_path / "golden-repos" / "legacy-repo")
        with pytest.raises(
            WorkspaceIdNotRetainedError, match="no retained workspace_id"
        ):
            manager.resolve_workspace_id("legacy-repo")


class TestGetWorkspaceId:
    def test_returns_id_when_retained(self, manager, tmp_path):
        _register(
            manager,
            "svc-bar",
            tmp_path / "golden-repos" / "svc-bar",
            workspace_id="ws-55aa",
        )
        assert manager.get_workspace_id("svc-bar") == "ws-55aa"

    def test_returns_none_for_unknown_alias(self, manager):
        assert manager.get_workspace_id("ghost") is None

    def test_returns_none_when_unset(self, manager, tmp_path):
        _register(manager, "no-ws", tmp_path / "golden-repos" / "no-ws")
        assert manager.get_workspace_id("no-ws") is None


class TestSetWorkspaceId:
    def test_backfills_and_is_resolvable(self, manager, tmp_path):
        _register(manager, "backfill-me", tmp_path / "golden-repos" / "backfill-me")
        # Prime this worker's cache so we also prove the cache is refreshed.
        assert manager.get_workspace_id("backfill-me") is None

        assert manager.set_workspace_id("backfill-me", "ws-99ff") is True

        # Shared backend persisted it...
        assert (
            manager._sqlite_backend.get_repo("backfill-me")["workspace_id"] == "ws-99ff"
        )
        # ...and both accessors (cache-first) now see it without a reload.
        assert manager.get_workspace_id("backfill-me") == "ws-99ff"
        assert manager.resolve_workspace_id("backfill-me") == "ws-99ff"

    def test_missing_alias_returns_false(self, manager):
        assert manager.set_workspace_id("nobody", "ws-0000") is False


class TestCaptureWiring:
    """The registration capture path accepts + forwards workspace_id.

    The full execute_add_golden_repo_work workflow (clone + index + global
    activation) is exercised elsewhere; here we assert only that the public
    entry points expose a workspace_id parameter (so a caller that knows the
    id can retain it) without running that heavy workflow.
    """

    def test_add_golden_repo_accepts_workspace_id_param(self):
        import inspect

        sig = inspect.signature(GoldenRepoManager.add_golden_repo)
        assert "workspace_id" in sig.parameters

    def test_execute_add_golden_repo_work_accepts_workspace_id_param(self):
        import inspect

        sig = inspect.signature(GoldenRepoManager.execute_add_golden_repo_work)
        assert "workspace_id" in sig.parameters
