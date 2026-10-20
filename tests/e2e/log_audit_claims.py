"""Expected log entries claimed by tests, and the session-level gate outcome.

Claims: a test that deliberately provokes a diagnostic (a refused request, a
revoked handle) records the current log id, makes the call, then claims the
exact texts it expects.  ``claim_expected_entries`` waits for one NEW
ERROR/WARNING entry per expected text, fails when one is missing, and marks
exactly those entry ids as expected; the phase gate
(``log_audit_gate.run_log_audit_gate``) skips claimed ids only, so the same
text logged by any other request is still audited.  The registry is per
process: every phase runs its server tests in one pytest process.

Session outcome: a phase's gate fixture records its result on the pytest
config; ``apply_gate_exit_status`` (called from each phase conftest's
``pytest_sessionfinish``) makes a failed gate a nonzero session exit status
and prints the report, whatever the outcome of the last test (a teardown
failure of an xfail-marked last test is otherwise reported as xfailed).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, FrozenSet, List, Sequence, Set

import pytest

CLAIM_TIMEOUT_SECONDS = 20.0
CLAIM_POLL_SECONDS = 0.3
CLAIM_SETTLE_SECONDS = 1.5

_CLAIMED_IDS: Set[int] = set()
_CLAIM_LOCK = threading.Lock()
_GATE_RESULTS = pytest.StashKey[List[Any]]()


def claimed_ids() -> FrozenSet[int]:
    with _CLAIM_LOCK:
        return frozenset(_CLAIMED_IDS)


def current_log_id(client: Any, token: str) -> int:
    """The newest ERROR/WARNING log id now (claims only look above it)."""
    from tests.e2e.log_audit_gate import flush_log_pipeline, get_log_watermark

    if hasattr(client, "app"):
        flush_log_pipeline(client)
    return get_log_watermark(client, token)


def _match_expected(
    fresh: List[Dict[str, Any]], expected: Sequence[str]
) -> tuple[Dict[int, Dict[str, Any]], List[str]]:
    taken: Dict[int, Dict[str, Any]] = {}
    missing: List[str] = []
    for text in expected:
        match = next(
            (
                e
                for e in fresh
                if int(e["id"]) not in taken and text in str(e.get("message"))
            ),
            None,
        )
        if match is None:
            missing.append(text)
        else:
            taken[int(match["id"])] = match
    return taken, missing


def claim_expected_entries(
    client: Any, token: str, after_id: int, expected: Sequence[str]
) -> List[Dict[str, Any]]:
    """Claim one new entry (id > *after_id*) per text in *expected*.

    Raises AssertionError naming the texts with no matching new entry once
    ``CLAIM_TIMEOUT_SECONDS`` have passed.
    """
    from tests.e2e.log_audit_gate import flush_log_pipeline, query_logs_via_mcp

    if not expected:
        return []
    deadline = time.monotonic() + CLAIM_TIMEOUT_SECONDS
    while True:
        if hasattr(client, "app"):
            flush_log_pipeline(client)
        entries = query_logs_via_mcp(client, token)
        with _CLAIM_LOCK:
            fresh = [
                e
                for e in entries
                if int(e.get("id") or 0) > after_id
                and int(e.get("id") or 0) not in _CLAIMED_IDS
            ]
            taken, missing = _match_expected(fresh, expected)
            if not missing:
                _CLAIMED_IDS.update(taken)
                return list(taken.values())
        if time.monotonic() >= deadline:
            raise AssertionError(f"expected log entries not found: {missing}")
        time.sleep(CLAIM_POLL_SECONDS)


def claim_present_entries(
    client: Any,
    token: str,
    after_id: int,
    texts: Sequence[str],
    settle_seconds: float = CLAIM_SETTLE_SECONDS,
) -> List[Dict[str, Any]]:
    """Claim every new entry (id > *after_id*) containing one of *texts*,
    present or not: for diagnostics a call may log asynchronously.  Waits
    *settle_seconds* for late writes; the window still starts at the
    caller's own mark, so the same text logged before it stays audited."""
    from tests.e2e.log_audit_gate import flush_log_pipeline, query_logs_via_mcp

    if not texts:
        return []
    time.sleep(settle_seconds)
    if hasattr(client, "app"):
        flush_log_pipeline(client)
    entries = query_logs_via_mcp(client, token)
    with _CLAIM_LOCK:
        taken = {
            int(e["id"]): e
            for e in entries
            if int(e.get("id") or 0) > after_id
            and int(e.get("id") or 0) not in _CLAIMED_IDS
            and any(text in str(e.get("message")) for text in texts)
        }
        _CLAIMED_IDS.update(taken)
    return list(taken.values())


def record_gate_result(config: pytest.Config, result: Any) -> None:
    """Keep a phase gate's result for ``apply_gate_exit_status``."""
    config.stash.setdefault(_GATE_RESULTS, []).append(result)


def apply_gate_exit_status(session: pytest.Session) -> None:
    """A failed gate makes the session exit nonzero, with its report."""
    failed = [r for r in session.config.stash.get(_GATE_RESULTS, []) if not r.passed]
    if not failed:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    for result in failed:
        if reporter is not None:
            reporter.write_line(result.failure_message(), red=True)
    if session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
