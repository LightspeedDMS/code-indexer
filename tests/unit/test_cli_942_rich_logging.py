"""Tests for issue #942: Rich Live progress bar corruption caused by stdlib StreamHandler.

Verifies that cli.py's setup_logging() installs RichHandler (not the stdlib
StreamHandler) so that WARNING emissions from code_indexer.* loggers are
coordinated with the Rich Live region and appear as complete, untruncated lines
above the progress bar.
"""

import io
import logging
import pathlib

import pytest
from rich.console import Console
from rich.logging import RichHandler
from rich.live import Live

from code_indexer.cli import setup_logging


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CLI_PATH = (
    pathlib.Path(__file__).parent.parent.parent / "src" / "code_indexer" / "cli.py"
)

_BAND_AID_PATTERN = (
    'logging.getLogger("code_indexer.services.provider_health_monitor").setLevel('
)


# ---------------------------------------------------------------------------
# Shared helpers / fixtures
# ---------------------------------------------------------------------------


def _captured_console() -> tuple[Console, io.StringIO]:
    """Return a (Console, StringIO) pair suitable for in-process output capture."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=120)
    return console, buf


@pytest.fixture()
def clean_root_handlers():
    """Save and restore root-logger handlers AND level around each test.

    Both attributes are mutated by setup_logging() and by the AC2 helper;
    restoring both prevents order-dependent failures.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    root.handlers.clear()
    yield root
    root.handlers.clear()
    root.handlers.extend(saved_handlers)
    root.level = saved_level


@pytest.fixture()
def installed_handlers(clean_root_handlers):
    """Run setup_logging() against a clean root logger; return installed handlers."""
    setup_logging()
    return logging.getLogger().handlers[:]


# ---------------------------------------------------------------------------
# AC1 — setup_logging() installs RichHandler, not plain StreamHandler
# ---------------------------------------------------------------------------


class TestSetupLoggingHandlers:
    """setup_logging() must install RichHandler and must not install plain StreamHandler."""

    def test_installs_rich_handler(self, installed_handlers: list) -> None:
        assert any(isinstance(h, RichHandler) for h in installed_handlers), (
            f"Expected RichHandler; got {[type(h).__name__ for h in installed_handlers]}"
        )

    def test_does_not_install_plain_stream_handler(
        self, installed_handlers: list
    ) -> None:
        plain = [h for h in installed_handlers if type(h) is logging.StreamHandler]
        assert not plain, (
            "Plain stdlib StreamHandler must not be present; "
            f"found {[type(h).__name__ for h in installed_handlers]}"
        )


# ---------------------------------------------------------------------------
# AC2 — RichHandler produces complete WARNING output during a Live region
# ---------------------------------------------------------------------------


class TestWarningCompleteWithRichHandler:
    """WARNING text must not be truncated when a Rich Live region is active."""

    def _emit_warnings_during_live(
        self,
        root: logging.Logger,
        messages: list[str],
        logger_name: str = "code_indexer.test942",
    ) -> str:
        """Emit messages as WARNINGs inside a Live region; return captured output.

        Receives the already-cleared root logger from the fixture so that
        all cleanup is handled by the shared fixture teardown.
        """
        console, buf = _captured_console()

        handler = RichHandler(
            console=console,
            rich_tracebacks=False,
            show_time=False,
            show_path=False,
        )
        handler.setLevel(logging.WARNING)
        root.setLevel(logging.WARNING)
        root.addHandler(handler)

        logger = logging.getLogger(logger_name)
        with Live(renderable="[progress bar]", console=console, refresh_per_second=4):
            for msg in messages:
                logger.warning(msg)

        # Strip ANSI escape codes — RichHandler emits color codes around
        # quoted/highlighted tokens which break substring assertions on plain
        # text (verified by capturing raw output during the bug repro).
        import re as _re

        return _re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", buf.getvalue())

    def test_single_warning_complete_during_live(self, clean_root_handlers) -> None:
        warning_text = "Vector file not found for point '<uuid>', skipping"
        captured = self._emit_warnings_during_live(
            clean_root_handlers,
            [warning_text],
            logger_name="code_indexer.storage.filesystem_vector_store",
        )
        assert warning_text in captured, (
            f"WARNING text was truncated or missing.\n"
            f"Expected: {warning_text!r}\n"
            f"Captured: {captured!r}"
        )

    def test_multiple_warnings_all_complete_during_live(
        self, clean_root_handlers
    ) -> None:
        messages = [
            "First warning message complete text alpha",
            "Second warning message complete text beta",
            "Third warning message complete text gamma",
        ]
        captured = self._emit_warnings_during_live(clean_root_handlers, messages)
        for msg in messages:
            assert msg in captured, (
                f"Message was truncated or missing: {msg!r}\nCaptured: {captured!r}"
            )


# ---------------------------------------------------------------------------
# AC3 — per-logger provider_health_monitor mute is absent from cli.py
# ---------------------------------------------------------------------------


class TestProviderHealthMonitorBandAidRemoved:
    """The per-logger ERROR setLevel band-aid for provider_health_monitor must be gone."""

    def test_band_aid_not_in_active_cli_code(self) -> None:
        source = _CLI_PATH.read_text(encoding="utf-8")
        lines = source.splitlines()
        active_lines = [ln.strip() for ln in lines if not ln.strip().startswith("#")]
        offenders = [ln for ln in active_lines if _BAND_AID_PATTERN in ln]
        assert not offenders, (
            "Per-logger band-aid mute for provider_health_monitor found in cli.py "
            "active code — it should have been removed by the systemic RichHandler "
            f"fix (issue #942). Offending lines: {offenders}"
        )
