"""Honest keyset pages for the Web recovery panel (SQLite AND PostgreSQL):
every quarantined row, open batch and stranded destination is visited
exactly once, and no page ever selects a batch body."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Optional

from code_indexer.server.services.siem_delivery import stats
from code_indexer.server.services.siem_delivery.db import SiemTx

from .backends import SiemBackendHarness

CONFIGURED = "harness:00000000000000aa"
BODY_MARKER = b"BATCH-BODY-MARKER-7f3"


def _seed_queue(
    b: SiemBackendHarness,
    count: int,
    *,
    dest: str = CONFIGURED,
    status: str = "pending",
    batch_id: Optional[str] = None,
    signature: Optional[str] = None,
) -> List[str]:
    uuids = [str(uuid.uuid4()) for _ in range(count)]

    def _do(tx: SiemTx) -> None:
        now = tx.ts(tx.now())
        for i, event_uuid in enumerate(uuids):
            tx.execute(
                "INSERT INTO siem_delivery_queue (event_uuid, destination_key, "
                "occurred_at, action_type, status, batch_id, batch_ordinal, "
                "next_attempt_at, mapping_version, quarantine_reason, "
                "quarantine_signature, created_at) "
                "VALUES (?, ?, ?, 'authentication_failure', ?, ?, ?, ?, 2, ?, ?, ?)",
                (
                    event_uuid,
                    dest,
                    "2026-01-01T00:00:00Z",
                    status,
                    batch_id,
                    i if batch_id else None,
                    now,
                    "rejected" if status == "quarantined" else None,
                    signature,
                    now,
                ),
            )

    b.db.write(_do)
    return uuids


def _seed_batch(
    b: SiemBackendHarness, batch_id: str, seconds: int, dest: str = CONFIGURED
) -> None:
    def _do(tx: SiemTx) -> None:
        created = tx.ts(tx.now() + timedelta(seconds=seconds))
        tx.execute(
            "INSERT INTO siem_delivery_batches (batch_id, destination_key, body, "
            "body_sha256, event_count, mapping_version, state, created_at, "
            "next_attempt_at) VALUES (?, ?, ?, 'x', 3, 2, 'pending_send', ?, ?)",
            (batch_id, dest, BODY_MARKER, created, created),
        )

    b.db.write(_do)


def _destination(b: SiemBackendHarness, key: str) -> None:
    b.db.write(
        lambda tx: tx.execute(
            "INSERT INTO siem_destinations (destination_key, region, project_id, "
            "location, instance_id, first_seen_at) VALUES (?, 'us', ?, 'us', ?, ?)",
            (key, f"project-{key[-3:]}", f"instance-{key[-3:]}", tx.ts(tx.now())),
        )
    )


def _walk(fetch: Any, limit: int) -> List[Dict[str, Any]]:
    """Follow ``next_after`` until ``has_more`` is false (bounded)."""
    seen: List[Dict[str, Any]] = []
    after: Any = None
    for _ in range(limit):
        page = fetch(after)
        seen.extend(page["rows"])
        if not page["has_more"]:
            return seen
        after = page["next_after"]
    raise AssertionError("paging did not terminate")


def test_quarantine_keyset_visits_250_rows_once(
    siem_backend: SiemBackendHarness,
) -> None:
    b = siem_backend
    uuids = _seed_queue(b, 250, status="quarantined", signature="400|BAD|x|-")
    _seed_queue(b, 3, status="pending")
    first = b.db.read(lambda tx: stats.quarantine_page(tx, 0))
    assert len(first["rows"]) == 100 and first["has_more"] is True
    rows = _walk(lambda a: b.db.read(lambda tx: stats.quarantine_page(tx, a or 0)), 10)
    assert sorted(r["event_uuid"] for r in rows) == sorted(uuids)
    assert [r["id"] for r in rows] == sorted(r["id"] for r in rows)
    sample = rows[0]
    assert sample["quarantine_signature"] == "400|BAD|x|-"
    assert sample["created_at"] and "T" in sample["created_at"]


def test_open_batches_keyset_visits_51_once_and_never_the_body(
    siem_backend: SiemBackendHarness,
) -> None:
    b = siem_backend
    ids = [f"batch-{i:03d}" for i in range(51)]
    for i, batch_id in enumerate(ids):
        _seed_batch(b, batch_id, seconds=i)
    first = b.db.read(lambda tx: stats.open_batches_page(tx, None))
    assert len(first["rows"]) == 50 and first["has_more"] is True
    rows = _walk(lambda a: b.db.read(lambda tx: stats.open_batches_page(tx, a)), 5)
    assert [r["batch_id"] for r in rows] == ids
    assert "body" not in rows[0]
    assert BODY_MARKER.decode() not in json.dumps(rows, default=str)
    assert rows[0]["event_count"] == 3 and rows[0]["state"] == "pending_send"


def test_open_batches_tie_on_created_at_break_by_batch_id(
    siem_backend: SiemBackendHarness,
) -> None:
    b = siem_backend
    ids = [f"batch-tie-{i}" for i in range(5)]

    def _same_instant(tx: SiemTx) -> None:
        created = tx.ts(tx.now())  # ONE instant for all five
        for batch_id in reversed(ids):  # insertion order must not matter
            tx.execute(
                "INSERT INTO siem_delivery_batches (batch_id, destination_key, "
                "body_sha256, event_count, mapping_version, state, created_at, "
                "next_attempt_at) VALUES (?, ?, 'x', 1, 2, 'pending_send', ?, ?)",
                (batch_id, CONFIGURED, created, created),
            )

    b.db.write(_same_instant)
    pages: List[List[str]] = []
    after = None
    for _ in range(5):
        page = b.db.read(lambda tx: stats.open_batches_page(tx, after, limit=2))
        pages.append([r["batch_id"] for r in page["rows"]])
        if not page["has_more"]:
            break
        after = page["next_after"]
    assert pages == [ids[0:2], ids[2:4], ids[4:5]]


def test_stranded_keyset_visits_51_keys_once(siem_backend: SiemBackendHarness) -> None:
    b = siem_backend
    keys = [f"harness:{i:016d}" for i in range(1, 52)]
    for key in keys:
        _destination(b, key)
        _seed_queue(b, 1, dest=key)
    _destination(b, CONFIGURED)
    _seed_queue(b, 2, dest=CONFIGURED)
    _destination(b, "harness:9999999999999999")  # nothing undelivered: omitted
    _seed_queue(b, 1, dest=keys[0], status="quarantined")
    rows = _walk(
        lambda a: b.db.read(lambda tx: stats.stranded_page(tx, CONFIGURED, a or "")),
        5,
    )
    assert [r["destination_key"] for r in rows] == keys
    assert rows[0]["has_quarantined"] is True and rows[1]["has_quarantined"] is False
    assert rows[0]["pending"] == 1 and rows[0]["region"] == "us"
    assert rows[0]["project_id"] == f"project-{keys[0][-3:]}"


def test_destination_summary_caps_counts_and_names_the_coordinates(
    siem_backend: SiemBackendHarness,
) -> None:
    b = siem_backend
    key = "harness:0000000000000777"
    _destination(b, key)
    _seed_queue(b, stats.COUNT_CAP + 50, dest=key)
    _seed_queue(b, 2, dest=key, status="batched", batch_id="batch-x")
    _seed_queue(b, 3, dest=key, status="quarantined")
    summary = b.db.read(lambda tx: stats.destination_summary(tx, key))
    assert summary["known"] is True
    assert (summary["region"], summary["project_id"], summary["instance_id"]) == (
        "us",
        "project-777",
        "instance-777",
    )
    assert summary["pending"] == stats.COUNT_CAP + 1 and summary["pending_capped"]
    assert (summary["batched"], summary["quarantined"]) == (2, 3)
    assert not summary["batched_capped"] and not summary["quarantined_capped"]
    [stranded] = b.db.read(lambda tx: stats.stranded_page(tx, CONFIGURED, ""))["rows"]
    assert stranded["pending"] == stats.COUNT_CAP + 1
    assert stranded["has_batched"] is True and stranded["has_quarantined"] is True
    unknown = b.db.read(lambda tx: stats.destination_summary(tx, "harness:none"))
    assert unknown["known"] is False and unknown["pending"] == 0


def test_batch_by_id_reads_one_batch_without_its_body(
    siem_backend: SiemBackendHarness,
) -> None:
    b = siem_backend
    _seed_batch(b, "batch-halted", seconds=5)
    row = b.db.read(lambda tx: stats.batch_by_id(tx, "batch-halted"))
    assert row is not None and row["batch_id"] == "batch-halted"
    assert "body" not in row and row["created_at"] and "T" in row["created_at"]
    assert b.db.read(lambda tx: stats.batch_by_id(tx, "batch-none")) is None


class _Recording(SiemTx):
    """A real transaction that also records every queue/batch SELECT."""

    def __init__(self, tx: SiemTx) -> None:
        super().__init__(tx.conn, tx.dialect)
        self.seen: List[Any] = []

    def query(self, sql: str, params: Any = ()) -> List[Dict[str, Any]]:
        if "siem_delivery_queue" in sql or "siem_delivery_batches" in sql:
            self.seen.append((sql, tuple(params)))
        return super().query(sql, params)


def _plan(tx: SiemTx, sql: str, params: Any) -> str:
    if tx.dialect.name == "postgres":
        tx.execute("SET LOCAL enable_seqscan = off")
        rows = SiemTx.query(tx, f"EXPLAIN {sql}", params)
        return " ".join(str(v) for r in rows for v in r.values())
    rows = SiemTx.query(tx, f"EXPLAIN QUERY PLAN {sql}", params)
    return " ".join(str(r.get("detail")) for r in rows)


def test_stranded_page_reads_the_queue_in_one_bounded_statement(
    siem_backend: SiemBackendHarness,
) -> None:
    b = siem_backend
    keys = [f"harness:{i:016d}" for i in range(1, 6)]
    for key in keys:
        _destination(b, key)
        _seed_queue(b, 400, dest=key)  # realistic backlog per key
    _seed_queue(b, 2, dest=keys[1], status="batched", batch_id="batch-s")
    _seed_queue(b, 1, dest=keys[2], status="quarantined")
    b.raw("ANALYZE siem_delivery_queue")

    def _page(tx: SiemTx) -> Any:
        rec = _Recording(tx)
        page = stats.stranded_page(rec, CONFIGURED, "")
        assert len(rec.seen) == 1, f"{len(rec.seen)} queue statements for one page"
        return page, _plan(tx, *rec.seen[0])

    page, plan = b.db.write(_page)
    rows = {r["destination_key"]: r for r in page["rows"]}
    assert [r["pending"] for r in page["rows"]] == [400] * 5
    assert (
        rows[keys[1]]["has_batched"] is True and rows[keys[0]]["has_batched"] is False
    )
    assert rows[keys[2]]["has_quarantined"] is True
    assert "Seq Scan on siem_delivery_queue" not in plan
    if b.name == "sqlite":
        assert "idx_siem_queue_status_dest_id" in plan, plan


def test_page_queries_use_the_named_indexes(siem_backend: SiemBackendHarness) -> None:
    # realistic statistics: many destinations, a small quarantined fraction
    for i in range(20):
        _seed_queue(siem_backend, 500, dest=f"harness:{i:016d}")
    _seed_queue(siem_backend, 300, status="quarantined")
    siem_backend.raw("ANALYZE siem_delivery_queue")
    siem_backend.raw("ANALYZE siem_delivery_batches")
    # The per-key count may be served by either destination index (the PG
    # planner picks (destination_key, created_at) when one status dominates).
    calls = {
        ("idx_siem_queue_status_id",): lambda t: stats.quarantine_page(t, 0),
        ("idx_siem_batches_state_created",): lambda t: stats.open_batches_page(
            t, "2026-01-01T00:00:00+00:00|batch-a"
        ),
        (
            "idx_siem_queue_status_dest_id",
            "idx_siem_queue_dest_created",
        ): lambda t: stats.destination_summary(t, f"harness:{0:016d}"),
    }

    def _check(tx: SiemTx) -> Dict[Any, str]:
        rec = _Recording(tx)
        plans = {}
        for indexes, call in calls.items():
            start = len(rec.seen)
            call(rec)
            plans[indexes] = _plan(tx, *rec.seen[start])
        return plans

    sqlite = siem_backend.name == "sqlite"
    for indexes, plan in siem_backend.db.write(_check).items():
        accepted = indexes[:1] if sqlite else indexes  # SQLite: the exact index
        assert any(i in plan for i in accepted), (indexes, plan)
        assert "Seq Scan" not in plan and "SCAN siem_delivery_queue " not in plan
