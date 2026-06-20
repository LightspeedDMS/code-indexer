import contextlib
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest


def _patch_research_assistant_service() -> contextlib.AbstractContextManager:  # type: ignore[type-arg]
    """Return a context manager that patches enforce_pace_maker_config in
    research_assistant_service when that module is importable (Python 3.9+
    with fastapi/bleach installed).  Falls back to a no-op context manager
    when the module cannot be imported (Python 3.11 without fastapi/bleach).
    """
    try:
        import code_indexer.server.services.research_assistant_service as _ras

        return patch.object(_ras, "enforce_pace_maker_config", MagicMock())
    except (ImportError, ModuleNotFoundError):
        return contextlib.nullcontext()


@pytest.fixture(autouse=True)
def _disable_pace_maker_guard() -> Generator[None, None, None]:
    import code_indexer.server.services.claude_invoker as _ci

    with patch.object(_ci, "enforce_pace_maker_config", MagicMock()):
        with _patch_research_assistant_service():
            yield
