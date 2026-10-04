"""Honest keyset pages for the Web recovery panel (SQLite AND PostgreSQL):
every quarantined row, open batch and stranded destination is visited
exactly once, and no page ever selects a batch body."""

from __future__ import annotations

import json
import re
import uuid
from datetime import timedelta
from typing import Any, Dict, Iterator, List, Optional

import pytest

from code_indexer.server.services.siem_delivery import stats
from code_indexer.server.services.siem_delivery.db import SiemTx
from code_indexer.server.storage.json_column import parse_json_column

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
    at_cap = "harness:0000000000000776"  # pages before the over-cap key
    _destination(b, at_cap)
    _seed_queue(b, stats.COUNT_CAP, dest=at_cap)
    exact, stranded = b.db.read(lambda tx: stats.stranded_page(tx, CONFIGURED, ""))[
        "rows"
    ]
    assert exact["destination_key"] == at_cap
    assert exact["pending"] == stats.COUNT_CAP  # exact AT the cap
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
    """The query plan as text.  PostgreSQL runs with sequential scans
    disabled, so this proves an index is USABLE, not that the production
    planner picks it (that is :func:`_pg_plan_nodes`)."""
    if tx.dialect.name == "postgres":
        tx.execute("SET LOCAL enable_seqscan = off")
        rows = SiemTx.query(tx, f"EXPLAIN {sql}", params)
        return " ".join(str(v) for r in rows for v in r.values())
    rows = SiemTx.query(tx, f"EXPLAIN QUERY PLAN {sql}", params)
    return " ".join(str(r.get("detail")) for r in rows)


# a full queue scan: SQLite (old and new detail formats, table or alias q)
# or PostgreSQL.  NOT the scan of the capped count's derived table
# ("SCAN SUBQUERY 1 AS c" / "SCAN c"): that reads the LIMITed result only.
_QUEUE_SCAN = re.compile(
    r"\bSCAN (?:TABLE )?(?:siem_delivery_queue|q)\b|Seq Scan on siem_delivery_queue\b"
)


def test_queue_scan_pattern_matches_only_real_queue_scans() -> None:
    for real in (
        "SCAN TABLE siem_delivery_queue",
        "SCAN q",
        "Seq Scan on siem_delivery_queue",
    ):
        assert _QUEUE_SCAN.search(real), real
    for derived in ("SCAN SUBQUERY 1 AS c", "SCAN c"):
        assert not _QUEUE_SCAN.search(derived), derived


def _settle_statistics(b: SiemBackendHarness) -> None:
    """Fresh statistics.  PostgreSQL also VACUUMs: autovacuum keeps a live
    queue's visibility map set, which is what makes the planner prefer
    index-only scans; a never-vacuumed table is not representative."""
    if b.name == "sqlite":
        b.raw("ANALYZE siem_delivery_queue")
        return
    with b.pool.connection() as conn:
        conn.autocommit = True  # VACUUM cannot run in a transaction
        try:
            conn.execute("VACUUM ANALYZE siem_delivery_queue")
        finally:
            conn.autocommit = False


def _pg_plan_root(
    tx: SiemTx, sql: str, params: Any, options: str = "FORMAT JSON"
) -> Dict[str, Any]:
    """The PRODUCTION planner's plan tree (no overrides)."""
    row = SiemTx.query(tx, f"EXPLAIN ({options}) {sql}", params)[0]
    explained = parse_json_column(next(iter(row.values())), list, "plan")
    assert explained, "EXPLAIN returned no plan"
    root: Dict[str, Any] = explained[0]["Plan"]
    return root


def _flatten(node: Dict[str, Any]) -> List[Dict[str, Any]]:
    stack = [node]
    nodes: List[Dict[str, Any]] = []
    while stack:  # a finite tree: each node is visited once
        current = stack.pop()
        nodes.append(current)
        stack.extend(current.get("Plans", []))
    return nodes


def _pg_plan_nodes(tx: SiemTx, sql: str, params: Any) -> List[Dict[str, Any]]:
    """Every node of the PRODUCTION planner's plan (no overrides)."""
    return _flatten(_pg_plan_root(tx, sql, params))


