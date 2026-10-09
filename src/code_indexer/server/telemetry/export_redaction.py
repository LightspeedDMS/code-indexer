"""Redaction at the telemetry exporter boundary.

Every span and log record the server exports passes through ONE redacting
exporter before it reaches the real exporter: each attribute value, each
span event's attributes (``exception.message``, ``exception.stacktrace``,
``exception.type``), the span status description and the log body are
redacted by ``redact_secret_fields``; URL-valued attributes are first
passed through the access-log query-value redaction. Whatever path put a
secret on a span or record, no credential value reaches a collector.

Request/client hooks (instrumentation.py) and the log bridge's record copy
(log_handler.py) still redact earlier, as defence in depth.

A span or record whose redaction fails is dropped (WARNING), never
exported unredacted.
"""

from __future__ import annotations

import copy
import dataclasses
import logging
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk._logs.export import LogRecordExporter, LogRecordExportResult
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Link, Status

from code_indexer.utils.credential_redaction import redact_secret_fields

logger = logging.getLogger(__name__)

# Attributes holding a URL or its query (http.* and url.* semantic names).
URL_ATTRIBUTES = ("http.url", "http.target", "url.full", "url.query")


def _redact_attribute(name: str, value: Any) -> Any:
    """``value`` redacted as the value of an attribute called ``name``."""
    if name in URL_ATTRIBUTES and isinstance(value, str):
        from code_indexer.server.utils.access_log_redaction import (
            redact_sensitive_query_values,
        )

        value = redact_sensitive_query_values(value)
    return redact_secret_fields({name: value})[name]


def redact_attributes(attributes: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """A redacted copy of an attribute mapping (empty for None)."""
    return {
        name: _redact_attribute(name, value)
        for name, value in (attributes or {}).items()
    }


def _redacted_span(span: ReadableSpan) -> ReadableSpan:
    """A concrete ReadableSpan (the SDK's read-only span class) copying
    ``span`` with its name, every attribute, every event's name and
    attributes, every link's attributes, the resource attributes and the
    status description redacted."""
    events = [
        Event(
            name=_redact_attribute("event.name", event.name),
            attributes=redact_attributes(event.attributes),
            timestamp=event.timestamp,
        )
        for event in span.events
    ]
    links = [
        Link(link.context, redact_attributes(link.attributes)) for link in span.links
    ]
    resource = Resource(
        redact_attributes(span.resource.attributes), span.resource.schema_url
    )
    status = span.status
    if status.description:
        description = _redact_attribute("status.description", status.description)
        status = Status(status.status_code, description)
    return ReadableSpan(
        name=_redact_attribute("span.name", span.name),
        context=span.context,
        parent=span.parent,
        resource=resource,
        attributes=redact_attributes(span.attributes),
        events=events,
        links=links,
        kind=span.kind,
        status=status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


def redact_each(
    items: Sequence[Any], redact: Callable[[Any], Any], kind: str
) -> List[Any]:
    """``redact`` applied to every item; an item whose redaction raises is
    dropped after a WARNING (never exported unredacted)."""
    redacted: List[Any] = []
    for item in items:
        try:
            redacted.append(redact(item))
        except Exception as exc:  # noqa: BLE001 -- never export unredacted
            logger.warning(
                "Telemetry %s redaction failed (%s); the %s is not exported",
                kind,
                type(exc).__name__,
                kind,
            )
    return redacted


class RedactingSpanExporter(SpanExporter):
    """Hands the wrapped exporter redacted copies of every span."""

    def __init__(self, delegate: SpanExporter) -> None:
        self._delegate = delegate

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._delegate.export(redact_each(spans, _redacted_span, "span"))

    def shutdown(self) -> None:
        self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return bool(self._delegate.force_flush(timeout_millis))


def _redacted_log_record(record: ReadableLogRecord) -> ReadableLogRecord:
    """A copy of ``record`` whose body and every attribute (``exception.*``
    included) are redacted."""
    log_record = copy.copy(record.log_record)
    log_record.body = _redact_attribute("body", record.log_record.body)
    log_record.attributes = redact_attributes(record.log_record.attributes)
    return dataclasses.replace(record, log_record=log_record)


class RedactingLogRecordExporter(LogRecordExporter):
    """Hands the wrapped exporter redacted copies of every log record."""

    def __init__(self, delegate: LogRecordExporter) -> None:
        self._delegate = delegate

    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        records = redact_each(batch, _redacted_log_record, "log record")
        return self._delegate.export(records)

    def shutdown(self) -> None:
        self._delegate.shutdown()
