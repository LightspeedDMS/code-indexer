"""Bug #1390: RefreshScheduler must receive golden_repos_metadata backend.

Bug: In lifespan.py, GlobalReposLifecycleManager(...) is constructed WITHOUT a
golden_repo_metadata_backend argument. RefreshScheduler's filesystem
reconciliation (_reconcile_registry_with_filesystem) needs to update BOTH
global_repos (via the existing `registry`) AND golden_repos_metadata (a
structurally separate table, bare-alias-keyed) -- without this wiring,
RefreshScheduler has no way to reach golden_repos_metadata in cluster/postgres
mode at all (it would silently construct a per-node SQLite fallback instead
of the shared PostgreSQL backend).

Fix: In lifespan.py where GlobalReposLifecycleManager(...) is constructed,
pass:
    golden_repo_metadata_backend=(
        backend_registry.golden_repo_metadata if backend_registry is not None else None
    )

Mirrors the exact pattern already proven for DescriptionRefreshScheduler's
golden_backend= wiring (test_lifespan_golden_backend_wiring_bug.py).
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)

_LIFECYCLE_CTOR = "global_lifecycle_manager = GlobalReposLifecycleManager("
_GOLDEN_BACKEND_ARG = "golden_repo_metadata_backend="
_REGISTRY_ATTR = "backend_registry.golden_repo_metadata"


def _source() -> str:
    return _LIFESPAN_PATH.read_text()


class TestLifespanGoldenRepoMetadataWiringSource:
    def test_golden_repo_metadata_backend_arg_passed_to_lifecycle_manager(self):
        """lifespan.py must pass golden_repo_metadata_backend= to
        GlobalReposLifecycleManager. Without it, RefreshScheduler cannot
        reach golden_repos_metadata in cluster mode."""
        source = _source()

        ctor_pos = source.find(_LIFECYCLE_CTOR)
        assert ctor_pos != -1, (
            f"GlobalReposLifecycleManager constructor not found in lifespan.py: "
            f"{_LIFECYCLE_CTOR!r}"
        )

        ctor_block = source[ctor_pos : ctor_pos + 2000]
        assert _GOLDEN_BACKEND_ARG in ctor_block, (
            f"Bug #1390: GlobalReposLifecycleManager construction in lifespan.py "
            f"does not pass {_GOLDEN_BACKEND_ARG!r}.\n"
            "Without this, RefreshScheduler's filesystem reconciliation cannot "
            "update golden_repos_metadata in cluster/postgres mode.\n"
            "Fix: add golden_repo_metadata_backend=(backend_registry.golden_repo_metadata "
            "if backend_registry is not None else None) to the "
            "GlobalReposLifecycleManager call."
        )

    def test_golden_repo_metadata_backend_references_backend_registry_attr(self):
        """The golden_repo_metadata_backend= arg must reference
        backend_registry.golden_repo_metadata (the same shared attribute
        DescriptionRefreshScheduler and GoldenRepoManager already use)."""
        source = _source()

        ctor_pos = source.find(_LIFECYCLE_CTOR)
        assert ctor_pos != -1, (
            f"GlobalReposLifecycleManager constructor not found: {_LIFECYCLE_CTOR!r}"
        )

        registry_attr_pos = source.find(_REGISTRY_ATTR, ctor_pos)
        assert registry_attr_pos != -1, (
            f"{_REGISTRY_ATTR!r} must appear AFTER the GlobalReposLifecycleManager "
            f"constructor call (pos {ctor_pos})."
        )
        # Must be reasonably close to the ctor call (same statement/block),
        # not just anywhere later in the huge lifespan() closure.
        assert registry_attr_pos - ctor_pos < 2000, (
            f"{_REGISTRY_ATTR!r} found too far (>{2000} chars) after the "
            "GlobalReposLifecycleManager constructor -- must be part of "
            "this specific wiring, not an unrelated later use."
        )

    def test_golden_repo_metadata_backend_wiring_is_guarded_for_solo_mode(self):
        """Must fall back to None when backend_registry is None (solo mode) so
        RefreshScheduler falls back to its own SQLite resolution, unchanged."""
        source = _source()

        ctor_pos = source.find(_LIFECYCLE_CTOR)
        assert ctor_pos != -1, (
            f"GlobalReposLifecycleManager constructor not found: {_LIFECYCLE_CTOR!r}"
        )

        ctor_region = source[ctor_pos : ctor_pos + 2000]
        has_none_guard = (
            "backend_registry is not None" in ctor_region
            or "if backend_registry" in ctor_region
        )
        assert has_none_guard, (
            "Bug #1390: golden_repo_metadata_backend wiring near "
            "GlobalReposLifecycleManager does not guard against "
            "backend_registry being None (solo/SQLite mode).\n"
            "Use: golden_repo_metadata_backend=(backend_registry.golden_repo_metadata "
            "if backend_registry is not None else None)"
        )
