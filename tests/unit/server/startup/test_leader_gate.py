"""Tests for the shared leader gate used by fleet-wide background work.

Two layers, deliberately:

* behavioural tests of :mod:`...startup.leader_gate` itself, and
* AST wiring tests that read ``lifespan.py`` and assert the four destructive /
  shared-state startup sweeps actually go through the gate.

The second layer exists because the first can pass while the gate is wired
nowhere -- which is exactly the failure this change is fixing. It also pins the
ONE sweep deliberately left ungated, so removing that exemption is a conscious
edit rather than a drive-by.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest

from code_indexer.server.startup.leader_gate import (
    await_sweep_leadership,
    resolve_sweep_leader,
)

# ──────────────────────────── resolve_sweep_leader ────────────────────────────


def test_resolve_holds_until_gate_ready() -> None:
    st = SimpleNamespace(scip_reconcile_leader_gate_ready=False, leader_election=None)
    assert resolve_sweep_leader(st) is False


def test_resolve_defers_to_election_once_ready() -> None:
    leader = SimpleNamespace(
        scip_reconcile_leader_gate_ready=True,
        leader_election=SimpleNamespace(is_leader=True),
    )
    follower = SimpleNamespace(
        scip_reconcile_leader_gate_ready=True,
        leader_election=SimpleNamespace(is_leader=False),
    )
    assert resolve_sweep_leader(leader) is True
    assert resolve_sweep_leader(follower) is False


def test_resolve_single_node_without_election_runs() -> None:
    """sqlite / solo mode has no election -- behaviour must be unchanged."""
    st = SimpleNamespace(scip_reconcile_leader_gate_ready=True, leader_election=None)
    assert resolve_sweep_leader(st) is True


def test_resolve_defaults_to_running_when_attrs_absent() -> None:
    assert resolve_sweep_leader(SimpleNamespace()) is True


def test_resolve_fails_open_when_is_leader_raises() -> None:
    """A broken election must not mean the self-heal sweeps never run anywhere.

    Fail-OPEN is the same direction the pre-gate code had (every process ran),
    so this can only ever be as bad as today -- whereas fail-closed would add a
    brand new way for a fleet to stop healing itself, silently.
    """

    class Exploding:
        @property
        def is_leader(self) -> bool:
            raise RuntimeError("election backend down")

    st = SimpleNamespace(
        scip_reconcile_leader_gate_ready=True, leader_election=Exploding()
    )
    assert resolve_sweep_leader(st) is True


# ─────────────────────────── await_sweep_leadership ───────────────────────────


class _State:
    """Minimal app.state stand-in with independently settable flags."""

    def __init__(self, ready: bool, is_leader: bool | None) -> None:
        self.scip_reconcile_leader_gate_ready = ready
        self.leader_election = (
            None if is_leader is None else SimpleNamespace(is_leader=is_leader)
        )


@pytest.mark.asyncio
async def test_await_returns_immediately_for_single_node() -> None:
    """No election -> no waiting at all: solo startup must not get slower."""
    loop = asyncio.get_running_loop()
    before = loop.time()
    assert await await_sweep_leadership(_State(True, None), sweep_name="x") is True
    assert loop.time() - before < 0.5


@pytest.mark.asyncio
async def test_await_returns_true_once_leadership_settles() -> None:
    """The leader pod may evaluate the gate BEFORE it has won the election.

    Without the wait, the pod that is about to become leader skips, every other
    pod skips too, and the sweep runs nowhere.
    """
    st = _State(False, False)

    async def _settle() -> None:
        await asyncio.sleep(0.05)
        st.scip_reconcile_leader_gate_ready = True
        st.leader_election.is_leader = True

    task = asyncio.ensure_future(_settle())
    try:
        assert (
            await await_sweep_leadership(st, sweep_name="x", timeout=5.0, poll=0.01)
            is True
        )
    finally:
        await task


@pytest.mark.asyncio
async def test_await_returns_false_for_a_follower_after_deadline() -> None:
    st = _State(True, False)
    assert (
        await await_sweep_leadership(st, sweep_name="x", timeout=0.05, poll=0.01)
        is False
    )


@pytest.mark.asyncio
async def test_await_checks_once_even_with_zero_timeout() -> None:
    """timeout=0 means 'no waiting', not 'never run'."""
    assert (
        await await_sweep_leadership(_State(True, True), sweep_name="x", timeout=0.0)
        is True
    )
    assert (
        await await_sweep_leadership(_State(True, False), sweep_name="x", timeout=0.0)
        is False
    )


@pytest.mark.asyncio
async def test_await_propagates_cancellation() -> None:
    """Shutdown must not be held up by a follower still polling the gate."""
    task = asyncio.ensure_future(
        await_sweep_leadership(
            _State(True, False), sweep_name="x", timeout=60.0, poll=0.01
        )
    )
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ───────────────────────────── lifespan.py wiring ─────────────────────────────

_LIFESPAN = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "code_indexer"
    / "server"
    / "startup"
    / "lifespan.py"
)

# The startup one-shots that touch SHARED state (NFS trash dirs, `.versioned`
# snapshots, lease files, the golden-repo registry). Each runs once per uvicorn
# WORKER PROCESS, i.e. 12x on a 6-pod/2-worker cluster.
_GATED_SWEEPS = [
    "_run_orphan_sweep",
    "_run_golden_repo_reconcile_sweep",
    "_run_vsr_sweep",
    "_run_legacy_lease_sweep",
]

# Deliberately NOT gated: a bare mkdir of a directory this module's own
# docstring promises exists "before serving traffic". Gating it would trade a
# harmless duplicate mkdir for a chance that the directory never appears.
_UNGATED_BY_DESIGN = "_run_primary_lease_directory_bootstrap"


def _async_defs(name: str) -> List[ast.AsyncFunctionDef]:
    tree = ast.parse(_LIFESPAN.read_text(encoding="utf-8"))
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == name
    ]


def _calls_gate(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        fn: Any = sub.func
        target = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if target in {"await_sweep_leadership", "_await_sweep_leadership"}:
            return True
    return False


@pytest.mark.parametrize("sweep", _GATED_SWEEPS)
def test_startup_sweep_is_leader_gated(sweep: str) -> None:
    defs = _async_defs(sweep)
    assert len(defs) == 1, (
        f"expected exactly one {sweep} in lifespan.py, got {len(defs)}"
    )
    assert _calls_gate(defs[0]), (
        f"{sweep} runs once per uvicorn worker process -- 12x on the tools-* "
        f"cluster -- against shared NFS/registry state. It must go through "
        f"await_sweep_leadership() so exactly one process in the fleet does the work."
    )


@pytest.mark.parametrize("sweep", _GATED_SWEEPS)
def test_gated_sweep_returns_early_when_not_leader(sweep: str) -> None:
    """The gate's result must be ACTED ON, not merely computed and logged."""
    body = _async_defs(sweep)[0]
    returns = [n for n in ast.walk(body) if isinstance(n, ast.Return)]
    assert returns, (
        f"{sweep} calls the leader gate but never returns -- a gate whose "
        f"False branch falls through to the work is decoration, which is the "
        f"exact bug this change fixes elsewhere (ScheduledCatchupService)."
    )


def test_primary_lease_directory_bootstrap_stays_ungated() -> None:
    defs = _async_defs(_UNGATED_BY_DESIGN)
    assert len(defs) == 1
    assert not _calls_gate(defs[0]), (
        f"{_UNGATED_BY_DESIGN} is an idempotent mkdir that must happen on every "
        f"node before it serves traffic. If gating it is now wanted, update this "
        f"test deliberately rather than letting a sweep-gating pass absorb it."
    )
