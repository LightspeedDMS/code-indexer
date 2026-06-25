"""Bug #1205 regression guard: additional-provider temporal loop must show progress.

During `cidx index --index-commits` with more than one embedding provider the
primary provider renders a live Rich progress tree, but every additional provider
(2..N) was previously running with NO progress output because the Rich Live
display was torn down immediately after the primary provider and never restarted
for the extra-provider loop.

This test suite uses source-order inspection (the documented CI-friendly pattern
for this repo) to guard five invariants:

1. PRIMARY teardown guard: `stop_display()` is called AFTER the primary
   `index_commits` and BEFORE the extra-provider loop.  This must remain
   byte-identical to the pre-fix behaviour.

2. EXTRA-PROVIDER start guard: inside the extra-provider loop,
   `start_bottom_display()` is called AFTER the "additional provider" intro
   print and BEFORE `index_commits`.

3. EXTRA-PROVIDER stop guard: inside the extra-provider loop,
   `stop_display()` is called AFTER `index_commits` and BEFORE the per-provider
   completion print (the `console.print(f"... commits")` line).

4. PROGRESS CALLBACK guard: `index_commits` inside the extra-provider loop
   is called with a `progress_callback=` keyword argument that is NOT None
   (i.e. not `progress_callback=None`).

5. EXCEPTION SAFETY guard: `stop_display()` inside the extra-provider loop
   is called inside the `finally:` block so a raised exception cannot leak
   an active Rich Live display.
"""

from __future__ import annotations

from pathlib import Path


# tests/unit/cli/ -> tests/unit/ -> tests/ -> repo-root (parents[3])
_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLI_PATH = _REPO_ROOT / "src" / "code_indexer" / "cli.py"


def _read_cli_source() -> str:
    return _CLI_PATH.read_text()


# ---------------------------------------------------------------------------
# Helper: locate the extra-provider loop body in the cli.py source text.
# ---------------------------------------------------------------------------


def _extra_loop_body(source: str) -> str:
    """Return the substring of cli.py that covers the extra-provider for-loop.

    We look for the loop header and extract everything up to the first
    ``sys.exit(0)`` that follows it (the normal end of the temporal branch).
    """
    loop_marker = (
        "for _extra_idx, _extra_provider in enumerate(_extra_temporal_providers):"
    )
    loop_start = source.find(loop_marker)
    assert loop_start != -1, (
        "Cannot find extra-provider loop in cli.py. "
        "Expected: 'for _extra_idx, _extra_provider in enumerate(_extra_temporal_providers):'"
    )
    # Find sys.exit(0) after loop start
    exit_pos = source.find("sys.exit(0)", loop_start)
    if exit_pos == -1:
        return source[loop_start:]
    return source[loop_start:exit_pos]


class TestPrimaryTeardownUnchanged:
    """Primary provider teardown must remain before the extra-provider loop."""

    def test_primary_stop_display_before_extra_loop(self):
        """stop_display() must still be called right after the primary index_commits.

        This is the existing :3455 teardown. Bug #1205 fix must NOT remove it.
        The teardown must appear BEFORE the extra-provider loop header.
        """
        source = _read_cli_source()

        # Primary teardown: 'if display_initialized:\n    rich_live_manager.stop_display()'
        # immediately after the primary index_commits block.
        stop_pos = source.find("rich_live_manager.stop_display()")
        assert stop_pos != -1, "rich_live_manager.stop_display() not found in cli.py"

        loop_marker = (
            "for _extra_idx, _extra_provider in enumerate(_extra_temporal_providers):"
        )
        loop_pos = source.find(loop_marker)
        assert loop_pos != -1, "Extra-provider loop not found in cli.py"

        assert stop_pos < loop_pos, (
            "Bug #1205: primary stop_display() must appear BEFORE the extra-provider loop. "
            f"stop_display at char {stop_pos}, loop starts at char {loop_pos}."
        )

    def test_display_initialized_guard_still_wraps_primary_stop(self):
        """'if display_initialized:' must still guard the primary stop_display call."""
        source = _read_cli_source()

        # Find the primary teardown block: the first 'if display_initialized:'
        # that precedes 'Temporal indexing completed!'
        completed_pos = source.find("Temporal indexing completed!")
        assert completed_pos != -1, "'Temporal indexing completed!' not found in cli.py"

        guard_pos = source.rfind("if display_initialized:", 0, completed_pos)
        assert guard_pos != -1, (
            "'if display_initialized:' guard not found before 'Temporal indexing completed!' "
            "in cli.py. The primary stop_display guard must remain unchanged."
        )


