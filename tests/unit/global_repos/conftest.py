import contextlib
import logging
from typing import Generator, List
from unittest.mock import MagicMock, patch

import pytest

from tests.fixtures.refresh_scheduler_stores import scheduler_iteration_failures

# A test that deliberately makes scheduler loop iterations fail opts in.
SCHEDULER_FAILURES_EXPECTED = "scheduler_iteration_failures_expected"
_SCHEDULER_LOGGER = "code_indexer.global_repos.refresh_scheduler"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{SCHEDULER_FAILURES_EXPECTED}: the test deliberately makes "
        "RefreshScheduler loop iterations fail",
    )


class _RecordCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture(autouse=True)
def _fail_on_hidden_scheduler_iteration_failure(
    request: pytest.FixtureRequest,
) -> Generator[None, None, None]:
    """A scheduler loop iteration that fails is caught and logged by the loop
    itself, so the test around it would still pass: fail it here instead."""
    collector = _RecordCollector()
    logger = logging.getLogger(_SCHEDULER_LOGGER)
    logger.addHandler(collector)
    try:
        yield
    finally:
        logger.removeHandler(collector)
    failures = scheduler_iteration_failures(collector.records)
    if (
        failures
        and request.node.get_closest_marker(SCHEDULER_FAILURES_EXPECTED) is None
    ):
        pytest.fail(
            "RefreshScheduler loop iterations failed during this test "
            f"(mark it {SCHEDULER_FAILURES_EXPECTED} if deliberate): {failures}"
        )


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
