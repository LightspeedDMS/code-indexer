"""Completion is fenced: a lease takeover that lands between the HTTP reply
and the completion write must never leave queue rows recorded as delivered
while their batch belongs to a new owner (SQLite and PostgreSQL)."""

from __future__ import annotations

import dataclasses
import threading
from typing import Any, Callable, List, Optional

import pytest

from code_indexer.server.services.siem_delivery.claim import (
    Claim,
    EngineContext,
    claim_batch,
    claim_specific,
)
from code_indexer.server.services.siem_delivery.classifier import (
    ACCEPTED,
    CREDENTIAL,
    DUPLICATE_RESPONSE,
    ROW_REJECTION,
    Classification,
)
from code_indexer.server.services.siem_delivery.completion import complete
from code_indexer.server.services.siem_delivery.db import SiemTx

from .backends import SiemBackendHarness
from .test_engine import _capture, engine  # noqa: F401  (fixture re-export)

_TAKEOVER_WAIT_SECONDS = 1.5


class _HookTx:
    """A SiemTx that runs *hook* once, right after its first statement."""

    def __init__(self, tx: SiemTx, hook: Callable[[], None]) -> None:
        self._tx = tx
        self._hook: Optional[Callable[[], None]] = hook

    def _after(self, result: Any) -> Any:
        hook, self._hook = self._hook, None
        if hook is not None:
            hook()
        return result

    def execute(self, *a: Any, **k: Any) -> Any:
        return self._after(self._tx.execute(*a, **k))

    def query(self, *a: Any, **k: Any) -> Any:
        return self._after(self._tx.query(*a, **k))

    def one(self, *a: Any, **k: Any) -> Any:
        return self._after(self._tx.one(*a, **k))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tx, name)


class _HookDb:
    def __init__(self, inner: Any, hook: Callable[[], None]) -> None:
        self._inner = inner
        self._hook = hook
        self.dialect = inner.dialect
        self.groups_db_path = inner.groups_db_path

    def write(self, fn: Callable[[Any], Any], *, phase: str = "write") -> Any:
        return self._inner.write(lambda tx: fn(_HookTx(tx, self._hook)), phase=phase)

    def read(self, fn: Callable[[Any], Any]) -> Any:
        return self._inner.read(fn)


def _expire_lease(b: SiemBackendHarness, batch_id: str) -> None:
    past = "2000-01-01T00:00:00.000Z" if b.name == "sqlite" else "2000-01-01T00:00:00Z"
    b.raw(
        "UPDATE siem_delivery_batches SET lease_expires_at = ? WHERE batch_id = ?",
        (past, batch_id),
    )


def _complete_with_takeover(
    engine: EngineContext,  # noqa: F811
    b: SiemBackendHarness,
    claim: Claim,
    cls: Classification,
) -> tuple:
    """Run complete(); right after its first statement a second owner tries
    to take the (expired) lease from another thread."""
    _expire_lease(b, claim.batch_id)
    taken: List[Optional[Claim]] = []
    errors: List[BaseException] = []
    threads: List[threading.Thread] = []

    def _takeover() -> None:
        try:
            taken.append(claim_specific(engine, claim.batch_id))
        except BaseException as exc:  # noqa: BLE001 - reported by the test
            errors.append(exc)

    def _hook() -> None:
        t = threading.Thread(target=_takeover)
        threads.append(t)
        t.start()
        t.join(_TAKEOVER_WAIT_SECONDS)  # blocks if completion holds the lock

    hooked = dataclasses.replace(engine, db=_HookDb(engine.db, _hook))  # type: ignore[arg-type]
    completed = complete(hooked, claim, cls)
    for t in threads:
        t.join(30)
    assert errors == [], errors  # e.g. a deadlock aborting the claimer
    assert len(taken) == 1
    return completed, taken[0]


def _batch(b: SiemBackendHarness, batch_id: str) -> Any:
    return b.db.read(
        lambda tx: tx.one(
            "SELECT state, lease_token FROM siem_delivery_batches WHERE batch_id = ?",
            (batch_id,),
        )
    )


def _queue(b: SiemBackendHarness) -> List[str]:
    rows = b.db.read(
        lambda tx: tx.query("SELECT status FROM siem_delivery_queue ORDER BY id")
    )
    return [r["status"] for r in rows]


def test_accepted_completion_and_takeover_never_both_win(
    engine: EngineContext,  # noqa: F811
    siem_backend: SiemBackendHarness,
) -> None:
    _capture(siem_backend, 3)
    claim = claim_batch(engine)
    assert claim is not None
    completed, takeover = _complete_with_takeover(
        engine, siem_backend, claim, Classification(ACCEPTED, "200|OK|accepted|")
    )
    delivered_total = siem_backend.count(
        "SELECT delivered_total FROM siem_delivery_state WHERE id = 1"
    )
    batch = _batch(siem_backend, claim.batch_id)
    if completed:
        assert takeover is None
        assert batch["state"] == "delivered"
        assert _queue(siem_backend) == ["delivered"] * 3
        assert delivered_total == 3
    else:
        assert takeover is not None
        assert batch["state"] == "pending_send"
        assert _queue(siem_backend) == ["batched"] * 3
        assert delivered_total == 0


@pytest.mark.parametrize(
    "cls",
    [
        Classification(DUPLICATE_RESPONSE, "409|ALREADY_EXISTS|duplicate_response|"),
        Classification(CREDENTIAL, "401|UNAUTHENTICATED|credential|"),
    ],
    ids=["duplicate", "credential"],
)
def test_systemic_completion_and_concurrent_claim_never_deadlock(
    engine: EngineContext,  # noqa: F811
    siem_backend: SiemBackendHarness,
    cls: Classification,
) -> None:
    """The systemic completion halts (state row) and releases the batch: it
    must take the state row lock FIRST, like every claimer, or a concurrent
    claim deadlocks it on PostgreSQL and the halt is lost."""
    _capture(siem_backend, 2)
    claim = claim_batch(engine)
    assert claim is not None
    completed, _takeover = _complete_with_takeover(engine, siem_backend, claim, cls)
    assert completed is True
    state = siem_backend.db.read(lambda tx: tx.one("SELECT * FROM siem_delivery_state"))
    assert state is not None
    assert state["halted_class"] == cls.cls
    assert state["halted_batch_id"] == claim.batch_id
    assert _queue(siem_backend) == ["batched"] * 2


@pytest.mark.parametrize("indices", [[1], None])
def test_rejection_completion_and_takeover_never_both_win(
    engine: EngineContext,  # noqa: F811
    siem_backend: SiemBackendHarness,
    indices: Optional[List[int]],
) -> None:
    _capture(siem_backend, 4)
    claim = claim_batch(engine)
    assert claim is not None
    cls = Classification(ROW_REJECTION, "400|INVALID_ARGUMENT|row|x", indices=indices)
    completed, takeover = _complete_with_takeover(engine, siem_backend, claim, cls)
    batch = _batch(siem_backend, claim.batch_id)
    batches = siem_backend.count("SELECT COUNT(*) AS n FROM siem_delivery_batches")
    if completed:
        assert takeover is None
        assert batch["state"] == "split"
        assert batches > 1
    else:
        assert takeover is not None
        assert batch["state"] == "pending_send"
        assert _queue(siem_backend) == ["batched"] * 4
        assert batches == 1
