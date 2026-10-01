"""Bug #1820 regression coverage: the leaked-IdentityQueueHandler guard.

Root cause (confirmed via py-spy + empirical reproduction): a test that calls
``async_logging.install_queue_logging(...)`` and then stops the returned
listener directly (``listener.stop()``) instead of calling
``async_logging.shutdown_queue_logging()`` leaves an ``IdentityQueueHandler``
permanently attached to the REAL process root logger. Because pytest runs
every test in ``tests/unit/`` in one shared process, every subsequent
``logger.error()``/``logger.critical()`` call anywhere in the same run
(propagate=True is the default) enqueues into that now-undrained queue. Once
it fills to ``DEFAULT_QUEUE_MAXSIZE`` (10,000), every later ERROR/CRITICAL log
pays the full ``_HIGH_SEVERITY_QUEUE_TIMEOUT_S`` (2s) bounded blocking put --
this is exactly the mechanism behind the ~50-minute test-suite near-stall
(266 CPU-seconds / 50 minutes wall) reported in Bug #1820.

``tests/unit/conftest.py`` installs an autouse guard fixture
(``_guard_no_leaked_identity_queue_handler_1820``) that snapshots the real
root logger's handlers before each test and, after it, self-heals (removes)
any newly-added ``IdentityQueueHandler`` while failing that specific test
loudly -- bounding the blast radius to the offending test instead of letting
the leak silently degrade the rest of the suite.

These tests cover the pure-logic helper (``_leaked_identity_queue_handlers``)
that fixture is built on, without needing to drive pytest's own fixture
machinery recursively.
"""

from __future__ import annotations

import logging
import queue as queue_module

from code_indexer.server.services.async_logging import IdentityQueueHandler
from tests.unit.conftest import _leaked_identity_queue_handlers


def test_leaked_identity_queue_handlers_detects_newly_added_handler() -> None:
    logger = logging.getLogger("bug1820.guard_test.new_handler")
    pre_existing = list(logger.handlers)
    new_handler = IdentityQueueHandler(queue_module.Queue())
    logger.addHandler(new_handler)
    try:
        leaked = _leaked_identity_queue_handlers(logger, pre_existing)
        assert leaked == [new_handler], (
            "a newly-added IdentityQueueHandler not present in the "
            "pre-existing snapshot must be reported as leaked"
        )
    finally:
        logger.removeHandler(new_handler)


def test_leaked_identity_queue_handlers_ignores_pre_existing_handler() -> None:
    logger = logging.getLogger("bug1820.guard_test.pre_existing_handler")
    existing_handler = IdentityQueueHandler(queue_module.Queue())
    logger.addHandler(existing_handler)
    try:
        pre_existing = list(logger.handlers)
        leaked = _leaked_identity_queue_handlers(logger, pre_existing)
        assert leaked == [], (
            "a handler that was already attached BEFORE the test started "
            "must not be reported as leaked -- only handlers newly added "
            "during the test are the test's own responsibility"
        )
    finally:
        logger.removeHandler(existing_handler)


def test_leaked_identity_queue_handlers_ignores_non_identity_queue_handlers() -> None:
    logger = logging.getLogger("bug1820.guard_test.other_handler_type")
    pre_existing = list(logger.handlers)
    other_handler = logging.StreamHandler()
    logger.addHandler(other_handler)
    try:
        leaked = _leaked_identity_queue_handlers(logger, pre_existing)
        assert leaked == [], (
            "only IdentityQueueHandler instances are the guard's concern -- "
            "an ordinary StreamHandler added by a test is not this bug's shape"
        )
    finally:
        logger.removeHandler(other_handler)
