"""Bug #1313 regression guard: lifespan wires the temporal metadata PG backend.

Root cause: TemporalMetadataStore (Story #669) was a SQLite-WAL database
that, in cluster mode, lives on the shared NFS golden-repos mount, serializing
all 8 indexing threads on NFS fsync. The fix routes cluster/postgres mode
through TemporalMetadataPostgresBackend via the process-level registry
(temporal_metadata_backend_registry.py), mirroring the coalescer registry
wiring pattern (Story #1079 Phase E).

These are source-text + source-order guards (mirroring
test_lifespan_coalescer_registry_wiring.py). They MUST fail before the #1313
lifespan wiring and pass after.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


class TestLifespanTemporalMetadataBackendWiring:
    def test_set_factory_present_in_startup(self):
        source = _LIFESPAN_PATH.read_text()
        assert "set_temporal_metadata_backend_factory" in source, (
            "lifespan.py must install the temporal metadata backend factory via "
            "set_temporal_metadata_backend_factory(...) in postgres mode"
        )

    def test_uses_postgres_backend_class(self):
        source = _LIFESPAN_PATH.read_text()
        assert "TemporalMetadataPostgresBackend" in source, (
            "lifespan.py must construct TemporalMetadataPostgresBackend as the "
            "factory target"
        )

    def test_clear_present_in_shutdown(self):
        source = _LIFESPAN_PATH.read_text()
        assert "clear_temporal_metadata_backend_factory" in source, (
            "lifespan.py must clear the temporal metadata backend factory on "
            "shutdown via clear_temporal_metadata_backend_factory()"
        )

    def test_set_before_yield_and_clear_after_yield(self):
        source = _LIFESPAN_PATH.read_text()
        yield_pos = source.find("yield  # Server is now running")
        set_pos = source.find("set_temporal_metadata_backend_factory")
        clear_pos = source.find("clear_temporal_metadata_backend_factory")

        assert yield_pos != -1, "could not locate the lifespan yield boundary"
        assert set_pos != -1 and clear_pos != -1
        assert set_pos < yield_pos, (
            "set_temporal_metadata_backend_factory must run during STARTUP "
            "(before the yield)"
        )
        assert clear_pos > yield_pos, (
            "clear_temporal_metadata_backend_factory must run during SHUTDOWN "
            "(after the yield)"
        )

    def _temporal_wiring_block(self) -> str:
        """Extract just the Bug #1313 temporal-metadata wiring block's source
        text (from its opening comment to the next unrelated startup section),
        so assertions below are scoped to this block and cannot accidentally
        match unrelated code elsewhere in this large file."""
        source = _LIFESPAN_PATH.read_text()
        start = source.find("# Bug #1313: route temporal collection metadata storage")
        end = source.find(
            "# Startup: Auto-seed API keys if server config is blank (Story #20)"
        )
        assert start != -1 and end != -1 and start < end, (
            "could not locate the Bug #1313 temporal metadata wiring block "
            "boundaries in lifespan.py"
        )
        return source[start:end]

    def test_wiring_is_still_non_fatal_for_server_startup(self):
        """A failure here must not crash the whole server -- mirrors the
        established non-fatal convention for every other postgres-mode
        wiring block in this file (coalescer registry, ConfigService pool).
        The block's own try/except must not propagate."""
        block = self._temporal_wiring_block()
        assert "try:" in block and "except Exception" in block, (
            "the temporal metadata wiring block must still be wrapped in "
            "try/except so an unexpected failure does not abort server "
            "startup"
        )

    def test_pool_unavailable_branch_fails_loud_not_silent_sqlite_fallback(self):
        """Bug #1313 round-2 rework (Codex Finding A): when no PostgreSQL
        connection pool is available, the block must install a poison
        factory (fail loud on use) instead of silently leaving the registry
        factory unset -- which would let TemporalMetadataStore's facade
        default fall back to the NFS-backed SQLite-WAL backend, silently
        reintroducing the exact bug this fix exists to solve."""
        block = self._temporal_wiring_block()

        assert "install_poison_temporal_metadata_backend_factory" in block, (
            "the temporal metadata wiring block must call "
            "install_poison_temporal_metadata_backend_factory on failure "
            "instead of silently leaving TemporalMetadataStore on the "
            "SQLite default in postgres mode"
        )
        # Must appear in BOTH failure paths: the no-pool branch and the
        # generic exception handler.
        assert block.count("install_poison_temporal_metadata_backend_factory") >= 2, (
            "install_poison_temporal_metadata_backend_factory must be "
            "called from BOTH the no-pool branch and the except-Exception "
            "branch of the temporal metadata wiring block"
        )

    def test_no_longer_documents_silent_sqlite_fallback_as_acceptable(self):
        """Regression guard for the exact wording Codex flagged: the block
        must no longer describe leaving the factory unset / falling back to
        SQLite as the intended failure behavior in postgres mode."""
        block = self._temporal_wiring_block()
        assert "falls back to the SQLite backend" not in block
        assert "will fall back to the SQLite backend" not in block

    def test_uses_shared_factory_function_not_inline_duplicate(self):
        """Bug #1313 round-3 (software-architect blueprint): lifespan.py must
        build its PostgreSQL factory via the SINGLE shared
        make_postgres_temporal_metadata_factory(pool) definition (also used
        by temporal_child_wiring.py for the child-process bootstrap path),
        not by re-deriving the sha256/collection_key formula inline. DRY;
        NO behavior change -- both paths must compute an identical
        collection_key for the same collection_path."""
        block = self._temporal_wiring_block()
        assert "make_postgres_temporal_metadata_factory" in block, (
            "lifespan.py must call make_postgres_temporal_metadata_factory(pool) "
            "instead of duplicating the inline factory-construction logic"
        )
