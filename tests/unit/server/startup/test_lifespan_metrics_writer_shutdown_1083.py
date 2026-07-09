"""Story #1083: lifespan must drain the batched metrics writer on shutdown.

The ApiMetricsService background writer batches drained metric events into one
transaction per drain. On shutdown the lifespan must call ``stop_writer()`` so any
still-queued events are flushed (no counts lost) and the writer thread joins.

Source-text + source-order guard, mirroring the pooled-client shutdown guard.
"""

from __future__ import annotations

from pathlib import Path

_PARENTS_TO_REPO_ROOT = 4
_REPO_ROOT = Path(__file__).resolve().parents[_PARENTS_TO_REPO_ROOT]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)

_STOP_CALL = "stop_writer()"


def _source() -> str:
    return _LIFESPAN_PATH.read_text()


def test_stop_writer_call_present() -> None:
    source = _source()
    assert _STOP_CALL in source, (
        "lifespan.py must call api_metrics_service.stop_writer() to drain the "
        "batched metrics writer on shutdown."
    )


def test_stop_writer_after_yield() -> None:
    source = _source()
    stop_idx = source.find(_STOP_CALL)
    yield_idx = source.find("yield  # Server is now running")

    assert stop_idx != -1, "stop_writer() call missing"
    assert yield_idx != -1, "lifespan yield marker missing"
    assert stop_idx > yield_idx, (
        "Metrics writer must be drained AFTER the lifespan yield (shutdown phase)."
    )
