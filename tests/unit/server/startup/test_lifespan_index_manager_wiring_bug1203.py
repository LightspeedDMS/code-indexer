"""Bug #1203 regression guard: lifespan must wire index_manager into ActivatedRepoManager.

Bug #1203: When a golden repo is activated on a non-default branch (or switched
via switch_branch, or synced via sync_with_golden_repository), the semantic
embedding index was never updated for files that differ from the default branch.

The fix adds ActivatedRepoManager._run_branch_delta_index, which is a no-op
when self._index_manager is None.  This test suite guards that lifespan.py
actually injects a non-None ActivatedRepoIndexManager into the production ARM
so the fix is not silently dormant in every deployment.

This test file:
1. Source-text guard: verifies the wiring assignment is present in lifespan.py.
2. Source-order guard: the assignment comes AFTER arm._clone_backend (Bug #1044).
"""

from __future__ import annotations

from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


class TestLifespanIndexManagerWiringSourceGuard:
    """Source-text guard: lifespan.py must contain arm._index_manager assignment."""

    def test_arm_index_manager_assignment_present_in_lifespan_source(self):
        """lifespan.py must assign arm._index_manager = <ActivatedRepoIndexManager>.

        Bug #1203: without this assignment, _run_branch_delta_index is always a
        no-op (self._index_manager is None) and the branch-delta reindex never
        runs in production — making the fix inert on every deployed node.

        The exact attribute name is _index_manager; the assignment must set it
        on the arm (ActivatedRepoManager) instance retrieved from
        golden_repo_manager.activated_repo_manager.
        """
        source = _LIFESPAN_PATH.read_text()

        has_arm_index_manager_write = "arm._index_manager =" in source
        assert has_arm_index_manager_write, (
            "Bug #1203: lifespan.py does not wire an ActivatedRepoIndexManager "
            "into the ActivatedRepoManager (arm._index_manager). "
            "Without this, _run_branch_delta_index is always a no-op and "
            "non-default-branch activations never reindex. "
            "Add inside the 'if arm is not None:' block (after arm._clone_backend): "
            "    from code_indexer.server.services.activated_repo_index_manager "
            "    import ActivatedRepoIndexManager\n"
            "    arm._index_manager = ActivatedRepoIndexManager("
            "activated_repo_manager=arm, "
            "background_job_manager=background_job_manager)"
        )

    def test_arm_index_manager_wired_inside_golden_repo_manager_not_none_block(self):
        """The arm._index_manager assignment must live inside 'if golden_repo_manager is not None:'.

        Ensures we do not attempt to read .activated_repo_manager from None.
        """
        source = _LIFESPAN_PATH.read_text()

        block_start = source.find("if golden_repo_manager is not None:")
        assert block_start != -1, (
            "'if golden_repo_manager is not None:' not found in lifespan.py"
        )

        assignment_pos = source.find("arm._index_manager =", block_start)
        assert assignment_pos != -1, (
            "Bug #1203: arm._index_manager assignment not found AFTER "
            "'if golden_repo_manager is not None:' in lifespan.py."
        )


class TestLifespanIndexManagerWiringSourceOrder:
    """Source-order guard: arm._index_manager must come AFTER arm._clone_backend.

    Ordering matters: arm is already retrieved from golden_repo_manager by the
    Bug #1044 clone_backend wiring. The index_manager wiring reuses that same
    'arm' local, so it must appear after arm._clone_backend is set.
    """

    def test_index_manager_wiring_appears_after_clone_backend_wiring(self):
        """arm._index_manager must come after arm._clone_backend in lifespan.py."""
        source = _LIFESPAN_PATH.read_text()

        clone_backend_pos = source.find("arm._clone_backend =")
        index_manager_pos = source.find("arm._index_manager =")

        assert clone_backend_pos != -1, (
            "arm._clone_backend not found in lifespan.py — "
            "Bug #1044 fix may have been removed."
        )
        assert index_manager_pos != -1, (
            "Bug #1203: arm._index_manager not found in lifespan.py."
        )
        assert clone_backend_pos < index_manager_pos, (
            "Source-order violation: arm._index_manager appears BEFORE "
            "arm._clone_backend in lifespan.py. "
            f"clone_backend_pos={clone_backend_pos}, "
            f"index_manager_pos={index_manager_pos}"
        )

    def test_index_manager_wired_with_activated_repo_manager_argument(self):
        """The ActivatedRepoIndexManager must be constructed with activated_repo_manager=arm.

        Without passing arm explicitly, the constructor creates a second
        ActivatedRepoManager instance (circular-construction hazard, Bug #1203).
        The source must show 'activated_repo_manager=arm' at the call site.
        """
        source = _LIFESPAN_PATH.read_text()

        has_explicit_arm_arg = "activated_repo_manager=arm" in source
        assert has_explicit_arm_arg, (
            "Bug #1203: ActivatedRepoIndexManager in lifespan.py is not "
            "constructed with activated_repo_manager=arm. Without this, the "
            "constructor creates a second ActivatedRepoManager (circular-"
            "construction tangle). "
            "Fix: ActivatedRepoIndexManager(activated_repo_manager=arm, ...)"
        )
