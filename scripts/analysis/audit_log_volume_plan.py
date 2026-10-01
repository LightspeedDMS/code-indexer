#!/usr/bin/env python3
"""Audit log read-path volume check (operator-only; not part of CI).

Seeds a SCRATCH audit store with ``--rows`` rows at a realistic skew (by
default 99.4% of rows are ``target_type='auth'`` authentication activity),
then captures the query plan and wall time of every statement the shared read
path (``services/audit_log_query.py``) issues:

- Security tier first page (all time and 7-day window);
- a deep cursor page;
- the capped count (all time and 7-day window);
- the actor / target id / correlation id filters;
- the authentication-activity aggregate over its default 24 h window.

SQLite always runs (a new file under ``--work-dir``).  PostgreSQL runs when
``--pg-dsn`` points at a server where the script may CREATE and DROP one
scratch database.  Nothing touches a real server data directory.

Usage:
    PYTHONPATH=src python3 scripts/analysis/audit_log_volume_plan.py \\
        --rows 1000000 --work-dir ~/.tmp/audit_volume [--pg-dsn "host=... port=..."]
"""

from __future__ import annotations

import argparse
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, List, Tuple

from code_indexer.server.services.audit_events import AUDIT_ROW_COLUMNS
from code_indexer.server.services.audit_log_query import (
    AUDIT_COUNT_CAP,
    POSTGRES_DIALECT,
    SQLITE_DIALECT,
    SECURITY_PROMOTED_AUTH_ACTIONS,
    AuditPage,
    SqlDialect,
    build_aggregate_sql,
    build_count_sql,
    build_filters,
    build_page_sql,
    query_audit_log,
)

_SECURITY_ACTIONS = ("user_deleted", "mcp_credential_created", "config_changed")
_AUTH_ACTIONS = (
    "token_refresh_success",
    "authentication_success",
    "oauth_authorization",
)
_BATCH = 50_000


def _rows(count: int, auth_share: float, seed: int) -> Iterator[Tuple[Any, ...]]:
    rng = random.Random(seed)
    start = datetime.now(timezone.utc) - timedelta(days=90)
    step = timedelta(days=90) / max(count, 1)
    for i in range(count):
        ts = (start + step * i).isoformat()
        if rng.random() < auth_share:
            action = _AUTH_ACTIONS[i % len(_AUTH_ACTIONS)]
            if i % 97 == 0:
                action = "authentication_failure"
            target_type = "auth"
            method = "none" if action.endswith("failure") else "jwt"
        else:
            pool = _SECURITY_ACTIONS + SECURITY_PROMOTED_AUTH_ACTIONS[:2]
            action = pool[i % len(pool)]
            target_type = "auth" if action in SECURITY_PROMOTED_AUTH_ACTIONS else "user"
            method = "web_session"
        yield (
            ts,
            f"user{i % 200}",
            action,
            target_type,
            f"target{i % 5000}",
            None,
            "success",
            "rest",
            f"192.0.2.{i % 250}",
            f"corr-{i}",
            None,
            method,
            0,
            str(uuid.uuid4()),
        )


def _batches(count: int, auth_share: float) -> Iterator[List[Tuple[Any, ...]]]:
    batch: List[Tuple[Any, ...]] = []
    for row in _rows(count, auth_share, seed=7):
        batch.append(row)
        if len(batch) >= _BATCH:
            yield batch
            batch = []
    if batch:
        yield batch


def _timed(label: str, fn: Callable[[], Any]) -> Any:
    started = time.perf_counter()
    result = fn()
    print(f"  {label:<38} {1000 * (time.perf_counter() - started):9.1f} ms")
    return result


def _statements(
    dialect: SqlDialect, mid_seek: Tuple[str, int]
) -> List[Tuple[str, str, List[Any]]]:
    now = datetime.now(timezone.utc)
    week = build_filters(date_from=now - timedelta(days=7))
    day = build_filters(date_from=now - timedelta(hours=24))
    none = build_filters()

    def page(filters: Any, tier: str, seek: Any) -> Tuple[str, List[Any]]:
        return build_page_sql(
            filters, tier, dialect, seek=seek, direction="older", limit=101, offset=0
        )

    out = [
        ("security first page, all time", *page(none, "security", None)),
        ("security first page, 7 d", *page(week, "security", None)),
        ("security deep cursor page", *page(none, "security", mid_seek)),
        (
            "count security, all time",
            *build_count_sql(none, "security", dialect, cap=AUDIT_COUNT_CAP),
        ),
        (
            "count security, 7 d",
            *build_count_sql(week, "security", dialect, cap=AUDIT_COUNT_CAP),
        ),
        (
            "aggregate auth activity, 24 h",
            *build_aggregate_sql(day, "auth_activity", dialect, max_groups=501),
        ),
    ]
    for name, value in (
        ("actor", "user17"),
        ("target_id", "target42"),
        ("correlation_id", "corr-12345"),
    ):
        out.append(
            (f"filter {name}", *page(build_filters(**{name: value}), "all", None))
        )
    return out


