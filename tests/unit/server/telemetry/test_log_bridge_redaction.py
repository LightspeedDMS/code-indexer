"""Log records leave the process through the OTEL log bridge with credential
values masked, in the body and in the attributes alike.

Driven through the real OTEL logging bridge handler and SDK pipeline, with an
in-memory exporter standing in for the collector.
"""

from __future__ import annotations

import logging

from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    InMemoryLogExporter,
    SimpleLogRecordProcessor,
)

from code_indexer.server.telemetry.log_handler import ContextAwareLogBridgeHandler

SECRET = "ExampleLogSecret123"


def test_bridge_exports_secret_masked_in_body_and_attributes() -> None:
    exporter = InMemoryLogExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    handler = ContextAwareLogBridgeHandler(
        LoggingHandler(level=logging.NOTSET, logger_provider=provider)
    )
    record = logging.LogRecord(
        name="code_indexer.example",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Request failed: token=%s",
        args=(SECRET,),
        exc_info=None,
    )
    record.api_key = SECRET
    record.repo_alias = "example-repo-global"

    try:
        handler.emit(record)
        provider.force_flush()
        exported = exporter.get_finished_logs()
    finally:
        provider.shutdown()

    assert len(exported) == 1
    log_record = exported[0].log_record
    body = str(log_record.body)
    assert body.startswith("Request failed: token=")
    assert SECRET not in body
    attributes = dict(log_record.attributes or {})
    assert SECRET not in str(attributes)
    assert attributes.get("api_key") != SECRET
    assert attributes.get("repo_alias") == "example-repo-global"


def test_bridge_exports_no_secret_from_a_logged_exception() -> None:
    exporter = InMemoryLogExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    handler = ContextAwareLogBridgeHandler(
        LoggingHandler(level=logging.NOTSET, logger_provider=provider)
    )
    example_logger = logging.getLogger("code_indexer.example.bridge")
    example_logger.addHandler(handler)
    example_logger.propagate = False
    try:
        try:
            raise RuntimeError(f"rejected token={SECRET}")
        except RuntimeError:
            example_logger.exception("Example operation failed")
        provider.force_flush()
        exported = exporter.get_finished_logs()
    finally:
        example_logger.removeHandler(handler)
        example_logger.propagate = True
        provider.shutdown()

    assert len(exported) == 1
    log_record = exported[0].log_record
    text = str(log_record.body) + repr(dict(log_record.attributes or {}))
    assert "Example operation failed" in text
    assert "RuntimeError" in text and "token=" in text
    assert (SECRET in text) is False
