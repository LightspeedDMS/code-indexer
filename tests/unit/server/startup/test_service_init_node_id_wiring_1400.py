"""Story #1400 CRITICAL 3: service_init.py must thread the resolved node_id
into both JobTracker and BackgroundJobManager construction.

service_init.py already resolves the real cluster node_id (from
config.json's `cluster.node_id`, defaulting to "local") for
DependencyLatencyTracker at construction time. JobTracker and
BackgroundJobManager were constructed without it, which meant
cleanup_orphaned_jobs_on_startup() always ran with node_id=None -- a
safe no-op on PostgreSQL (Bug #535 protection) but NOT the node-scoped
cleanup CRITICAL 3 requires; and no job was ever stamped with
executing_node at registration time.

Why a source-order test (not a full initialize_services() invocation):
initialize_services() creates DB schemas, spawns background threads,
connects to PostgreSQL, and performs bootstrap git operations -- exercising
it directly would require mocking dozens of external boundaries (the same
rationale test_lifespan_hnsw_fts_worker_budget_1166.py documents for the
same function). A source-inspection test is the narrowest reliable check
for this specific wiring invariant.
"""

from __future__ import annotations

from pathlib import Path

_PARENTS_TO_REPO_ROOT = 4
_REPO_ROOT = Path(__file__).resolve().parents[_PARENTS_TO_REPO_ROOT]
_SERVICE_INIT_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "service_init.py"
)


class TestServiceInitNodeIdWiring:
    def _source(self) -> str:
        return _SERVICE_INIT_PATH.read_text()

    def test_node_id_resolved_before_job_tracker_construction(self) -> None:
        source = self._source()
        node_id_pos = source.find('_node_id = (\n        _cluster_cfg.get("node_id"')
        job_tracker_pos = source.find("job_tracker = _JobTracker(")

        assert node_id_pos != -1, (
            "_node_id resolution block not found in service_init.py -- "
            "expected the existing cluster.node_id read used by "
            "DependencyLatencyTracker."
        )
        assert job_tracker_pos != -1, (
            "job_tracker = _JobTracker( construction not found in service_init.py."
        )
        assert node_id_pos < job_tracker_pos, (
            "SOURCE-ORDER VIOLATION: _node_id must be resolved BEFORE "
            "JobTracker construction so it can be threaded through."
        )

    def test_job_tracker_receives_node_id_kwarg(self) -> None:
        source = self._source()
        start = source.find("job_tracker = _JobTracker(")
        assert start != -1, "job_tracker = _JobTracker( not found"
        # The call block contains a nested parenthesized expression
        # (`_backend_registry.background_jobs if _backend_registry else
        # None`), so a naive first-')' search truncates before reaching
        # trailing kwargs. The call's own closing paren is the one at the
        # call's original 4-space indentation, i.e. a line containing only
        # ")".
        end = source.find("\n    )", start)
        call_block = source[start:end]
        assert "node_id=_node_id" in call_block, (
            "Story #1400 CRITICAL 3: JobTracker(...) construction must pass "
            "node_id=_node_id so cleanup_orphaned_jobs_on_startup() runs "
            "node-scoped instead of the unconditional-no-op-on-PG default. "
            f"Call block was: {call_block!r}"
        )

    def test_background_job_manager_receives_node_id_kwarg(self) -> None:
        source = self._source()
        start = source.find("background_job_manager = BackgroundJobManager(")
        assert start != -1, "background_job_manager = BackgroundJobManager( not found"
        end = source.find("\n    )", start)
        call_block = source[start:end]
        assert "node_id=_node_id" in call_block, (
            "Story #1400 CRITICAL 3: BackgroundJobManager(...) construction "
            "must pass node_id=_node_id so fail_orphaned_jobs() can detect "
            "and skip the unscoped sweep against a PostgreSQL backend. "
            f"Call block was: {call_block!r}"
        )
