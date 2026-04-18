"""Regression tests for Bug #749: run_async() must tolerate sync results.

The CLI wraps every API-client method invocation in ``run_async()``. API
clients use synchronous ``httpx.Client`` and return plain results (dict,
list, etc.) — FastAPI will run them in its threadpool when they're invoked
from an ``async def`` handler. Before Bug #749's fix, ``run_async()``
called ``asyncio.run(value)`` unconditionally, which crashed with
``ValueError: a coroutine was expected, got {'group_id': 5, ...}`` when
the input was a sync result instead of a coroutine.

The fix is a defensive pass-through in ``run_async()``: if the input is
not a coroutine, return it verbatim. Sync methods stay sync (and run on
the threadpool when invoked from async context), and the CLI wrapper
accepts both coroutines and plain values.

These tests enforce that contract permanently.
"""

from __future__ import annotations

from code_indexer.cli import run_async


async def _async_return_dict() -> dict:
    """Helper: a real coroutine that returns a dict — the happy async path."""
    return {"group_id": 5, "name": "async_grp"}


def test_run_async_returns_coroutine_result_unchanged() -> None:
    """Existing behavior: passing a coroutine still awaits it and returns its result."""
    result = run_async(_async_return_dict())
    assert result == {"group_id": 5, "name": "async_grp"}


def test_run_async_passes_through_dict() -> None:
    """Bug #749: sync-method results (dict) must pass through without raising.

    This is the exact shape that caused the original
    ``"a coroutine was expected, got {'group_id': 5, 'name': 'testgrp'}"``
    crash when ``cidx admin groups create`` was invoked.
    """
    payload = {"group_id": 5, "name": "testgrp"}
    result = run_async(payload)
    assert result is payload  # identity preserved, no wrapping


def test_run_async_passes_through_none_and_primitives() -> None:
    """Bug #749: pass-through is general — not dict-specific.

    Defensive coverage: None, strings, and lists all flow through unchanged.
    """
    assert run_async(None) is None
    assert run_async("plain string") == "plain string"

    items = [1, 2, 3]
    assert run_async(items) is items
