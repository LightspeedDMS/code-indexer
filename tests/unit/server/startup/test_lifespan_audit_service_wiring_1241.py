"""Issue #1241 P1.3 regression guard: lifespan wires audit_service start/stop.

AuditLogService.start() must be called in lifespan STARTUP (so log() enqueues
asynchronously rather than blocking request threads).  AuditLogService.stop()
must be called in lifespan SHUTDOWN (to drain the queue on graceful restart,
preventing audit-record loss).

Without the start() call, _writer_thread stays None, _enqueue_or_write_sync()
always takes the synchronous branch, and P1.3 is dead code in production.
Without the stop() call, queued audit rows are silently dropped on shutdown.

Source-text + source-order guards, mirroring:
- tests/unit/server/startup/test_lifespan_coalescer_registry_wiring.py
- tests/unit/server/startup/test_lifespan_clone_backend_wiring_bug1044.py
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


class TestLifespanAuditServiceWiring1241:
    def test_audit_service_start_present_in_startup(self) -> None:
        source = _LIFESPAN_PATH.read_text()
        assert "audit_service.start()" in source, (
            "lifespan.py must call audit_service.start() during startup "
            "(Issue #1241 P1.3) so log() enqueues async instead of blocking "
            "the request thread."
        )

    def test_audit_service_stop_present_in_shutdown(self) -> None:
        source = _LIFESPAN_PATH.read_text()
        assert "_audit_svc.stop()" in source or "audit_service.stop()" in source, (
            "lifespan.py must call audit_service.stop() (or _audit_svc.stop()) "
            "during shutdown (Issue #1241 P1.3) to drain the async queue and "
            "prevent audit-record loss on graceful restart."
        )

    def test_start_before_yield_and_stop_after_yield(self) -> None:
        source = _LIFESPAN_PATH.read_text()
        yield_pos = source.find("yield  # Server is now running")
        start_pos = source.find("audit_service.start()")
        # stop may be reached via _audit_svc.stop() or audit_service.stop()
        stop_pos_1 = source.find("_audit_svc.stop()")
        stop_pos_2 = source.find("audit_service.stop()")
        stop_pos = stop_pos_1 if stop_pos_1 != -1 else stop_pos_2

        assert yield_pos != -1, "could not locate the lifespan yield boundary"
        assert start_pos != -1, "audit_service.start() not found in lifespan.py"
        assert stop_pos != -1, (
            "_audit_svc.stop() / audit_service.stop() not found in lifespan.py"
        )
        assert start_pos < yield_pos, (
            "audit_service.start() must run during STARTUP (before the yield), "
            f"but it appears at position {start_pos} vs yield at {yield_pos}"
        )
        assert stop_pos > yield_pos, (
            "audit_service stop must run during SHUTDOWN (after the yield), "
            f"but it appears at position {stop_pos} vs yield at {yield_pos}"
        )
