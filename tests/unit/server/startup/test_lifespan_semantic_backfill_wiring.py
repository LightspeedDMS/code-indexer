"""Regression guard: lifespan must schedule the semantic self-heal backfill AND cancel
it on shutdown (mirrors the sibling SCIP/registry reconcile wiring guards).

The semantic backfill task is created unconditionally (the enabled cost-gate is checked
inside the loop), so it MUST be cancelled on shutdown like its siblings — otherwise it
keeps running past lifespan teardown ("Task was destroyed but it is pending"). Guard the
wiring at the source level, the same lightweight approach used for the SCIP reconcile.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


class TestLifespanSemanticBackfillWiringSourceGuard:
    def test_semantic_backfill_scheduled_in_lifespan_source(self):
        source = _LIFESPAN_PATH.read_text()
        assert (
            "asyncio.create_task(" in source and "_run_semantic_backfill()" in source
        ), (
            "the semantic backfill must be scheduled as a background task "
            "(asyncio.create_task(_run_semantic_backfill()))."
        )
        assert "app.state.semantic_backfill_task" in source, (
            "the semantic backfill task must be kept on app.state so it survives GC "
            "and can be cancelled on shutdown."
        )

    def test_semantic_backfill_is_periodic_and_leader_gated(self):
        source = _LIFESPAN_PATH.read_text()
        marker = "async def _run_semantic_backfill"
        start = source.find(marker)
        assert start != -1, "the _run_semantic_backfill helper is missing"
        body = source[start : source.find("app.state.semantic_backfill_task", start)]
        assert "while True:" in body and "asyncio.sleep" in body, (
            "the semantic backfill helper must loop (while True + asyncio.sleep)."
        )
        assert "run_semantic_backfill_cycle" in body, (
            "the helper must drive run_semantic_backfill_cycle."
        )
        # Leader-gated like the SCIP reconcile (one pod enqueues) + inert when disabled.
        assert "is_leader" in body, "the semantic backfill must be leader-gated."
        assert "enabled" in body, (
            "the semantic backfill must honor the enabled cost-gate before enqueuing."
        )

    def test_semantic_backfill_cancelled_on_shutdown(self):
        source = _LIFESPAN_PATH.read_text()
        assert 'getattr(app.state, "semantic_backfill_task"' in source, (
            "the semantic backfill task must be cancelled on shutdown "
            "(getattr(app.state, 'semantic_backfill_task', None) -> cancel()) — it is "
            "created unconditionally so it must be torn down unconditionally too."
        )