def _capped_probes(root: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The Limit node(s) of the per-key subplans (the capped pending count),
    at any depth under the subplan root."""
    return [
        node
        for sub in _flatten(root)
        if sub.get("Subplan Name")
        for node in _flatten(sub)
        if node["Node Type"] == "Limit"
    ]


# the nodes that read queue rows (the bitmap INDEX scan only collects TIDs)
_HEAP_SIDE = ("Bitmap Heap Scan", "Index Scan", "Index Only Scan", "Seq Scan")


def _node_label(node: Dict[str, Any]) -> str:
    return f"{node['Node Type']} on {node.get('Relation Name')}"


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
    _settle_statistics(b)

    def _page(tx: SiemTx) -> Any:
        rec = _Recording(tx)
        page = stats.stranded_page(rec, CONFIGURED, "")
        assert len(rec.seen) == 1, f"{len(rec.seen)} queue statements for one page"
        return page, rec.seen[0]

    page, statement = b.db.read(_page)
    rows = {r["destination_key"]: r for r in page["rows"]}
    assert [r["pending"] for r in page["rows"]] == [400] * 5
    assert (
        rows[keys[1]]["has_batched"] is True and rows[keys[0]]["has_batched"] is False
    )
    assert rows[keys[2]]["has_quarantined"] is True
    # Per key the work is bounded by the cap: the capped probe reads the
    # (status, destination_key, id) index IN id ORDER, so LIMIT 1 OFFSET cap
    # stops after cap + 1 entries.  A sort would read the whole backlog first.
    if b.name == "sqlite":
        plan = b.db.read(lambda tx: _plan(tx, *statement))
        assert "COVERING INDEX idx_siem_queue_status_dest_id" in plan, plan
        assert not _QUEUE_SCAN.search(plan) and "TEMP B-TREE" not in plan, plan
        return
    nodes = b.db.read(lambda tx: _pg_plan_nodes(tx, *statement))  # no overrides
    assert not [n for n in nodes if _QUEUE_SCAN.search(_node_label(n))], nodes
    probes = b.db.read(lambda tx: _capped_probes(_pg_plan_root(tx, *statement)))
    assert len(probes) == 1, nodes  # the capped per-key probe
    (under,) = probes[0]["Plans"]
    assert under["Node Type"] in ("Index Scan", "Index Only Scan"), probes[0]
    assert under["Index Name"] == "idx_siem_queue_status_dest_id", probes[0]


@pytest.fixture()
def unvacuumed_pg(siem_backend: SiemBackendHarness) -> Iterator[SiemBackendHarness]:
    """PostgreSQL only: the queue as it is before autovacuum (fresh inserts,
    no visibility map).  The setting is restored for the shared schema."""
    if siem_backend.name != "postgres":
        pytest.skip("PostgreSQL planner behaviour before autovacuum")
    siem_backend.raw("ALTER TABLE siem_delivery_queue SET (autovacuum_enabled = false)")
    try:
        yield siem_backend
    finally:
        siem_backend.raw("ALTER TABLE siem_delivery_queue RESET (autovacuum_enabled)")


def _seed_pg_backlogs(b: SiemBackendHarness, big: int = 30_000) -> None:
    """60 known keys: the first 3 hold *big* pending rows each, the rest 5."""
    b.raw(
        "INSERT INTO siem_destinations (destination_key, region, project_id, "
        "location, instance_id, first_seen_at) SELECT 'harness:' || "
        "lpad(i::text, 16, '0'), 'us', 'p', 'us', 'i', now() "
        "FROM generate_series(1, 60) i"
    )
    for first, last, rows in ((1, 3, big), (4, 60, 5)):
        b.raw(
            "INSERT INTO siem_delivery_queue (event_uuid, destination_key, "
            "occurred_at, action_type, status, next_attempt_at, mapping_version, "
            "created_at) SELECT k || '-' || g, 'harness:' || lpad(k::text, 16, '0'), "
            "'2026-01-01T00:00:00Z', 'authentication_failure', 'pending', now(), 2, "
            f"now() FROM generate_series({first}, {last}) k, "
            f"generate_series(1, {rows}) g"
        )
    b.raw("ANALYZE siem_delivery_queue")  # statistics only: NO vacuum
    b.raw("ANALYZE siem_destinations")


def test_stranded_count_reads_at_most_cap_plus_one_rows_per_key_before_vacuum(
    unvacuumed_pg: SiemBackendHarness,
) -> None:
    """Before autovacuum the planner reaches each key's rows through a bitmap
    scan.  The per-key count must still never sort, and its heap side must
    read at most COUNT_CAP + 1 rows per key (the default planner, no enable_*
    overrides).

    Known residual: before vacuum the bitmap INDEX scan still collects all of
    a key's index entries (TIDs only); the heap reads are capped.  Once the
    visibility map is set, the count is a capped Index Only Scan (see
    test_stranded_page_reads_the_queue_in_one_bounded_statement)."""
    b = unvacuumed_pg
    _seed_pg_backlogs(b)
    with b.pool.connection() as conn:
        (allvisible,) = conn.execute(
            "SELECT relallvisible FROM pg_class "
            "WHERE oid = 'siem_delivery_queue'::regclass"
        ).fetchone()
    assert allvisible == 0, "the queue must be unvacuumed for this test"

    def _statement(tx: SiemTx) -> Any:
        rec = _Recording(tx)
        page = stats.stranded_page(rec, CONFIGURED, "", limit=2)  # 3 big keys
        assert [r["pending"] for r in page["rows"]] == [stats.COUNT_CAP + 1] * 2
        return rec.seen[0]

    sql, params = b.db.read(_statement)
    root = b.db.read(
        lambda tx: _pg_plan_root(
            tx, sql, params, options="ANALYZE, BUFFERS, FORMAT JSON"
        )
    )
    # Only the capped per-key count (a Limit inside a subplan over the queue)
    # is judged: a Sort serving the outer ORDER BY of the small key page is
    # legitimate, a Sort beneath the per-key cap reads the whole backlog.
    probes = [
        probe
        for probe in _capped_probes(root)
        if any(n.get("Relation Name") == "siem_delivery_queue" for n in _flatten(probe))
    ]
    assert probes, _flatten(root)
    for probe in probes:
        under = _flatten(probe)
        assert not [n for n in under if n["Node Type"] == "Sort"], probe
        heap = [n for n in under if n["Node Type"] in _HEAP_SIDE]
        assert heap, probe
        assert all(n["Actual Rows"] <= stats.COUNT_CAP + 1 for n in heap), probe


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
        assert "Seq Scan" not in plan and not _QUEUE_SCAN.search(plan), plan
