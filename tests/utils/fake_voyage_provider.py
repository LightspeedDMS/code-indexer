"""The local fake VoyageAI provider for tests that run a real `cidx index`.

Loads scripts/analysis/reembed_repro/fake_voyage_server.py (plain HTTP on
127.0.0.1, sentinel key): no real provider call is possible through it.
"""

from __future__ import annotations

import contextlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator

_FAKE_SERVER_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "analysis"
    / "reembed_repro"
    / "fake_voyage_server.py"
)


def load_fake_voyage_server_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "fake_voyage_server", _FAKE_SERVER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["fake_voyage_server"] = module
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def running_fake_voyage_server(api_key: str) -> Iterator[Any]:
    """A started FakeVoyageServer; stopped on exit, which then asserts the
    fake saw no protocol violation."""
    server = load_fake_voyage_server_module().FakeVoyageServer(api_key)
    server.start("127.0.0.1", 0)
    try:
        yield server
    finally:
        server.stop()
    assert server.ledger.violations() == []
