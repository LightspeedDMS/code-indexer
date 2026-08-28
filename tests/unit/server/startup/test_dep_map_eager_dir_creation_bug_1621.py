"""Bug #1621: eager dep-map output directory creation at startup.

On a never-before-visited node, `_get_dep_map_output_dir()` in
dependency_map_routes.py returns None because
golden-repos/cidx-meta/dependency-map/ does not exist on disk yet -- it was
previously only ever created lazily, as a side effect of the first analysis
run. This caused the very first HTMX poll of the dashboard partial to render
"Dashboard analysis infrastructure unavailable" even though the directory
would be created moments later.

The fix eagerly creates that directory at server startup (a trivial O(1)
single `mkdir`, safe to run unconditionally) via a new
`_ensure_dep_map_output_dir()` helper in startup/lifespan.py, wired into the
existing dependency-map scheduler initialization block BEFORE the pre-existing
stale-sentinel-cleanup `.exists()` check.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest


class TestEnsureDepMapOutputDirHelper:
    """Unit tests for the new `_ensure_dep_map_output_dir()` helper (real filesystem)."""

    def test_creates_directory_when_missing(self, tmp_path):
        """Calling the helper on a never-before-visited golden-repos dir creates
        cidx-meta/dependency-map/ on disk (real filesystem, no mocking)."""
        from code_indexer.server.startup.lifespan import _ensure_dep_map_output_dir

        golden_repos_dir = tmp_path / "golden-repos"
        expected = golden_repos_dir / "cidx-meta" / "dependency-map"
        assert not expected.exists(), "precondition: directory must not pre-exist"

        result = _ensure_dep_map_output_dir(golden_repos_dir)

        assert expected.is_dir(), (
            "_ensure_dep_map_output_dir must create cidx-meta/dependency-map/ "
            "eagerly, even when no analysis has ever run"
        )
        assert result == expected

    def test_idempotent_when_directory_already_exists(self, tmp_path):
        """Calling the helper twice (e.g. across restarts) must not raise and
        must not disturb existing contents (parents=True, exist_ok=True)."""
        from code_indexer.server.startup.lifespan import _ensure_dep_map_output_dir

        golden_repos_dir = tmp_path / "golden-repos"
        dep_map_dir = golden_repos_dir / "cidx-meta" / "dependency-map"
        dep_map_dir.mkdir(parents=True)
        sentinel_file = dep_map_dir / "_domains.json"
        sentinel_file.write_text("{}", encoding="utf-8")

        _ensure_dep_map_output_dir(golden_repos_dir)
        _ensure_dep_map_output_dir(golden_repos_dir)  # second call must be a no-op

        assert sentinel_file.exists(), "existing content must survive re-creation"
        assert sentinel_file.read_text(encoding="utf-8") == "{}"

    def test_accepts_str_path_argument(self, tmp_path):
        """golden_repos_dir in lifespan.py is a Path, but the helper should also
        tolerate a plain str (defensive: matches the loose typing used
        throughout the surrounding startup code)."""
        from code_indexer.server.startup.lifespan import _ensure_dep_map_output_dir

        golden_repos_dir_str = str(tmp_path / "golden-repos")
        expected = Path(golden_repos_dir_str) / "cidx-meta" / "dependency-map"

        _ensure_dep_map_output_dir(golden_repos_dir_str)

        assert expected.is_dir()


class TestLifespanWiresEagerCreationBeforeStaleSentinelCheck:
    """Source-order guard: startup must create the dir BEFORE consulting
    `_dep_map_dir.exists()` for stale-sentinel cleanup, so that check always
    sees a real (if empty) directory rather than skipping entirely on a
    never-before-visited node."""

    def test_ensure_helper_called_in_make_lifespan(self):
        from code_indexer.server.startup.lifespan import make_lifespan

        source = inspect.getsource(make_lifespan)
        assert "_ensure_dep_map_output_dir(" in source, (
            "make_lifespan must call _ensure_dep_map_output_dir(...) to eagerly "
            "create the dependency-map output directory at startup (Bug #1621)"
        )

    def test_ensure_helper_called_before_stale_sentinel_exists_check(self):
        from code_indexer.server.startup.lifespan import make_lifespan

        source = inspect.getsource(make_lifespan)
        ensure_pos = source.find("_ensure_dep_map_output_dir(")
        stale_check_pos = source.find("if _dep_map_dir.exists():")

        assert ensure_pos != -1, "_ensure_dep_map_output_dir(...) call not found"
        assert stale_check_pos != -1, "stale-sentinel .exists() check not found"
        assert ensure_pos < stale_check_pos, (
            "_ensure_dep_map_output_dir(...) must run BEFORE the stale-sentinel "
            "'.exists()' check -- otherwise a never-before-visited node still "
            "skips eager creation and the ordering bug (#1621) persists"
        )


def _patch_dep_map_output_dir_collaborators(monkeypatch, golden_repos_dir: Path):
    """Wire dependency_map_routes._get_dep_map_output_dir() to resolve against
    an isolated tmp_path golden-repos tree, for both the primary (dep_map
    service) and fallback (config service) resolution branches -- so the test
    never touches this developer machine's real, already-provisioned
    cidx-server state.

    This is a UNIT-level test double (a plain stub, not a mock of the code
    path under test): only the two collaborator lookups that would otherwise
    reach into live global server state (app.state / the on-disk server
    config) are stubbed. `_get_dep_map_output_dir()` and
    `_get_dashboard_cache_backend()` themselves -- the functions under test --
    run unmodified, and directory existence is checked against the real
    filesystem via tmp_path.
    """
    from code_indexer.server.web import dependency_map_routes
    import code_indexer.server.services.config_service as config_service_module

    class _FakeDepMapService:
        def get_sentinel_dir(self) -> Path:
            return golden_repos_dir / "cidx-meta" / "dependency-map"

    class _FakeConfigManager:
        # Deliberately a DIFFERENT tree than golden_repos_dir above: the
        # fallback branch is only reached when the primary (service) branch's
        # directory does not exist yet, and must independently also report
        # "not found" in that case -- proving the "before" state is a genuine
        # infrastructure-unavailable condition, not an artifact of the fake.
        server_dir = golden_repos_dir.parent / "unrelated-server-dir"

    class _FakeConfigService:
        config_manager = _FakeConfigManager()

    monkeypatch.setattr(
        dependency_map_routes,
        "_get_dep_map_service_from_state",
        lambda: _FakeDepMapService(),
    )
    monkeypatch.setattr(
        config_service_module,
        "get_config_service",
        lambda: _FakeConfigService(),
    )


class TestBug1621ReproductionThroughRealRoutePath:
    """Unit-level reproduction of the #1621 symptom using the REAL, unmodified
    `_get_dep_map_output_dir()` / `_get_dashboard_cache_backend()` functions
    from dependency_map_routes.py against a real tmp_path filesystem tree --
    only the dep-map-service/config-service collaborator lookups are stubbed
    (Messi Rule #1: the functions under test are never mocked)."""

    def test_cache_backend_is_none_before_directory_exists(self, tmp_path, monkeypatch):
        """Reproduces the bug precondition: on a never-before-visited node
        (directory not yet on disk), the dashboard cache backend -- and thus
        job submission -- is unavailable."""
        from code_indexer.server.web import dependency_map_routes

        golden_repos_dir = tmp_path / "golden-repos"
        _patch_dep_map_output_dir_collaborators(monkeypatch, golden_repos_dir)

        assert not (golden_repos_dir / "cidx-meta" / "dependency-map").exists()
        assert dependency_map_routes._get_dep_map_output_dir() is None
        assert dependency_map_routes._get_dashboard_cache_backend() is None

    def test_cache_backend_available_immediately_after_eager_creation(
        self, tmp_path, monkeypatch
    ):
        """The fix: once startup has eagerly created the directory (simulated
        here by calling the real _ensure_dep_map_output_dir helper, exactly as
        make_lifespan now does), the very first request sees a usable
        directory -- no more "infrastructure unavailable" on first load."""
        from code_indexer.server.web import dependency_map_routes
        from code_indexer.server.startup.lifespan import _ensure_dep_map_output_dir

        golden_repos_dir = tmp_path / "golden-repos"
        _patch_dep_map_output_dir_collaborators(monkeypatch, golden_repos_dir)

        # Precondition unchanged from the bug scenario: nothing exists yet.
        assert dependency_map_routes._get_dep_map_output_dir() is None

        # The fix: startup eagerly creates the directory.
        _ensure_dep_map_output_dir(golden_repos_dir)

        resolved = dependency_map_routes._get_dep_map_output_dir()
        assert resolved is not None, (
            "After eager creation at startup, _get_dep_map_output_dir() must "
            "resolve on the very first request -- this is the #1621 fix"
        )
        assert resolved == golden_repos_dir / "cidx-meta" / "dependency-map"

        cache_backend = dependency_map_routes._get_dashboard_cache_backend()
        assert cache_backend is not None, (
            "cache_backend must be available on the first request once the "
            "directory has been eagerly created -- reproduces the exact "
            "'_submit_dashboard_job: required dependency unavailable "
            "(cache_backend=False)' warning being eliminated"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