def _report_store(
    store: Any, explain: Callable[[str, List[Any]], str], dialect: SqlDialect
) -> None:
    first = _timed(
        "query_audit_log security page 1",
        lambda: query_audit_log(store, tier="security"),
    )
    assert isinstance(first, AuditPage)
    print(f"    total={first.total} capped={first.total_capped}")
    mid = query_audit_log(store, tier="all", legacy_offset=40_000, limit=1)
    assert isinstance(mid, AuditPage) and mid.rows
    seek = (mid.rows[0].timestamp, mid.rows[0].id)
    _timed(
        "query_audit_log security deep cursor",
        lambda: query_audit_log(store, tier="security", cursor=mid.next_cursor),
    )
    _timed(
        "query_audit_log auth aggregate 24 h",
        lambda: query_audit_log(store, tier="auth_activity", aggregate=True),
    )
    for label, sql, params in _statements(dialect, seek):
        print(f"  PLAN {label}:")
        for line in explain(sql, params).splitlines():
            print(f"      {line}")


def run_sqlite(rows: int, auth_share: float, work_dir: Path) -> None:
    from code_indexer.server.services.audit_log_service import AuditLogService

    db_path = work_dir / f"audit_volume_{uuid.uuid4().hex[:8]}.db"
    store = AuditLogService(db_path)
    conn = store._get_connection()
    sql = (
        f"INSERT INTO audit_logs ({', '.join(AUDIT_ROW_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in AUDIT_ROW_COLUMNS)})"
    )
    started = time.perf_counter()
    for batch in _batches(rows, auth_share):
        conn.executemany(sql, batch)
        conn.commit()
    conn.execute("ANALYZE")
    conn.commit()
    elapsed = time.perf_counter() - started
    print(f"SQLite: seeded {rows} rows in {elapsed:.1f}s ({db_path})")

    def explain(sql_text: str, params: List[Any]) -> str:
        found = conn.execute("EXPLAIN QUERY PLAN " + sql_text, params).fetchall()
        return "\n".join(str(r[3]) for r in found)

    _report_store(store, explain, SQLITE_DIALECT)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)


def run_postgres(rows: int, auth_share: float, dsn: str) -> None:
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from code_indexer.server.services.audit_log_service import AuditLogService
    from code_indexer.server.storage.postgres.audit_log_backend import (
        AuditLogPostgresBackend,
    )
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
    from code_indexer.server.storage.postgres.migrations.runner import MigrationRunner

    name = f"audit_volume_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    params = conninfo_to_dict(dsn)
    params["dbname"] = name
    scratch = make_conninfo(**params)  # type: ignore[arg-type]
    pool = None
    try:
        with MigrationRunner(scratch) as runner:
            runner.run()
        started = time.perf_counter()
        with psycopg.connect(scratch) as conn:
            with conn.cursor().copy(
                f"COPY audit_logs ({', '.join(AUDIT_ROW_COLUMNS)}) FROM STDIN"
            ) as copy:
                for batch in _batches(rows, auth_share):
                    for row in batch:
                        copy.write_row(row)
            conn.commit()
        with psycopg.connect(scratch, autocommit=True) as conn:
            conn.execute("ANALYZE audit_logs")
        print(f"PostgreSQL: seeded {rows} rows in {time.perf_counter() - started:.1f}s")
        pool = ConnectionPool(scratch, min_size=1, max_size=2)
        store = AuditLogService(
            Path("/unused"), storage_backend=AuditLogPostgresBackend(pool)
        )

        def explain(sql_text: str, sql_params: List[Any]) -> str:
            with psycopg.connect(scratch) as conn:
                found = conn.execute(
                    "EXPLAIN (ANALYZE, BUFFERS) " + sql_text, sql_params
                ).fetchall()
            return "\n".join(str(r[0]) for r in found)

        _report_store(store, explain, POSTGRES_DIALECT)
    finally:
        if pool is not None:
            pool.close()
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rows", type=int, default=1_000_000)
    parser.add_argument("--auth-share", type=float, default=0.994)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--pg-dsn", default="")
    args = parser.parse_args()
    args.work_dir.expanduser().mkdir(parents=True, exist_ok=True)
    run_sqlite(args.rows, args.auth_share, args.work_dir.expanduser())
    if args.pg_dsn:
        run_postgres(args.rows, args.auth_share, args.pg_dsn)


if __name__ == "__main__":
    main()
