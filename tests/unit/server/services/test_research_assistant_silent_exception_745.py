"""
Unit tests for Story #929 Item #15 — anti-silent-failure fix.

Tests verify that when JobTracker.update_status or register_job raises an
exception at research_assistant_service.py line ~1091, a logger.warning is
emitted with exc_info=True (Messi Rule #13) and chat execution continues.

The fix replaces:
    except Exception:
        pass  # Tracker failure must never break chat execution

With:
    except Exception as e:
        logger.warning("Job tracker registration failed: %s", e, exc_info=True)
"""

import logging
from typing import Callable, cast
from unittest.mock import MagicMock, patch

import pytest

_RA_LOGGER = "code_indexer.server.services.research_assistant_service"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def make_failing_send(tmp_path) -> Callable[[str, str], str]:
    """
    Fixture factory that injects a failing JobTracker into a real
    ResearchAssistantService and mocks threading.Thread at the OS boundary.

    Returns a callable:
        send(fail_method, error_msg) -> job_id

    Where fail_method is the tracker method to make raise (e.g. "update_status"
    or "register_job") and error_msg is the RuntimeError message.
    """
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.services.research_assistant_service import (
        ResearchAssistantService,
    )

    db_path = str(tmp_path / "data" / "cidx_server.db")
    (tmp_path / "data").mkdir(parents=True)
    DatabaseSchema(db_path=db_path).initialize_database()
    service = ResearchAssistantService(db_path=db_path)

    session = service.create_session()
    session_id = session["id"]

    def send(fail_method: str, error_msg: str) -> str:
        failing_tracker = MagicMock()
        getattr(failing_tracker, fail_method).side_effect = RuntimeError(error_msg)
        service._job_tracker = failing_tracker
        mock_thread = MagicMock()
        target = (
            "code_indexer.server.services.research_assistant_service.threading.Thread"
        )
        with patch(target, return_value=mock_thread):
            # execute_prompt is typed as Any (untyped service method), but this test path returns str
            return cast(
                str, service.execute_prompt(session_id=session_id, user_prompt="hello")
            )

    return send


def _assert_tracker_warning_with_exc_info(caplog_records: list) -> None:
    """Assert at least one tracker-related WARNING with exc_info is present."""
    tracker_warnings = [
        r
        for r in caplog_records
        if r.levelno == logging.WARNING and "tracker" in r.message.lower()
    ]
    assert tracker_warnings, (
        "Expected logger.warning about tracker failure. "
        f"Got records: {[(r.levelno, r.message) for r in caplog_records]}"
    )
    for rec in tracker_warnings:
        assert rec.exc_info is not None and rec.exc_info[0] is not None, (
            f"logger.warning must be called with exc_info=True (Messi Rule #13). "
            f"Record: {rec.message!r}, exc_info={rec.exc_info}"
        )


# ---------------------------------------------------------------------------
# Tests — update_status failure
# ---------------------------------------------------------------------------


class TestUpdateStatusFailureIsLogged:
    """Item #15: update_status failure must be logged with exc_info, not swallowed."""

    def test_warning_with_exc_info_when_update_status_raises(
        self, make_failing_send, caplog
    ):
        """
        When JobTracker.update_status raises, logger.warning must be emitted
        with exc_info=True and a message mentioning the tracker failure.
        """
        with caplog.at_level(logging.WARNING, logger=_RA_LOGGER):
            make_failing_send("update_status", "tracker unavailable")

        _assert_tracker_warning_with_exc_info(caplog.records)

    def test_job_id_returned_after_update_status_failure(self, make_failing_send):
        """Chat execution must continue: send_message returns a job_id."""
        job_id = make_failing_send("update_status", "tracker unavailable")

        assert job_id is not None and isinstance(job_id, str), (
            f"send_message must return a job_id even when tracker fails. Got: {job_id!r}"
        )


# ---------------------------------------------------------------------------
# Tests — register_job failure
# ---------------------------------------------------------------------------


class TestRegisterJobFailureIsLogged:
    """Item #15: register_job failure must also be logged with exc_info, not swallowed."""

    def test_warning_with_exc_info_when_register_job_raises(
        self, make_failing_send, caplog
    ):
        """
        When register_job raises, logger.warning with exc_info=True must be
        emitted and send_message must still return a job_id.
        """
        with caplog.at_level(logging.WARNING, logger=_RA_LOGGER):
            job_id = make_failing_send("register_job", "register failed")

        assert job_id is not None, (
            f"send_message must return a job_id even when register_job fails. Got: {job_id!r}"
        )
        _assert_tracker_warning_with_exc_info(caplog.records)
