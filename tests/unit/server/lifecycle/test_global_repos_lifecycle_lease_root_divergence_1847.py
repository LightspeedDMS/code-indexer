"""Bug #1847 AC3: the snapshot-reader-lease root must have exactly ONE
derivation, not two independent ones that happen to agree today by
algebraic coincidence.

Today, ``GlobalReposLifecycleManager.__init__`` wires the CleanupManager's
(read-side / deleter) lease root as ``self.golden_repos_dir / "cidx-meta"``
-- a value computed purely from the constructor's ``golden_repos_dir``
argument. The write side (readers publishing leases, e.g.
``server/cache/__init__.py::_resolve_hnsw_lease_kwargs()``) instead computes
``get_cidx_meta_path(get_config_service().config_manager.server_dir)``.

These two derivations are algebraically identical ONLY because every real
caller happens to construct ``golden_repos_dir`` as
``Path(server_data_dir) / "data" / "golden-repos"`` from the SAME
``server_data_dir`` the config service resolves. Nothing in the code
enforces that relationship -- if a future caller ever constructs
``GlobalReposLifecycleManager`` with a ``golden_repos_dir`` that does not
match the live ``ConfigService``'s ``server_dir`` (e.g. a
``CIDX_DATA_DIR``-style override applied to one but not the other, or a
stale value threaded through a refactor), the deleter would silently check
leases in the WRONG directory while readers publish them in the right one
-- reintroducing Bug #1845's original defect invisibly.

This test constructs the REAL ``GlobalReposLifecycleManager`` (the actual
production wiring path startup/lifespan.py drives) with a real,
independently-configured ``ConfigService`` singleton (via the documented
``set_config_service``/``reset_config_service`` test seam -- no internal
helper is monkeypatched), and proves the resulting
``CleanupManager._lease_root`` tracks the canonical
``get_cidx_meta_path(server_dir)`` resolver -- the SAME single source of
truth the write side uses -- rather than an independent
``golden_repos_dir``-based computation that can silently drift from it.
"""

from pathlib import Path

from code_indexer.server.lifecycle.global_repos_lifecycle import (
    GlobalReposLifecycleManager,
)
from code_indexer.server.services.cidx_meta_backup import get_cidx_meta_path
from code_indexer.server.services.config_service import (
    ConfigService,
    reset_config_service,
    set_config_service,
)


def test_lease_root_tracks_canonical_resolver_not_golden_repos_dir(tmp_path):
    """The lifecycle manager's lease root must equal
    get_cidx_meta_path(<the live ConfigService's server_dir>) -- even when
    the golden_repos_dir passed to the constructor points somewhere else
    entirely. If it instead reflects golden_repos_dir, the two derivations
    are independent and can diverge silently -- exactly the AC3 defect.
    """
    divergent_server_dir = tmp_path / "divergent-server-dir"
    divergent_server_dir.mkdir()
    set_config_service(ConfigService(server_dir_path=str(divergent_server_dir)))
    try:
        unrelated_golden_repos_dir = tmp_path / "unrelated-golden-repos"

        lifecycle = GlobalReposLifecycleManager(
            golden_repos_dir=str(unrelated_golden_repos_dir)
        )

        canonical_lease_root = get_cidx_meta_path(Path(divergent_server_dir))

        assert lifecycle.cleanup_manager._lease_root == canonical_lease_root, (
            "CleanupManager's lease root must be derived from the single "
            "canonical get_cidx_meta_path(server_dir) resolver -- the same "
            "one the write side (readers) uses -- not independently from "
            "golden_repos_dir. Got "
            f"{lifecycle.cleanup_manager._lease_root!r}, expected "
            f"{canonical_lease_root!r}."
        )
    finally:
        reset_config_service()
