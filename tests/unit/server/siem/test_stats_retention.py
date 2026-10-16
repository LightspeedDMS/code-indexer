"""Bounded stats, boundary-window counting and paced retention."""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from code_indexer.server.services.siem_delivery import retention, stats
from code_indexer.server.services.siem_delivery import state_store

from .backends import SiemBackendHarness

DEST = "harness:00000000000000aa"


def _now(b: SiemBackendHarness) -> datetime:
    return b.db.read(lambda tx: tx.now())


def _row(
    b: SiemBackendHarness,
    status: str,
    created: datetime,
    *,
    delivered: Optional[datetime] = None,
    boundary: Optional[str] = None,
    dest: str = DEST,
) -> None:
    _rows(b, [(status, created, delivered, boundary, dest)])


_ROW_SQL = (
    "INSERT INTO siem_delivery_queue (event_uuid, destination_key, occurred_at, "
    "action_type, event_payload, status, attempts, next_attempt_at, mapping_version, "
    "boundary_kind, created_at, delivered_at) VALUES (?, ?, 'x', 'user_created', '{}', "
    "?, 0, ?, 1, ?, ?, ?)"
)


def _rows(b: SiemBackendHarness, specs: List[Tuple[Any, ...]]) -> None:
    """Insert (status, created, delivered, boundary, dest) rows in ONE
    transaction (one commit, however many rows)."""
    ts = b.db.dialect.ts

    def _do(tx: Any) -> None:
        for status, created, delivered, boundary, dest in specs:
            tx.execute(
                _ROW_SQL,
                (
                    str(uuid.uuid4()),
                    dest,
                    status,
                    ts(created),
                    boundary,
                    ts(created),
                    ts(delivered) if delivered else None,
                ),
            )

    b.db.write(_do)


