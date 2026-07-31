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


class TestAuthoritativeResolve:
    """EVO-65021 (Bug #1316 shape): resolve_workspace_id must read the
    AUTHORITATIVE shared-backend state, not a stale per-worker cache HIT — its
    outcome decides whether a delegated SCIP build runs."""

    def test_resolve_sees_cross_worker_backfill_despite_stale_cache(
        self, manager, tmp_path
    ):
        # Repo registered with NO workspace_id; this worker's cache holds it.
        _register(manager, "svc-x", tmp_path / "golden-repos" / "svc-x")
        assert manager.get_workspace_id("svc-x") is None  # primes the cache
        assert manager.golden_repos["svc-x"].workspace_id is None

        # ANOTHER worker backfills the shared backend directly — this worker's
        # in-memory cache is NOT refreshed (simulating cross-worker staleness;
        # we deliberately bypass set_workspace_id's local model_copy).
        manager._sqlite_backend.update_workspace_id("svc-x", "ws-backfilled")

        # A cache-first probe still returns the stale None...
        assert manager.get_workspace_id("svc-x") is None
        # ...but resolve_workspace_id reads authoritatively and sees the value,
        # so the delegated build is NOT wrongly fail-softed as "no workspace_id".
        assert manager.resolve_workspace_id("svc-x") == "ws-backfilled"
        # Authoritative read also refreshed the cache.
        assert manager.golden_repos["svc-x"].workspace_id == "ws-backfilled"


class TestCaptureWiring:
    """The registration capture path actually FORWARDS workspace_id end-to-end.

    These drive real behaviour (not a signature check): the GoldenRepo model hop
    and the DB-layer add_repo hand-off. Dropping ``workspace_id=workspace_id``
    from either now fails a test.
    """

    def test_model_hop_preserves_workspace_id(self):
        from code_indexer.server.repositories.golden_repo_manager import GoldenRepo

        repo = GoldenRepo(
            alias="svc-x",
            repo_url="https://example.com/r.git",
            default_branch="main",
            clone_path="/tmp/x",
            created_at="2026-01-01T00:00:00+00:00",
            workspace_id="ws-model-hop",
        )
        assert repo.to_dict()["workspace_id"] == "ws-model-hop"
        # Round-trips back through the constructor (get_repo -> GoldenRepo(**row)).
        assert GoldenRepo(**repo.to_dict()).workspace_id == "ws-model-hop"

    def test_execute_add_golden_repo_work_retains_workspace_id_to_db(
        self, manager, tmp_path, monkeypatch
    ):
        """Drive the real forwarding: stub the clone/index/activation seams, then
        assert the DB row (real sqlite backend) stores the workspace_id."""
        clone_dir = tmp_path / "golden-repos" / "svc-cap"
        clone_dir.mkdir(parents=True, exist_ok=True)

        # Stub the heavy seams (clone + post-clone index) — leave add_repo real.
        monkeypatch.setattr(manager, "_remove_orphan_clone_for_retry", lambda *a: False)
        monkeypatch.setattr(manager, "_clone_repository", lambda *a: str(clone_dir))
        monkeypatch.setattr(manager, "_is_in_place_registration", lambda *a: True)
        monkeypatch.setattr(
            manager, "_execute_post_clone_workflow", lambda *a, **k: None
        )
        monkeypatch.setattr(
            manager, "_register_lifecycle_after_registration", lambda *a, **k: None
        )

        # Stub global activation (writes pointer files) so it succeeds without git.
        import code_indexer.global_repos.global_activation as ga

        class _FakeActivator:
            def __init__(self, *a, **k):
                pass

            def activate_golden_repo(self, *a, **k):
                return None

        monkeypatch.setattr(ga, "GlobalActivator", _FakeActivator)

        result = manager.execute_add_golden_repo_work(
            repo_url="https://example.com/svc-cap.git",
            alias="svc-cap",
            default_branch="main",
            workspace_id="ws-retained-42",
        )
        assert result["success"] is True

        # The real DB row carries the workspace_id (guards the add_repo hand-off).
        row = manager._sqlite_backend.get_repo("svc-cap")
        assert row["workspace_id"] == "ws-retained-42"
        # And it is resolvable through the accessor stream-01 consumes.
        assert manager.resolve_workspace_id("svc-cap") == "ws-retained-42"