class TestExtraProviderDisplayStarted:
    """Each extra provider must start a fresh Rich Live display."""

    def test_start_bottom_display_called_inside_extra_loop(self):
        """start_bottom_display() must be called inside the extra-provider loop.

        Bug #1205: without this call there is no active Live display for the
        extra provider's progress to render into.
        """
        loop_body = _extra_loop_body(_read_cli_source())

        assert "start_bottom_display()" in loop_body, (
            "Bug #1205: rich_live_manager.start_bottom_display() not found inside the "
            "extra-provider loop.  Each additional provider must start its own "
            "Rich Live display so progress renders during its index_commits call."
        )

    def test_start_display_before_index_commits_in_extra_loop(self):
        """start_bottom_display() must precede index_commits inside the extra loop.

        The display must be active BEFORE index_commits is called so the
        progress callback has a live target to render into.
        """
        loop_body = _extra_loop_body(_read_cli_source())

        start_pos = loop_body.find("start_bottom_display()")
        assert start_pos != -1, (
            "start_bottom_display() not found in extra-provider loop body"
        )

        index_commits_pos = loop_body.find("_extra_temporal_indexer.index_commits(")
        assert index_commits_pos != -1, (
            "_extra_temporal_indexer.index_commits( not found in extra-provider loop body"
        )

        assert start_pos < index_commits_pos, (
            "Bug #1205: start_bottom_display() must be called BEFORE "
            "_extra_temporal_indexer.index_commits() in the extra-provider loop. "
            f"start at offset {start_pos}, index_commits at offset {index_commits_pos}."
        )

    def test_start_display_after_intro_print_in_extra_loop(self):
        """start_bottom_display() must come AFTER the 'additional provider' intro print.

        Printing 'additional provider: {provider}' BEFORE the Live display is
        started ensures the bare console.print does not interleave with an
        active Rich Live display.
        """
        loop_body = _extra_loop_body(_read_cli_source())

        intro_pos = loop_body.find("additional provider:")
        assert intro_pos != -1, (
            "'additional provider:' intro print not found in extra-provider loop body"
        )

        start_pos = loop_body.find("start_bottom_display()")
        assert start_pos != -1, (
            "start_bottom_display() not found in extra-provider loop body"
        )

        assert intro_pos < start_pos, (
            "Bug #1205: 'additional provider:' intro print must appear BEFORE "
            "start_bottom_display() to avoid interleaving with an active Rich Live display. "
            f"intro at offset {intro_pos}, start_bottom_display at offset {start_pos}."
        )


