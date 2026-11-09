"""Patch an attribute of a Starlette ``app.state`` so teardown restores it.

Do not ``monkeypatch.setattr(app.state, ...)``. ``State`` keeps its attributes
in a private ``_state`` dict, not the instance ``__dict__``. Since pytest 9.2,
``setattr`` reads the value to restore from ``__dict__``, so it sees nothing
there and teardown *deletes* the attribute instead of restoring it. A second
patch of the same name then raises ``KeyError`` on teardown, and a value set
before the test (the server app's real services) is lost for later tests.
``monkeypatch.setitem`` on the backing dict restores correctly under every
pytest version.
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.datastructures import State


def patch_app_state(
    monkeypatch: pytest.MonkeyPatch, state: Any, name: str, value: Any
) -> None:
    """Set ``state.<name>`` to *value* for the current test only."""
    if isinstance(state, State):
        monkeypatch.setitem(state._state, name, value)
    else:
        monkeypatch.setattr(state, name, value, raising=False)
