"""Bug #1575 round 6, item 2 (Codex + opus dual review of round 5's diff):
``_do_reconcile_with_database``'s hoisted ``begin_indexing()`` call (Fix 4,
moved before the non-git modified-file delete loop so those deletes land
INSIDE the session) sits OUTSIDE the try/finally block whose ``finally``
calls ``end_indexing()``. Any exception raised between
``begin_indexing()`` and that try (i.e. inside the modified-file delete
loop itself -- ``delete_by_filter()`` or the ``progress_callback`` call)
therefore leaks the indexing session forever: ``end_indexing()`` never
runs, so ``self._indexing_session_changes[collection_name]`` and this
collection's cached ``PathIndex``/``id_index`` entries stay open,
permanently disabling out-of-session persistence (Gap D/B) for that
collection until process restart.

Reproduction requires a genuinely NON-git repository: the modified-file
delete loop this bug targets is gated on ``not self.is_git_aware()``, so a
git repo (even with an uncommitted dirty file) never reaches it at all --
confirmed empirically before writing this test.

Reuses the real ``FakeVectorStoreClient``/indexer-construction helpers
from ``test_reconcile_batch_content_id_1505.py`` (an in-memory,
controllable substitute for the storage backend -- the #2 preference in
the mocking hierarchy, not a mock of the code under test), overriding
``delete_by_filter()`` to raise -- a genuine mid-section failure -- and
counting ``end_indexing()`` calls.
"""

from __future__ import annotations

from typing import Any, Dict

from tests.unit.services.test_reconcile_batch_content_id_1505 import (
    FakeVectorStoreClient,
    _make_indexer,
    _write_file,
)


class _RaisingDeleteByFilterClient(FakeVectorStoreClient):
    """A FakeVectorStoreClient whose delete_by_filter() raises, simulating
    a genuine failure inside the modified-file delete loop -- the exact
    section between begin_indexing() and the pre-fix try block."""

    def __init__(self) -> None:
        super().__init__()
        self.end_indexing_call_count = 0

    def delete_by_filter(
        self, collection_name: str, filter_conditions: Dict[str, Any]
    ) -> bool:
        raise RuntimeError("simulated failure inside modified-file delete loop")

    def end_indexing(self, collection_name, progress_callback=None):
        self.end_indexing_call_count += 1
        return super().end_indexing(collection_name, progress_callback)


def test_mid_section_exception_still_calls_end_indexing(tmp_path):
    """A genuine exception raised inside the non-git modified-file delete
    loop (between begin_indexing() and the BranchAwareIndexer try/finally)
    must still result in end_indexing() being called before the exception
    propagates -- otherwise the indexing session leaks forever."""
    root = tmp_path / "repo"
    root.mkdir()
    # Deliberately NOT a git repo -- the target loop is gated on
    # `not self.is_git_aware()`.
    _write_file(root, "a.py", "print('a')\n")

    vector_store = _RaisingDeleteByFilterClient()
    file_stat = (root / "a.py").stat()
    # A working_dir DB point whose content-id FORMAT never matches the
    # disk-side computation (":working_dir:M:S" vs ":working_dir_M_S"),
    # so this file is reliably classified as "modified" and enters the
    # delete loop regardless of actual mtime/size values.
    vector_store.add_working_dir_point("a.py", file_stat.st_mtime, file_stat.st_size)

    indexer = _make_indexer(root, vector_store)
    assert indexer.is_git_aware() is False

    raised = None
    try:
        indexer._do_reconcile_with_database(
            batch_size=50,
            progress_callback=None,
            git_status={"git_available": False},
            provider_name="test-provider",
            model_name="test-model",
            quiet=True,
            vector_thread_count=2,
        )
    except RuntimeError as exc:
        raised = exc

    assert raised is not None, (
        "expected the simulated delete_by_filter() failure to propagate"
    )
    assert vector_store.end_indexing_call_count >= 1, (
        "end_indexing() was NEVER called after the mid-section exception -- "
        "this is the round-6 session leak: begin_indexing() opened a "
        "session that nothing ever closed, permanently disabling "
        "out-of-session path_index.bin persistence for this collection "
        "until process restart"
    )
