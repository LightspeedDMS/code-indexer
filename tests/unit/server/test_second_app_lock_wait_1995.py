"""A second in-process app does not wait out the primary-instance bound (#1995).

The first ``create_app()`` in a process takes ``primary_instance.lock`` for
its data directory and holds it for the life of the process (Bug #1549).  A
second ``create_app()`` against the same directory -- routine in this suite
-- is refused, but only after the full production bound (5 s) has elapsed.
Under the gate's ``CIDX_TEST_FAST_SQLITE=1`` flag the suite's conftest makes
startup's acquire use a zero bound, so the refusal is immediate.  Production
code is unchanged; explicit timeouts (``test_primary_instance_lock_1549.py``)
are untouched.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from code_indexer.server.utils.primary_instance_lock import (
    release_primary_instance_lock,
)
from tests.unit.server._fast_sqlite import FAST_SQLITE_ENV

_LOCK_LOGGER = "code_indexer.server.utils.primary_instance_lock"
_PRODUCTION_BOUND_SECONDS = 5.0


@pytest.mark.skipif(
    os.environ.get(FAST_SQLITE_ENV) != "1",
    reason="the zero-bound startup acquire is installed only under the gate flag",
)
def test_second_app_in_one_process_is_refused_without_waiting(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from code_indexer.server.app import create_app
    from code_indexer.server.services.config_service import reset_config_service

    data_dir = str(tmp_path / "server")
    with patch.dict(
        "os.environ", {"CIDX_SERVER_DATA_DIR": data_dir, "CIDX_DATA_DIR": data_dir}
    ):
        try:
            reset_config_service()
            create_app()
            reset_config_service()
            with caplog.at_level(logging.WARNING, logger=_LOCK_LOGGER):
                started = time.monotonic()
                create_app()
                elapsed = time.monotonic() - started
        finally:
            reset_config_service()
            release_primary_instance_lock(data_dir)

    refusals = [
        r.getMessage()
        for r in caplog.records
        if r.name == _LOCK_LOGGER and "still holds" in r.getMessage()
    ]
    assert len(refusals) == 1, refusals
    assert "after waiting 0.0s" in refusals[0], refusals[0]
    assert elapsed < _PRODUCTION_BOUND_SECONDS, f"second create_app took {elapsed:.2f}s"
