"""Expected log entries claimed by tests, and the session-level gate outcome.

Claims: a test that deliberately provokes a diagnostic (a refused request, a
revoked handle) opens a ``LogMark`` before its calls, sends every call with
its own correlation id, and afterwards claims the COMPLETE entries it
expects: each ``LogClaim`` names the logger and a full-message pattern.  An
entry is claimed only inside the call's own window: id above the mark,
created between the mark and the claim, and carrying one of the window's
correlation ids (``same_request``) or, for diagnostics written off the
request thread, either none of them or no correlation id at all.  The phase
gate (``log_audit_gate.run_log_audit_gate``) skips claimed ids only, so the
same text logged by any other request is still audited.  The registry is
per process: every phase runs its server tests in one pytest process.

Session outcome: a phase's gate fixture records its result on the pytest
config; ``apply_gate_exit_status`` (called from each phase conftest's
``pytest_sessionfinish``) makes a failed gate a nonzero session exit status
and prints the report, whatever the outcome of the last test (a teardown
failure of an xfail-marked last test is otherwise reported as xfailed).
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

import pytest

CLAIM_TIMEOUT_SECONDS = 20.0
CLAIM_POLL_SECONDS = 0.3
CLAIM_SETTLE_SECONDS = 1.5
# A logged traceback is appended to the message it explains.
_TRACEBACK_SUFFIX = r"(?:\nTraceback \(most recent call last\):\n[\s\S]*)?"

_CLAIMED_IDS: Set[int] = set()
_CLAIM_LOCK = threading.Lock()
_GATE_RESULTS = pytest.StashKey[List[Any]]()


@dataclass(frozen=True)
class LogClaim:
    """One expected entry: logger ``source`` and a full-message regex.

    ``same_request``: the entry must carry a correlation id of the window's
    own requests; otherwise an entry with no correlation id (logged off the
    request thread) is accepted too."""

    source: str
    message: str
    same_request: bool = True

    def matches(self, entry: Dict[str, Any]) -> bool:
        return entry.get("source") == self.source and bool(
            re.fullmatch(self.message + _TRACEBACK_SUFFIX, str(entry.get("message")))
        )


def exact(source: str, message: str, same_request: bool = True) -> LogClaim:
    return LogClaim(source, re.escape(message), same_request)


def pattern(source: str, regex: str, same_request: bool = True) -> LogClaim:
    return LogClaim(source, regex, same_request)


@dataclass(frozen=True)
class LogMark:
    """Opened before a call: the newest audited log id and the time."""

    after_id: int
    started: datetime


@dataclass(frozen=True)
class LogWindow:
    after_id: int
    started: datetime
    ended: datetime
    correlation_ids: FrozenSet[str]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _entry_time(entry: Dict[str, Any]) -> Optional[datetime]:
    raw = str(entry.get("timestamp") or "")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _in_window(entry: Dict[str, Any], claim: LogClaim, window: LogWindow) -> bool:
    if int(entry.get("id") or 0) <= window.after_id:
        return False
    created = _entry_time(entry)
    if created is None or not window.started <= created <= window.ended:
        return False
    correlation = entry.get("correlation_id")
    if correlation in window.correlation_ids:
        return True
    return not claim.same_request and not correlation


def match_claims(
    entries: Sequence[Dict[str, Any]],
    claims: Sequence[LogClaim],
    window: LogWindow,
    already_claimed: FrozenSet[int],
) -> Tuple[Dict[int, Dict[str, Any]], List[LogClaim]]:
    """One distinct unclaimed entry inside *window* per claim; the claims
    left without one are returned as missing."""
    taken: Dict[int, Dict[str, Any]] = {}
    missing: List[LogClaim] = []
    for claim in claims:
        match = next(
            (
                e
                for e in entries
                if int(e.get("id") or 0) not in taken
                and int(e.get("id") or 0) not in already_claimed
                and claim.matches(e)
                and _in_window(e, claim, window)
            ),
            None,
        )
        if match is None:
            missing.append(claim)
        else:
            taken[int(match["id"])] = match
    return taken, missing


def claimed_ids() -> FrozenSet[int]:
    with _CLAIM_LOCK:
        return frozenset(_CLAIMED_IDS)


def _logs(client: Any, token: str) -> List[Dict[str, Any]]:
    from tests.e2e.log_audit_gate import flush_log_pipeline, query_logs_via_mcp

    if hasattr(client, "app"):
        flush_log_pipeline(client)
    return query_logs_via_mcp(client, token)


def open_log_mark(client: Any, token: str) -> LogMark:
    """Mark the start of a call window (claims only look after it)."""
    entries = _logs(client, token)
    newest = max((int(e.get("id") or 0) for e in entries), default=0)
    return LogMark(newest, utc_now())


def claim_expected_entries(
    client: Any, token: str, window: LogWindow, claims: Sequence[LogClaim]
) -> List[Dict[str, Any]]:
    """Claim one entry inside *window* per claim in *claims*.

    Raises AssertionError naming the claims with no matching entry once
    ``CLAIM_TIMEOUT_SECONDS`` have passed.
    """
    if not claims:
        return []
    deadline = time.monotonic() + CLAIM_TIMEOUT_SECONDS
    while True:
        entries = _logs(client, token)
        with _CLAIM_LOCK:
            taken, missing = match_claims(
                entries, claims, window, frozenset(_CLAIMED_IDS)
            )
            if not missing:
                _CLAIMED_IDS.update(taken)
                return list(taken.values())
        if time.monotonic() >= deadline:
            raise AssertionError(f"expected log entries not found: {missing}")
        time.sleep(CLAIM_POLL_SECONDS)


def claim_present_entries(
    client: Any, token: str, window: LogWindow, claims: Sequence[LogClaim]
) -> List[Dict[str, Any]]:
    """Claim every entry inside *window* matching one of *claims*, present or
    not (diagnostics a call may log asynchronously; the caller waits for
    late writes before closing the window)."""
    if not claims:
        return []
    entries = _logs(client, token)
    with _CLAIM_LOCK:
        taken = {
            int(e["id"]): e
            for e in entries
            if int(e.get("id") or 0) not in _CLAIMED_IDS
            and any(c.matches(e) and _in_window(e, c, window) for c in claims)
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
