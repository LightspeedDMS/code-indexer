"""Story #1083: lifespan must close the pooled production httpx client on shutdown.

HttpClientFactory owns ONE long-lived keep-alive sync client on the production
path (fault injection OFF). It is built lazily on the first query and reused
across all queries. To avoid leaking the SSLContext + connection pool across
lifespan cycles, the lifespan shutdown block MUST call
``close_pooled_clients()`` on ``app.state.http_client_factory`` AFTER the yield.

Source-text + source-order guard, mirroring the query_executor wiring guard.
All tests MUST fail before the fix and pass after.
"""

from __future__ import annotations

from pathlib import Path

# tests/unit/server/startup/ -> tests/unit/server/ -> tests/unit/ -> tests/ -> repo root
_PARENTS_TO_REPO_ROOT = 4

_REPO_ROOT = Path(__file__).resolve().parents[_PARENTS_TO_REPO_ROOT]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)

_CLOSE_CALL = "close_pooled_clients()"


def _source() -> str:
    return _LIFESPAN_PATH.read_text()


def test_close_pooled_clients_call_present() -> None:
    source = _source()
    assert _CLOSE_CALL in source, (
        "lifespan.py must call close_pooled_clients() on the http client factory "
        "to release the pooled production httpx client on shutdown."
    )


def test_close_pooled_clients_after_yield() -> None:
    source = _source()
    close_idx = source.find(_CLOSE_CALL)
    yield_idx = source.find("yield  # Server is now running")

    assert close_idx != -1, "close_pooled_clients() call missing"
    assert yield_idx != -1, "lifespan yield marker missing"
    assert close_idx > yield_idx, (
        "Pooled client must be closed AFTER the lifespan yield (shutdown phase)."
    )
