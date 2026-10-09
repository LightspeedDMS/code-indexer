"""Fake Langfuse SDK for export-boundary tests (imported, never collected).

The Langfuse SDK is the external boundary: everything the server hands it
leaves the process. FakeLangfuseSdk is injected at the real seam -- the
``_langfuse`` attribute LangfuseClient's lazy initialization checks -- so the
real LangfuseClient, TraceStateManager and AutoSpanLogger run unchanged and
every SDK call is recorded exactly as it would be sent.

Like the real SDK (langfuse 3.x ``_process_media_and_apply_mask``), the fake
applies the client's mask hook to input, output and metadata -- and to
nothing else. That the client really passes its mask hook to the SDK is
proven against the real SDK in tests/unit/server/telemetry/
test_span_export_redaction.py.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Tuple

from code_indexer.server.services.langfuse_client import (
    LangfuseClient,
    _redact_sdk_data,
)
from code_indexer.server.services.trace_state_manager import TraceStateManager
from code_indexer.server.utils.config_manager import LangfuseConfig

_TRACE_ID = "example-trace-id"
_MASKED_FIELDS = ("input", "output", "metadata")

Sent = List[Tuple[str, Dict[str, Any]]]
Mask = Callable[..., Any]


def _masked(mask: Mask, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: mask(data=value) if key in _MASKED_FIELDS else value
        for key, value in kwargs.items()
    }


class FakeSdkSpan:
    """An SDK span: a context manager that can be updated and ended."""

    def __init__(self, sent: Sent, mask: Mask) -> None:
        self._sent = sent
        self._mask = mask
        self.trace_id = _TRACE_ID

    def __enter__(self) -> "FakeSdkSpan":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def update(self, **kwargs: Any) -> None:
        self._sent.append(("span.update", _masked(self._mask, kwargs)))

    def end(self, **kwargs: Any) -> None:
        self._sent.append(("span.end", kwargs))


class FakeLangfuseSdk:
    """Records every call the server makes to the Langfuse SDK."""

    def __init__(self, mask: Mask) -> None:
        self.sent: Sent = []
        self._mask = mask

    def start_as_current_span(self, **kwargs: Any) -> FakeSdkSpan:
        self.sent.append(("start_as_current_span", _masked(self._mask, kwargs)))
        return FakeSdkSpan(self.sent, self._mask)

    def update_current_trace(self, **kwargs: Any) -> None:
        self.sent.append(("update_current_trace", _masked(self._mask, kwargs)))

    def start_span(self, **kwargs: Any) -> FakeSdkSpan:
        self.sent.append(("start_span", _masked(self._mask, kwargs)))
        return FakeSdkSpan(self.sent, self._mask)

    def create_score(self, **kwargs: Any) -> object:
        self.sent.append(("create_score", kwargs))
        return object()

    def flush(self) -> None:
        self.sent.append(("flush", {}))

    def sent_text(self) -> str:
        return json.dumps(self.sent, default=str)


def traced_client() -> Tuple[LangfuseClient, TraceStateManager, FakeLangfuseSdk]:
    """A real, enabled LangfuseClient whose SDK is the fake (with the
    client's mask hook), and a real TraceStateManager over it."""
    sdk = FakeLangfuseSdk(mask=_redact_sdk_data)
    client = LangfuseClient(
        LangfuseConfig(enabled=True, public_key="example", secret_key="example")
    )
    client._langfuse = sdk
    return client, TraceStateManager(client), sdk
