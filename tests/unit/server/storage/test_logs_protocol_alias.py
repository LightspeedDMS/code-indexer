"""
Tests for Story #876 Phase C: LogsBackend Protocol must accept `alias` kwarg.

The LogsBackend Protocol defines the contract that both SQLite (standalone)
and PostgreSQL (cluster) backends must satisfy. To enable lifecycle-runner
ERROR rows to be tagged with a repo alias, the insert_log() signature must
carry an `alias: Optional[str] = None` keyword argument.

This test drives the Protocol change in
src/code_indexer/server/storage/protocols.py.

Note: protocols.py uses `from __future__ import annotations`, so
inspect.signature() returns string annotations. We resolve them with
typing.get_type_hints() before comparing.
"""

from __future__ import annotations

import inspect
from typing import Optional, Union, get_args, get_origin, get_type_hints


def test_logs_backend_protocol_insert_log_declares_alias_parameter() -> None:
    """LogsBackend.insert_log must declare `alias: Optional[str] = None`."""
    from code_indexer.server.storage.protocols import LogsBackend

    sig = inspect.signature(LogsBackend.insert_log)
    params = sig.parameters

    assert "alias" in params, (
        "LogsBackend.insert_log must declare `alias` to carry the repo tag "
        "from the lifecycle-runner (Story #876 Phase C). "
        f"Current parameters: {list(params.keys())}"
    )

    alias_param = params["alias"]

    # Default must be None so existing callers that omit alias still work.
    assert alias_param.default is None, (
        "`alias` must default to None for backward compatibility. "
        f"Got default={alias_param.default!r}"
    )

    # Resolve stringified annotations (protocols.py uses PEP 563 postponed
    # evaluation) before comparing against the expected type.
    hints = get_type_hints(LogsBackend.insert_log)
    assert "alias" in hints, (
        "Type hints for insert_log must include `alias`; none resolved."
    )

    resolved = hints["alias"]
    expected = Optional[str]
    assert resolved == expected or (
        get_origin(resolved) is Union and set(get_args(resolved)) == {str, type(None)}
    ), f"`alias` must be typed as Optional[str]; got {resolved!r}"
