"""
Structural tests for prepare_stored_secret_key wiring in lifespan.py (Story #999 Step 8).

Verifies that:
1. lifespan.py startup region calls prepare_stored_secret_key(
2. server_data_dir appears within +/-_PROXIMITY_LINES lines of that call site
3. storage_mode appears within +/-_PROXIMITY_LINES lines of that call site
"""

from __future__ import annotations

from pathlib import Path


# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)

# Number of lines before and after the call site to include in the proximity window.
_PROXIMITY_LINES = 5


def _lifespan_source() -> str:
    return _LIFESPAN_PATH.read_text()


def _startup_region(source: str) -> str:
    """Return source text before the bare yield (startup half only)."""
    marker = "\n        yield"
    idx = source.find(marker)
    assert idx != -1, "Could not find 'yield' marker in lifespan.py"
    return source[:idx]


def _call_site_window(startup: str) -> str:
    """Return a +/-_PROXIMITY_LINES-line window around the prepare_stored_secret_key( call.

    Returns up to 2*_PROXIMITY_LINES+1 lines total: _PROXIMITY_LINES before the call,
    the call line itself, and _PROXIMITY_LINES after it.
    Returns empty string if the call is not found.
    """
    lines = startup.splitlines()
    for i, line in enumerate(lines):
        if "prepare_stored_secret_key(" in line:
            start = max(0, i - _PROXIMITY_LINES)
            end = min(len(lines), i + _PROXIMITY_LINES + 1)
            return "\n".join(lines[start:end])
    return ""


def _encryption_salt_call_window() -> str:
    """Assert call exists in startup region and return its +/-_PROXIMITY_LINES-line window."""
    startup = _startup_region(_lifespan_source())
    assert "prepare_stored_secret_key(" in startup, (
        "lifespan.py startup region must call prepare_stored_secret_key("
    )
    window = _call_site_window(startup)
    assert window, "prepare_stored_secret_key( call not found in startup region"
    return window


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLifespanEncryptionSaltWiring:
    """Step 8: structural checks that the startup region calls prepare_stored_secret_key
    and keeps server_data_dir/storage_mode within a +/-_PROXIMITY_LINES-line window
    of that call."""

    def test_startup_calls_prepare_stored_secret_key(self):
        """lifespan.py startup region must call prepare_stored_secret_key(."""
        startup = _startup_region(_lifespan_source())
        assert "prepare_stored_secret_key(" in startup, (
            "lifespan.py startup region must call prepare_stored_secret_key("
        )

    def test_call_site_references_server_data_dir(self):
        """server_data_dir must appear within +/-_PROXIMITY_LINES lines of the call."""
        window = _encryption_salt_call_window()
        assert "server_data_dir" in window, (
            f"server_data_dir must appear within +/-{_PROXIMITY_LINES} lines of "
            f"prepare_stored_secret_key( call.\nActual window:\n{window}"
        )

    def test_call_site_references_storage_mode(self):
        """storage_mode must appear within +/-_PROXIMITY_LINES lines of the call."""
        window = _encryption_salt_call_window()
        assert "storage_mode" in window, (
            f"storage_mode must appear within +/-{_PROXIMITY_LINES} lines of "
            f"prepare_stored_secret_key( call.\nActual window:\n{window}"
        )
