"""
Unit tests for CleanupManager._onerror callback (Bug #343).

Tests that the _onerror callback inside _robust_delete() handles TypeError
correctly when os.open() is retried without its required 'flags' argument.

TDD RED phase: test_onerror_with_os_open_does_not_raise_typeerror is written
BEFORE the fix is applied. It is expected to FAIL until the fix is applied
(changing 'except OSError' to 'except (OSError, TypeError)' at line 185).
"""

import errno
import os
import unittest.mock as mock
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_onerror(tmp_path: Path):
    """
    Instantiate CleanupManager and extract the _onerror closure from
    _robust_delete by intercepting shutil.rmtree during the call.
    """
    from code_indexer.global_repos.cleanup_manager import CleanupManager
    from code_indexer.global_repos.query_tracker import QueryTracker

    tracker = QueryTracker()
    manager = CleanupManager(query_tracker=tracker)

    captured = {}

    def _capture_rmtree(path, onerror=None, **kwargs):
        captured["onerror"] = onerror
        # Do NOT actually delete anything

    with mock.patch("shutil.rmtree", side_effect=_capture_rmtree):
        manager._robust_delete(tmp_path)

    return captured.get("onerror")


# ---------------------------------------------------------------------------
# Bug #343: TypeError when _onerror retries os.open() without flags arg
# ---------------------------------------------------------------------------


class TestOnerrorCallbackBug343:
    """
    Verify that _onerror does NOT propagate TypeError when func=os.open
    is called with only (path,) — missing the required 'flags' argument.

    Root cause: shutil._rmtree_safe_fd() calls os.open(path, flags), and
    when it fails with EMFILE, _onerror receives func=os.open. The retry
    'func(failed_path)' only passes the path, omitting 'flags', causing:
        TypeError: open() missing required argument 'flags' (pos 2)

    Fix: catch (OSError, TypeError) instead of just OSError.
    """

    def test_onerror_with_os_open_does_not_raise_typeerror(self, tmp_path):
        """
        BUG #343 regression test: calling _onerror with func=os.open and an
        EMFILE OSError must NOT raise TypeError.

        This test FAILS before the fix (only OSError caught) and PASSES after
        the fix (OSError, TypeError caught).
        """
        onerror = _extract_onerror(tmp_path)
        assert onerror is not None, "Could not capture _onerror closure"

        emfile_exc = OSError(errno.EMFILE, "Too many open files")
        exc_info = (type(emfile_exc), emfile_exc, None)

        # os.open requires two positional args: (path, flags).
        # When _onerror retries func(failed_path) with func=os.open,
        # calling os.open(path) raises TypeError.
        # After the fix, that TypeError must be caught, not propagated.
        try:
            onerror(os.open, "/some/path", exc_info)
        except TypeError as e:
            pytest.fail(
                f"_onerror propagated TypeError (Bug #343 not fixed): {e}"
            )
        except OSError:
            # An OSError from the retry itself is acceptable — not the bug
            pass

    def test_onerror_retries_simple_callable_on_emfile(self, tmp_path):
        """
        Existing behavior: when func is a simple callable (e.g. os.unlink),
        _onerror retries it on EMFILE.  The callable receives the path and
        either succeeds or raises OSError (which is silently swallowed).

        This test must pass both BEFORE and AFTER the fix.
        """
        onerror = _extract_onerror(tmp_path)
        assert onerror is not None, "Could not capture _onerror closure"

        call_log = []

        def fake_unlink(path):
            call_log.append(path)

        emfile_exc = OSError(errno.EMFILE, "Too many open files")
        exc_info = (type(emfile_exc), emfile_exc, None)

        # Should NOT raise; the callable is invoked and succeeds silently
        onerror(fake_unlink, "/some/file.txt", exc_info)

        assert call_log == ["/some/file.txt"], (
            "Expected _onerror to retry the callable with the failed path"
        )

    def test_onerror_raises_on_non_emfile_oserror(self, tmp_path):
        """
        Non-EMFILE OSErrors must still be re-raised by _onerror.
        This ensures the fix does not suppress legitimate errors.
        """
        onerror = _extract_onerror(tmp_path)
        assert onerror is not None, "Could not capture _onerror closure"

        perm_exc = OSError(errno.EACCES, "Permission denied")
        exc_info = (type(perm_exc), perm_exc, None)

        def fake_func(path):
            pass

        with pytest.raises(OSError) as exc_info_ctx:
            onerror(fake_func, "/some/path", exc_info)

        assert exc_info_ctx.value.errno == errno.EACCES
