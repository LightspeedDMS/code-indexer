"""Per-run exact provider-input meter of the ``cidx index`` child (Story #2122).

The semantic indexing child arms ONE process-local meter. The reuse resolver
(``FileChunkingManager``, Story #2119) registers every chunk it found MISSING
from the store this run; the multimodal path registers the inputs it sends
(it has no reuse). Every embedding provider attempt is charged inside
``instrument_call`` BEFORE the HTTP call, so the checks precede the network:

- an input that was never registered is refused (a planning defect);
- attempts or inputs beyond ``sum(registered * (1 + max_retries))`` are
  refused (a retry or resend defect), ``max_retries`` being the sending
  provider's own retry setting at registration time.

A refusal trips the meter for the rest of the run: every later charge is
refused too, so a defective run sends nothing more. The trip raises
``RunSpendDefect``, a ``BaseException`` like ``SystemExit``, so the
provider retry loops and the indexer's generic ``except Exception``
handlers cannot swallow it and retry; the CLI maps it to exit 88
(``index_failure_exit_codes.EXIT_CODE_RUN_SPEND_DEFECT``). The trip is also
sticky state, so a run that swallowed it anyway still exits 88.

Not fail-open: nothing here degrades to "allow". Only purpose ``query``
(the server's query path) and processes that never armed the meter (the
server, ``cidx query``, the temporal child) are outside it.

Keys are ``sha256(text)`` hex, the same digest the resolver stores as a
chunk's ``content_hash``; kept as the 64-bit prefix to bound memory on a
first full index.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence, Set

#: The one purpose never metered: server/query-path embeddings.
UNMETERED_PURPOSE = "query"


class RunSpendDefect(BaseException):
    """The run tried to send provider input it had not planned. Derives from
    ``BaseException`` so provider retry loops and generic handlers cannot
    swallow it; the CLI exits 88 on it."""


@dataclass(frozen=True)
class RunSpendSnapshot:
    inputs_sent: int
    attempts: int
    registered_misses: int
    allowance: int


def input_key(text: str) -> str:
    """The meter's key of one provider input: its sha256 hex digest."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _compact(hex_key: str) -> int:
    return int(hex_key[:16], 16)


class RunSpendMeter:
    """Process-local, thread-safe. One instance per process (see
    ``get_run_spend_meter``)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._armed = False
        self._registered: Set[int] = set()
        self._registered_misses = 0
        self._allowance = 0
        self._attempts = 0
        self._inputs_sent = 0
        self._tripped: Optional[RunSpendDefect] = None

    def arm(self) -> None:
        """Start metering this process's run with zeroed counters."""
        self._reset(armed=True)

    def disarm(self) -> None:
        """End the run completely: unarmed, counters and any trip cleared.
        The CLI calls it after the run's exit code was decided and its final
        record emitted, so nothing leaks into later work in the process."""
        self._reset(armed=False)

    def _reset(self, *, armed: bool) -> None:
        with self._lock:
            self._armed = armed
            self._registered = set()
            self._registered_misses = 0
            self._allowance = 0
            self._attempts = 0
            self._inputs_sent = 0
            self._tripped = None

    def is_armed(self) -> bool:
        with self._lock:
            return self._armed

    @property
    def tripped(self) -> Optional[RunSpendDefect]:
        with self._lock:
            return self._tripped

    def raise_if_tripped(self) -> None:
        defect = self.tripped
        if defect is not None:
            raise defect

    def register_misses(self, keys: Iterable[str], *, max_retries: int) -> None:
        """Register input keys this run must send (resolver misses or the
        multimodal path's inputs). Each one allows ``1 + max_retries``
        attempts. No-op while the meter is not armed."""
        if isinstance(max_retries, bool) or not isinstance(max_retries, int):
            raise TypeError(f"max_retries must be an int, got {max_retries!r}")
        if max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {max_retries}")
        compact = [_compact(key) for key in keys]
        with self._lock:
            if not self._armed:
                return
            self._registered.update(compact)
            self._registered_misses += len(compact)
            self._allowance += len(compact) * (1 + max_retries)

    def charge(self, *, purpose: str, inputs: Optional[Sequence[str]]) -> None:
        """Charge ONE provider attempt carrying ``inputs`` BEFORE it is sent.

        Raises ``RunSpendDefect`` (and trips the meter) when the meter is
        tripped, ``inputs`` is missing, any input is unregistered, or the
        attempt would exceed the allowance."""
        # Unlocked pre-check (re-checked under the lock): query purpose and
        # unarmed processes never pay for hashing.
        if purpose == UNMETERED_PURPOSE or not self._armed:
            return
        # Hash outside the lock so worker threads are not serialized on it.
        keys = None if inputs is None else [_compact(input_key(t)) for t in inputs]
        with self._lock:
            if not self._armed:
                return
            if self._tripped is not None:
                raise self._tripped
            reason = self._refusal_reason(keys)
            if reason is not None:
                self._tripped = RunSpendDefect(reason)
                raise self._tripped
            assert keys is not None  # _refusal_reason refuses None
            self._attempts += 1
            self._inputs_sent += len(keys)

    def _refusal_reason(self, keys: Optional[Sequence[int]]) -> Optional[str]:
        if keys is None:
            return "provider call carried no meterable inputs"
        unregistered = sum(1 for key in keys if key not in self._registered)
        if unregistered:
            return (
                f"{unregistered} of {len(keys)} provider input(s) were not "
                "registered as misses this run"
            )
        if self._attempts + 1 > self._allowance:
            return (
                f"attempt {self._attempts + 1} exceeds the run allowance "
                f"{self._allowance} ({self._registered_misses} misses)"
            )
        if self._inputs_sent + len(keys) > self._allowance:
            return (
                f"{self._inputs_sent + len(keys)} inputs exceed the run "
                f"allowance {self._allowance} ({self._registered_misses} misses)"
            )
        return None

    def snapshot(self) -> RunSpendSnapshot:
        with self._lock:
            return RunSpendSnapshot(
                inputs_sent=self._inputs_sent,
                attempts=self._attempts,
                registered_misses=self._registered_misses,
                allowance=self._allowance,
            )


_METER = RunSpendMeter()


def get_run_spend_meter() -> RunSpendMeter:
    """The process's one meter."""
    return _METER
