"""Bug #1392: lifespan.py must wire the hnswlib capability startup check.

Mirrors the established source-text guard pattern from
test_lifespan_clone_backend_wiring_bug1044.py: verifies the wiring call is
present in lifespan.py's source, and that it is a `try/except` non-fatal
call (per this file's own "don't block server startup" idiom).
"""

import ast
import logging
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


class TestHnswlibCapabilityCheckWiringSourceGuard:
    """Source-text guard: lifespan.py must call
    run_hnswlib_capability_startup_check() during startup."""

    def test_run_hnswlib_capability_startup_check_called_in_lifespan_source(self):
        source = _LIFESPAN_PATH.read_text()
        assert "run_hnswlib_capability_startup_check()" in source, (
            "Bug #1392: lifespan.py does not call "
            "run_hnswlib_capability_startup_check() during startup -- hnswlib "
            "capability drift on the server's own Python environment would "
            "never be surfaced at startup."
        )


class TestHnswlibCapabilityCheckLoggerExcInfoWiring:
    """Code-review remediation (LOW finding): format_error_log(**context)
    absorbs exc_info into the returned STRING -- it never reaches
    logger.error()'s real exc_info parameter, so no traceback was ever
    attached to the APP-GENERAL-1392 except-block log line. exc_info=True
    must be a kwarg of the OUTER logger.error(...) call, not the inner
    format_error_log(...) call.
    """

    def test_exc_info_is_kwarg_of_logger_error_not_format_error_log(self) -> None:
        source = _LIFESPAN_PATH.read_text()
        tree = ast.parse(source)

        inner_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "format_error_log"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "APP-GENERAL-1392"
        ]
        assert inner_calls, (
            "Could not find the format_error_log('APP-GENERAL-1392', ...) "
            "call in lifespan.py -- has the wiring moved?"
        )
        inner_call = inner_calls[0]

        inner_kwargs = {kw.arg for kw in inner_call.keywords if kw.arg}
        assert "exc_info" not in inner_kwargs, (
            "exc_info=True must NOT be passed into format_error_log() -- "
            "format_error_log(**context) absorbs it into the returned "
            "STRING, so it never reaches logging's real exc_info handling "
            "and no traceback is attached."
        )

        # Find the enclosing logger.error(...) call (the Call node whose
        # first positional arg IS this exact format_error_log call node).
        outer_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "error"
            and node.args
            and node.args[0] is inner_call
        ]
        assert outer_calls, (
            "Could not find the enclosing logger.error(...) call wrapping "
            "the APP-GENERAL-1392 format_error_log(...) call."
        )
        outer_call = outer_calls[0]
        outer_kwargs = {kw.arg: kw.value for kw in outer_call.keywords if kw.arg}
        assert "exc_info" in outer_kwargs, (
            "logger.error(...) must be called with exc_info=True directly "
            "so the actual traceback is attached to the log record."
        )
        exc_info_value = outer_kwargs["exc_info"]
        assert (
            isinstance(exc_info_value, ast.Constant) and exc_info_value.value is True
        ), (
            f"Expected exc_info=True on logger.error(...); got AST node: "
            f"{ast.dump(exc_info_value)}"
        )


class TestFormatErrorLogExcInfoAbsorption:
    """Runtime proof of the LOW finding's root cause and fix:
    format_error_log's **context absorbs exc_info into its returned string;
    only exc_info passed directly to logger.error() attaches a real
    traceback to the log record.
    """

    def test_exc_info_passed_to_format_error_log_does_not_attach_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from code_indexer.server.logging_utils import format_error_log

        logger = logging.getLogger("test_1392_buggy_exc_info_pattern")
        with caplog.at_level("ERROR", logger=logger.name):
            try:
                raise RuntimeError("boom")
            except RuntimeError as e:
                logger.error(
                    format_error_log("APP-GENERAL-1392", f"...: {e}", exc_info=True)
                )
        record = caplog.records[-1]
        assert record.exc_info is None, (
            "Reproduces the bug: exc_info absorbed by format_error_log's "
            "**context never reaches the logging record's real exc_info -- "
            "no traceback is attached."
        )

    def test_exc_info_passed_directly_to_logger_error_attaches_traceback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from code_indexer.server.logging_utils import format_error_log

        logger = logging.getLogger("test_1392_fixed_exc_info_pattern")
        with caplog.at_level("ERROR", logger=logger.name):
            try:
                raise RuntimeError("boom")
            except RuntimeError as e:
                logger.error(
                    format_error_log("APP-GENERAL-1392", f"...: {e}"),
                    exc_info=True,
                )
        record = caplog.records[-1]
        assert record.exc_info is not None, (
            "Fixed pattern: exc_info=True passed directly to logger.error() "
            "must attach the real traceback to the log record."
        )
        assert "RuntimeError: boom" in caplog.text
        assert "Traceback" in caplog.text
