"""Spawn one ``cidx`` child and optionally interrupt its process group.

The child runs in its own session/process group (as the server's
``run_with_popen_progress`` / ``run_with_cancel`` start it), and an
interruption signals the WHOLE group, as systemd's control-group kill and the
server's shutdown do.

Interruption kinds:

* ``SIGTERM``  -- SIGTERM, SIGKILL after ``TERM_GRACE_SECONDS``;
* ``SIGKILL``  -- SIGKILL;
* ``CANCEL``   -- the server's job cancel: SIGTERM, SIGKILL after
  ``CANCEL_GRACE_SECONDS`` (``utils/process_group.SIGTERM_GRACE_SECONDS``
  today; S20 raises it for leased children -- update it here then);
* ``RESTART``  -- a manual ``systemctl restart``: SIGTERM to the cgroup,
  SIGKILL after systemd's default stop timeout ``RESTART_GRACE_SECONDS``.

Interruption points:

* ``early:<seconds>``    -- seconds after spawn (listing / hash pass);
* ``planned:<seconds>``  -- seconds after the caller's ``planned_probe``
  first reports the run has written its plan (status in_progress);
* ``chunks:<count>``     -- once the fake provider has received ``count``
  embedding inputs during this run (mid-embedding);
* ``inflight:<count>``   -- once ``count`` requests are held unanswered by
  the fake (the run must hold responses; deterministic R-d);
* ``final:<seconds>``    -- seconds after the caller's ``finalize_probe``
  reports every planned file processed (finalization).

Just before signalling, the kill instant and the requests still unanswered
are recorded in the provider-boundary ledger.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from fake_voyage_server import EmbeddingLedger

#: How long a SIGTERM-ed group may take to exit before SIGKILL escalation.
TERM_GRACE_SECONDS = 60.0
CANCEL_GRACE_SECONDS = 2.0
RESTART_GRACE_SECONDS = 90.0
_KINDS = {
    "SIGTERM": (signal.SIGTERM, TERM_GRACE_SECONDS),
    "SIGKILL": (signal.SIGKILL, TERM_GRACE_SECONDS),
    "CANCEL": (signal.SIGTERM, CANCEL_GRACE_SECONDS),
    "RESTART": (signal.SIGTERM, RESTART_GRACE_SECONDS),
}
_TRIGGERS = ("early", "planned", "chunks", "inflight", "final")
_COUNT_TRIGGERS = ("chunks", "inflight")
_PROBE_POLL_SECONDS = 0.1
#: How long to wait for every member of the group to disappear.
GROUP_EXIT_SECONDS = 30
_POLL_SECONDS = 0.5


@dataclass(frozen=True)
class InterruptSpec:
    signum: int
    trigger: str
    value: float
    name: str = "SIGTERM"
    grace: float = TERM_GRACE_SECONDS

    @classmethod
    def parse(cls, text: str) -> "InterruptSpec":
        try:
            kind, rest = text.split("@", 1)
            trigger, raw_value = rest.split(":", 1)
            value = float(raw_value)
        except ValueError as exc:
            raise ValueError(
                f"bad interrupt spec {text!r}; want SIGTERM@chunks:2000"
            ) from exc
        if kind not in _KINDS or trigger not in _TRIGGERS or value < 0:
            raise ValueError(f"bad interrupt spec {text!r}")
        if trigger in _COUNT_TRIGGERS:
            value = int(value)
        signum, grace = _KINDS[kind]
        return cls(signum, trigger, value, kind, grace)

    def __str__(self) -> str:
        value = int(self.value) if self.trigger in _COUNT_TRIGGERS else self.value
        return f"{self.name}@{self.trigger}:{value}"


@dataclass
class ChildResult:
    exit_code: int
    duration_s: float
    interrupted: bool
    inputs_at_interrupt: Optional[int]
    note: str
    unanswered_at_kill: Set[int] = field(default_factory=set)


def _wait_for_group_exit(pgid: int) -> None:
    deadline = time.monotonic() + GROUP_EXIT_SECONDS
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        time.sleep(_POLL_SECONDS)
    raise RuntimeError(
        f"process group {pgid} still alive {GROUP_EXIT_SECONDS}s after exit"
    )


def _signal_group(proc: "subprocess.Popen[bytes]", spec: InterruptSpec) -> str:
    os.killpg(proc.pid, spec.signum)
    try:
        proc.wait(timeout=spec.grace)
        return ""
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=TERM_GRACE_SECONDS)
        return f"; escalated to SIGKILL after {spec.grace}s"


def _await_probe(
    proc: "subprocess.Popen[bytes]",
    spec: InterruptSpec,
    deadline: float,
    probe: Callable[[], bool],
) -> bool:
    while not probe():
        if proc.poll() is not None:
            return False
        if time.monotonic() >= deadline:
            raise TimeoutError(f"trigger {spec} not reached before the run deadline")
        time.sleep(_PROBE_POLL_SECONDS)
    try:
        proc.wait(timeout=spec.value)
        return False
    except subprocess.TimeoutExpired:
        return True


def _await_trigger(
    proc: "subprocess.Popen[bytes]",
    ledger: EmbeddingLedger,
    spec: InterruptSpec,
    deadline: float,
    probes: Dict[str, Optional[Callable[[], bool]]],
) -> bool:
    """True when the trigger fired while the child was alive."""
    if spec.trigger in ("planned", "final"):
        probe = probes[spec.trigger]
        if probe is None:
            raise ValueError(f"a {spec.trigger} trigger needs a {spec.trigger} probe")
        return _await_probe(proc, spec, deadline, probe)
    if spec.trigger == "early":
        if spec.value > deadline - time.monotonic():
            raise TimeoutError(f"trigger {spec} is later than the run deadline")
        try:
            proc.wait(timeout=spec.value)
            return False
        except subprocess.TimeoutExpired:
            return True
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        if spec.trigger == "chunks":
            reached = ledger.wait_for_run_inputs(int(spec.value), timeout=_POLL_SECONDS)
        else:
            reached = ledger.wait_for_held(int(spec.value), timeout=_POLL_SECONDS)
        if reached:
            return proc.poll() is None
    raise TimeoutError(f"trigger {spec} not reached before the run deadline")


def run_child(
    argv: List[str],
    cwd: Path,
    env: Dict[str, str],
    ledger: EmbeddingLedger,
    log_prefix: Path,
    interrupt: Optional[InterruptSpec],
    max_seconds: float,
    planned_probe: Optional[Callable[[], bool]] = None,
    finalize_probe: Optional[Callable[[], bool]] = None,
) -> ChildResult:
    probes = {"planned": planned_probe, "final": finalize_probe}
    if (
        interrupt is not None
        and interrupt.trigger in probes
        and probes[interrupt.trigger] is None
    ):
        raise ValueError(
            f"a {interrupt.trigger} trigger needs a {interrupt.trigger}_probe"
        )
    start = time.monotonic()
    deadline = start + max_seconds
    unanswered: Set[int] = set()
    with open(f"{log_prefix}.out", "wb") as out, open(f"{log_prefix}.err", "wb") as err:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdout=out, stderr=err, start_new_session=True
        )
        try:
            interrupted = False
            inputs_at_interrupt = None
            note = "ran to completion"
            if interrupt is not None:
                if _await_trigger(proc, ledger, interrupt, deadline, probes):
                    inputs_at_interrupt = ledger.current_run_inputs()
                    unanswered = ledger.mark_kill(ledger.current_label())
                    note = f"{interrupt} sent" + _signal_group(proc, interrupt)
                    interrupted = True
                else:
                    note = f"exited before {interrupt} fired"
            proc.wait(timeout=max(1.0, deadline - time.monotonic()))
        except BaseException:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            raise
    _wait_for_group_exit(proc.pid)
    return ChildResult(
        exit_code=proc.returncode,
        duration_s=round(time.monotonic() - start, 1),
        interrupted=interrupted,
        inputs_at_interrupt=inputs_at_interrupt,
        note=note,
        unanswered_at_kill=unanswered,
    )
