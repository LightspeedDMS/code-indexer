"""Lifespan wiring guard for the unified audit capture binding.

The ONE lifespan-owned AuditLogService must be bound for capture right after
it is started and published on app.state (startup), and unbound only after
its writer has drained (shutdown).  Source-order guard, mirroring
test_lifespan_audit_service_wiring_1241.py.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


def _source() -> str:
    return _LIFESPAN_PATH.read_text()


def test_bind_follows_start_and_publication_before_yield() -> None:
    source = _source()
    start = source.find("audit_service.start()")
    publish = source.find("app.state.audit_service = audit_service")
    bind = source.find("bind_audit_service(\n")
    yield_pos = source.find("yield  # Server is now running")
    assert -1 not in (start, publish, bind, yield_pos)
    assert start < publish < bind < yield_pos


def test_bind_passes_the_started_service_and_node_id() -> None:
    source = _source()
    bind = source.find("bind_audit_service(\n")
    call = source[bind : source.find(")", bind)]
    assert "audit_service" in call
    assert "node_id=" in call


def test_clear_follows_the_writer_drain_after_yield() -> None:
    source = _source()
    yield_pos = source.find("yield  # Server is now running")
    stop = source.find("_audit_svc.stop()")
    clear = source.find("clear_audit_service()")
    assert -1 not in (yield_pos, stop, clear)
    assert yield_pos < stop < clear