def test_pending_counts_undelivered_rows_and_caps(
    siem_backend: SiemBackendHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = _now(siem_backend)
    for status in ("pending", "pending", "batched", "quarantined", "delivered"):
        _row(siem_backend, status, now - timedelta(minutes=30))
    counts = siem_backend.db.read(lambda tx: stats.live_counts(tx, DEST))
    assert counts["pending"] == 3 and counts["quarantined"] == 1
    assert counts["oldest_pending_age_seconds"] >= 1700
    assert counts["due_work"] is True
    monkeypatch.setattr(stats, "COUNT_CAP", 1)
    capped = siem_backend.db.read(lambda tx: stats.live_counts(tx, DEST))
    assert capped["pending_capped"] is True


def _register(b: SiemBackendHarness, key: str) -> None:
    b.raw(
        "INSERT INTO siem_destinations (destination_key, first_seen_at) VALUES (?, ?)",
        (key, b.db.dialect.ts(_now(b))),
    )


def test_unconfigured_destination_rows_are_reported(
    siem_backend: SiemBackendHarness,
) -> None:
    now = _now(siem_backend)
    for key in (DEST, "gsecops:00000000000000ff", "gsecops:00000000000000ee"):
        _register(siem_backend, key)  # every captured key was once configured
    _row(siem_backend, "pending", now, dest="gsecops:00000000000000ff")
    _row(siem_backend, "pending", now, dest=DEST)
    counts = siem_backend.db.read(lambda tx: stats.live_counts(tx, DEST))
    assert counts["unconfigured_destinations"] == [
        {"destination_key": "gsecops:00000000000000ff", "pending": 1}
    ]


def test_backlog_estimate_spans_every_destination(
    siem_backend: SiemBackendHarness,
) -> None:
    now = _now(siem_backend)
    _row(siem_backend, "pending", now, dest="harness:zzzzzzzzzzzzzzzz")  # lowest id
    _row(siem_backend, "pending", now, dest="harness:0000000000000000")
    _row(siem_backend, "delivered", now)
    estimate = siem_backend.db.read(stats._backlog_estimate)
    assert estimate == 3


def test_one_refresher_per_interval(siem_backend: SiemBackendHarness) -> None:
    first = stats.maybe_refresh_stats(
        siem_backend.db, refresh_seconds=60, destination_key=DEST
    )
    second = stats.maybe_refresh_stats(
        siem_backend.db, refresh_seconds=60, destination_key=DEST
    )
    assert first is not None and second is None
    snapshot = stats.persisted_snapshot(state_store.read_state(siem_backend.db))
    assert snapshot["pending"] == 0


def test_capture_after_boundary_is_counted_and_late_is_a_defect(
    siem_backend: SiemBackendHarness,
) -> None:
    now = _now(siem_backend)
    boundary = now - timedelta(seconds=600)
    _row(siem_backend, "delivered", boundary, boundary="disable")
    _row(siem_backend, "delivered", boundary + timedelta(seconds=30))
    _row(siem_backend, "pending", boundary + timedelta(seconds=120))
    # captured before the boundary: not counted
    _row(siem_backend, "pending", boundary - timedelta(seconds=5))
    stats.maybe_refresh_stats(siem_backend.db, refresh_seconds=0, destination_key=DEST)
    st = state_store.read_state(siem_backend.db)
    assert st["capture_after_boundary_total"] == 1
    assert st["capture_after_boundary_late_total"] == 1


def _refresh_and_state(b: SiemBackendHarness) -> Dict[str, Any]:
    stats.maybe_refresh_stats(b.db, refresh_seconds=0, destination_key=DEST)
    return state_store.read_state(b.db)


def test_reenable_ends_the_closing_interval(siem_backend: SiemBackendHarness) -> None:
    now = _now(siem_backend)
    t0 = now - timedelta(seconds=900)
    _row(siem_backend, "delivered", t0, boundary="disable")
    _row(siem_backend, "delivered", t0 + timedelta(seconds=30))  # within 90 s
    _row(siem_backend, "delivered", t0 + timedelta(seconds=200), boundary="enable")
    _row(siem_backend, "delivered", t0 + timedelta(seconds=300))  # legitimate
    _row(siem_backend, "delivered", t0 + timedelta(seconds=400))  # legitimate
    st = _refresh_and_state(siem_backend)
    assert st["capture_after_boundary_total"] == 1
    assert st["capture_after_boundary_late_total"] == 0
    st = _refresh_and_state(siem_backend)  # settled: never counted twice
    assert st["capture_after_boundary_total"] == 1
    assert st["capture_after_boundary_late_total"] == 0


def test_late_rows_arriving_after_a_scan_are_still_counted(
    siem_backend: SiemBackendHarness,
) -> None:
    now = _now(siem_backend)
    t1 = now - timedelta(seconds=300)
    _row(siem_backend, "delivered", t1, boundary="disable")
    st = _refresh_and_state(siem_backend)
    assert st["capture_after_boundary_late_total"] == 0
    # a row committed after that scan, captured 150 s after the disable
    _row(siem_backend, "pending", t1 + timedelta(seconds=150))
    st = _refresh_and_state(siem_backend)
    assert st["capture_after_boundary_late_total"] == 1
    st = _refresh_and_state(siem_backend)  # the open window is recounted
    assert st["capture_after_boundary_late_total"] == 1


def test_destination_change_closes_the_other_destinations(
    siem_backend: SiemBackendHarness,
) -> None:
    other = "harness:00000000000000bb"
    for key in (DEST, other):
        siem_backend.raw(
            "INSERT INTO siem_destinations (destination_key, first_seen_at) "
            "VALUES (?, ?)",
            (key, siem_backend.db.dialect.ts(_now(siem_backend))),
        )
    now = _now(siem_backend)
    t0 = now - timedelta(seconds=900)
    _row(siem_backend, "delivered", t0, boundary="destination_change", dest=other)
    _row(siem_backend, "delivered", t0 + timedelta(seconds=10), dest=DEST)  # after
    _row(siem_backend, "pending", t0 + timedelta(seconds=500), dest=DEST)  # late
    _row(siem_backend, "pending", t0 + timedelta(seconds=500), dest=other)  # new dest
    st = _refresh_and_state(siem_backend)
    assert st["capture_after_boundary_total"] == 1
    assert st["capture_after_boundary_late_total"] == 1


def _counts(b: SiemBackendHarness) -> List[Any]:
    return b.db.read(
        lambda tx: tx.query(
            "SELECT status, COUNT(*) AS n FROM siem_delivery_queue "
            "GROUP BY status ORDER BY status"
        )
    )


def test_retention_prunes_only_terminal_rows_in_paced_transactions(
    siem_backend: SiemBackendHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A small per-transaction cap keeps the data small while still forcing
    # several paced rounds (10 + 10 + 5) through the real statements.
    monkeypatch.setattr(retention, "PRUNE_ROWS_PER_TX", 10)
    monkeypatch.setattr(retention, "PRUNE_YIELD_SECONDS", 0.0)
    now = _now(siem_backend)
    old = now - timedelta(days=60)
    specs: List[Tuple[Any, ...]] = [("delivered", old, old, None, DEST)] * 25
    specs.append(("delivered", now, now, None, DEST))
    for status in ("pending", "batched", "quarantined", "abandoned", "unrecoverable"):
        specs.append((status, old, None, None, DEST))
    _rows(siem_backend, specs)
    target = retention._targets(24 * 30)[0]
    first_round = retention._delete_round(siem_backend.db, target)
    assert first_round == retention.PRUNE_ROWS_PER_TX
    retention.prune_terminal(
        siem_backend.db, threading.Event(), audit_retention_hours=24 * 30
    )
    remaining = {r["status"]: int(r["n"]) for r in _counts(siem_backend)}
    assert remaining == {"batched": 1, "delivered": 1, "pending": 1, "quarantined": 1}


@pytest.mark.parametrize("mode", ["sqlite", "postgres"])
def test_retention_fails_loudly_without_the_siem_store(
    tmp_path: Path, mode: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A backend registry without the SIEM store (or PostgreSQL mode with no
    registry) is a wiring defect: reported as a failed table, never patched
    over by opening the SQLite file."""
    from types import SimpleNamespace

    from code_indexer.server.services.data_retention_scheduler import (
        DataRetentionScheduler,
    )

    registries = [SimpleNamespace()] + ([None] if mode == "postgres" else [])
    for registry in registries:
        scheduler = DataRetentionScheduler(
            log_db_path=tmp_path / "logs.db",
            main_db_path=tmp_path / "main.db",
            groups_db_path=tmp_path / "groups.db",
            config_service=None,
            storage_mode=mode,
            backend_registry=registry,
        )
        failed: List[str] = []
        errors: Dict[str, str] = {}
        cfg = SimpleNamespace(audit_logs_retention_hours=720)
        with caplog.at_level(logging.ERROR):
            assert scheduler._safe_prune_siem(cfg, failed, errors) == 0
        assert failed == ["siem_delivery"]
        assert "SIEM delivery store" in errors["siem_delivery"]
        assert not (tmp_path / "groups.db").exists()


def test_sqlite_retention_without_siem_tables_is_a_no_op(tmp_path: Path) -> None:
    """SQLite mode without a registry cleans each table in its own file, and
    a table that does not exist there has nothing to clean (as for every
    other table).  It must neither fail the cycle nor create groups.db."""
    import sqlite3
    from types import SimpleNamespace

    from code_indexer.server.services.audit_log_service import AuditLogService
    from code_indexer.server.services.data_retention_scheduler import (
        DataRetentionScheduler,
    )

    groups = tmp_path / "groups.db"

    def _prune() -> Tuple[int, List[str]]:
        scheduler = DataRetentionScheduler(
            log_db_path=tmp_path / "logs.db",
            main_db_path=tmp_path / "main.db",
            groups_db_path=groups,
            config_service=None,
        )
        failed: List[str] = []
        cfg = SimpleNamespace(audit_logs_retention_hours=720)
        return scheduler._safe_prune_siem(cfg, failed, {}), failed

    assert _prune() == (0, [])
    assert not groups.exists()  # never created by the prune
    sqlite3.connect(str(groups)).close()  # a groups.db without SIEM tables
    assert _prune() == (0, [])
    AuditLogService(groups)  # creates the SIEM tables beside audit_logs
    conn = sqlite3.connect(str(groups))
    try:
        conn.execute(
            "INSERT INTO siem_delivery_queue (event_uuid, destination_key, "
            "occurred_at, action_type, status, attempts, next_attempt_at, "
            "mapping_version, created_at, delivered_at) VALUES ('u', 'd', 'x', "
            "'a', 'delivered', 0, '2000-01-01T00:00:00.000Z', 1, "
            "'2000-01-01T00:00:00.000Z', '2000-01-01T00:00:00.000Z')"
        )
        conn.commit()
    finally:
        conn.close()
    assert _prune() == (1, [])


_PLANS = (
    (
        "siem_delivery_queue",
        "id",
        "status",
        "delivered",
        "delivered_at",
        "idx_siem_queue_status_delivered",
    ),
    (
        "siem_delivery_queue",
        "id",
        "status",
        "abandoned",
        "created_at",
        "idx_siem_queue_status_created",
    ),
    (
        "siem_delivery_batches",
        "batch_id",
        "state",
        "delivered",
        "created_at",
        "idx_siem_batches_state_created",
    ),
)


def _bulk_queue(b: SiemBackendHarness) -> None:
    """A realistic queue: almost every row is delivered, a few are pending."""
    b.raw(
        "INSERT INTO siem_delivery_queue (event_uuid, destination_key, occurred_at, "
        "action_type, status, attempts, next_attempt_at, mapping_version, created_at, "
        "delivered_at) SELECT gen_random_uuid()::text, "
        "(ARRAY['d1','d2','d3'])[1 + g % 3], 'x', 'a', "
        "CASE WHEN g % 1000 = 0 THEN 'pending' ELSE 'delivered' END, 0, "
        "now() - (g || ' seconds')::interval, 1, now() - (g || ' seconds')::interval, "
        "now() FROM generate_series(1, 30000) g"
    )
    b.raw("ANALYZE siem_delivery_queue")


def _plan(b: SiemBackendHarness, sql: str, params: tuple) -> str:
    if b.name == "sqlite":
        return b.db.read(
            lambda tx: " ".join(
                str(r.get("detail"))
                for r in tx.query(f"EXPLAIN QUERY PLAN {sql}", params)
            )
        )
    rows = b.db.read(lambda tx: tx.query(f"EXPLAIN {sql}", params))
    return " ".join(str(list(r.values())[0]) for r in rows)


def test_min_id_is_an_index_seek(siem_backend: SiemBackendHarness) -> None:
    if siem_backend.name == "postgres":
        _bulk_queue(siem_backend)
    text = _plan(siem_backend, stats.MIN_ID_SQL, ("pending",))
    assert "idx_siem_queue_status_id" in text, text


def test_candidate_read_walks_an_index_without_sorting(
    siem_backend: SiemBackendHarness,
) -> None:
    from code_indexer.server.services.siem_delivery.claim import CANDIDATE_SQL

    if siem_backend.name == "postgres":
        _bulk_queue(siem_backend)
    params = ("d1", siem_backend.db.dialect.ts(_now(siem_backend)), 100)
    text = _plan(siem_backend, CANDIDATE_SQL, params)
    assert "idx_siem_queue_status_dest_id" in text, text
    assert "TEMP B-TREE" not in text.upper(), text
    assert "Sort" not in text, text


@pytest.mark.parametrize("plan", _PLANS, ids=[p[5] for p in _PLANS])
def test_retention_statements_are_index_served(
    siem_backend: SiemBackendHarness, plan: Any
) -> None:
    table, key, col, value, cutoff, index = plan
    inner = (
        f"SELECT {key} FROM {table} WHERE {col} = ? AND {cutoff} < ? "
        f"ORDER BY {cutoff} LIMIT 500"
    )
    if siem_backend.name == "sqlite":
        text = siem_backend.db.read(
            lambda tx: " ".join(
                str(r.get("detail"))
                for r in tx.query(f"EXPLAIN QUERY PLAN {inner}", (value, "2026"))
            )
        )
        uses_index = (
            f"USING INDEX {index}" in text or f"USING COVERING INDEX {index}" in text
        )
        assert uses_index, text
        assert "SCAN" not in text.replace(f"INDEX {index}", ""), text
        return
    if table == "siem_delivery_queue":
        siem_backend.raw(
            "INSERT INTO siem_delivery_queue (event_uuid, destination_key, occurred_at, "
            "action_type, status, attempts, next_attempt_at, mapping_version, created_at, "
            "delivered_at) SELECT gen_random_uuid()::text, 'd', 'x', 'a', "
            "(ARRAY['delivered','abandoned','pending'])[1 + g % 3], 0, now(), 1, "
            "now() - (g || ' seconds')::interval, now() - (g || ' seconds')::interval "
            "FROM generate_series(1, 30000) g"
        )
    else:
        siem_backend.raw(
            "INSERT INTO siem_delivery_batches (batch_id, destination_key, body_sha256, "
            "event_count, mapping_version, state, created_at, next_attempt_at) "
            "SELECT gen_random_uuid()::text, 'd', 'h', 1, 1, "
            "(ARRAY['delivered','split','pending_send'])[1 + g % 3], "
            "now() - (g || ' seconds')::interval, now() FROM generate_series(1, 30000) g"
        )
    siem_backend.raw(f"ANALYZE {table}")
    cutoff_value = datetime.now(timezone.utc) - timedelta(seconds=29_500)
    rows = siem_backend.db.read(
        lambda tx: tx.query(f"EXPLAIN {inner}", (value, cutoff_value))
    )
    text = " ".join(str(list(r.values())[0]) for r in rows)
    assert index in text, text