class TestExtraProviderDisplayStopped:
    """Each extra provider must stop its Rich Live display before printing completion."""

    def test_stop_display_called_inside_extra_loop(self):
        """stop_display() must be called inside the extra-provider loop.

        The Bug #1205 fix must add a stop_display() call so the display is
        torn down after each additional provider completes.
        """
        loop_body = _extra_loop_body(_read_cli_source())

        assert "stop_display()" in loop_body, (
            "Bug #1205: rich_live_manager.stop_display() not found inside the "
            "extra-provider loop.  Each additional provider must stop its Rich "
            "Live display after index_commits completes."
        )

    def test_stop_display_after_index_commits_in_extra_loop(self):
        """stop_display() must follow index_commits inside the extra loop."""
        loop_body = _extra_loop_body(_read_cli_source())

        index_commits_pos = loop_body.find("_extra_temporal_indexer.index_commits(")
        assert index_commits_pos != -1, (
            "_extra_temporal_indexer.index_commits( not found in extra-provider loop body"
        )

        stop_pos = loop_body.find("stop_display()")
        assert stop_pos != -1, "stop_display() not found in extra-provider loop body"

        assert stop_pos > index_commits_pos, (
            "Bug #1205: stop_display() must be called AFTER "
            "_extra_temporal_indexer.index_commits() in the extra-provider loop. "
            f"index_commits at offset {index_commits_pos}, stop_display at offset {stop_pos}."
        )

    def test_stop_display_before_completion_print_in_extra_loop(self):
        """stop_display() must precede the per-provider completion print.

        Rich Live + bare console.print interleave badly.  The display must be
        stopped BEFORE the '... commits' completion line is printed.
        """
        loop_body = _extra_loop_body(_read_cli_source())

        stop_pos = loop_body.find("stop_display()")
        assert stop_pos != -1, "stop_display() not found in extra-provider loop body"

        # The completion print contains '.total_commits' in the f-string
        completion_pos = loop_body.find("_extra_indexing_result.total_commits")
        assert completion_pos != -1, (
            "'_extra_indexing_result.total_commits' completion print not found "
            "in extra-provider loop body"
        )

        assert stop_pos < completion_pos, (
            "Bug #1205: stop_display() must be called BEFORE the completion "
            "print ('_extra_indexing_result.total_commits') to avoid interleaving "
            "a Rich Live display with a bare console.print. "
            f"stop_display at offset {stop_pos}, completion print at offset {completion_pos}."
        )


class TestExtraProviderProgressCallback:
    """index_commits in the extra-provider loop must receive a non-None progress_callback."""

    def test_index_commits_receives_non_none_progress_callback_kwarg(self):
        """_extra_temporal_indexer.index_commits() must be called with a real callback.

        Without this argument (or with progress_callback=None) the display has
        no events to render regardless of whether it is started.
        """
        loop_body = _extra_loop_body(_read_cli_source())

        index_commits_pos = loop_body.find("_extra_temporal_indexer.index_commits(")
        assert index_commits_pos != -1, (
            "_extra_temporal_indexer.index_commits( not found in extra-provider loop body"
        )

        # Locate the full call argument list (up to the matching close paren).
        # The call spans multiple lines; we scan for the closing paren by tracking depth.
        open_paren = loop_body.find("(", index_commits_pos)
        assert open_paren != -1

        depth = 0
        close_paren = open_paren
        for i, ch in enumerate(loop_body[open_paren:], start=open_paren):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    close_paren = i
                    break

        call_text = loop_body[index_commits_pos : close_paren + 1]

        assert "progress_callback" in call_text, (
            "Bug #1205: _extra_temporal_indexer.index_commits() must be called with "
            "progress_callback= so that the active Rich Live display receives updates. "
            f"Current call text: {call_text!r}"
        )

        # Ensure it is not explicitly set to None
        assert "progress_callback=None" not in call_text, (
            "Bug #1205: progress_callback=None explicitly disables progress for the "
            "extra-provider loop.  Pass the real callback (_make_offset_callback(...))."
        )


class TestExtraProviderExceptionSafety:
    """The extra-provider display teardown must be exception-safe."""

    def test_stop_display_inside_finally_block_in_extra_loop(self):
        """stop_display() must live inside the finally: block of the extra-provider try.

        If index_commits raises, the Rich Live display must still be stopped —
        leaking an active Live display breaks all subsequent console output.
        The existing try/finally block that closes _extra_temporal_indexer is
        the correct place to put the stop_display() call.
        """
        loop_body = _extra_loop_body(_read_cli_source())

        finally_pos = loop_body.find("finally:")
        assert finally_pos != -1, (
            "'finally:' block not found in extra-provider loop body. "
            "The existing try/finally is needed for exception-safe teardown."
        )

        stop_pos = loop_body.find("stop_display()", finally_pos)
        assert stop_pos != -1, (
            "Bug #1205: stop_display() must appear INSIDE the 'finally:' block "
            "of the extra-provider loop so it fires even when index_commits raises. "
            "A leaked Rich Live display corrupts all subsequent console output."
        )
