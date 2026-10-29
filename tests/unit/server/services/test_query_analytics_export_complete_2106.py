"""Bug #2106: an export contains every matching row, filtered in the store.

Every test runs against a REAL store on both backends (see
search_event_real_stores). The large-data tests cross the former 50,000-row
cap with cache hits AND misses on both sides of it, so a cap, or a cache
filter applied after a cap, changes the exported set.
"""

import re
import sqlite3
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Set

import openpyxl
import pytest

from code_indexer.server.services import query_analytics_export_service as qae
from code_indexer.server.services.query_analytics_export_service import (
    QueryAnalyticsExportService,
)
from tests.unit.server.services.search_event_real_stores import (
    PG_AVAILABLE,
    PG_SKIP_REASON,
    TEST_DSN,
    RealStore,
    make_record,
    open_store,
)

_KINDS = [
    "sqlite",
    pytest.param(
        "postgres", marks=pytest.mark.skipif(not PG_AVAILABLE, reason=PG_SKIP_REASON)
    ),
]

_CORRELATION_COL = qae._EXCEL_COLUMNS.index("correlation_id")
_TIMESTAMP_COL = qae._EXCEL_COLUMNS.index("timestamp")

# Large dataset: OLD rows are older than the NEW block, and the NEW block alone
# fills the former 50,000-row cap (newest first).
_BASE_TS = 1_700_000_000.0
_OLD_ROWS = 1_500
_NEW_ROWS = 50_000


def _is_hit(i: int) -> bool:
    return i % 2 == 0


def _large_records() -> List[Any]:
    records = []
    for i in range(_OLD_ROWS):
        records.append(
            make_record(
                timestamp=_BASE_TS + i,
                voyage_cache_hit=_is_hit(i),
                # Bug #2112: the cache was consulted (a hit or a miss).
                voyage_cache_mode="on",
                correlation_id=f"old-{i}",
            )
        )
    for j in range(_NEW_ROWS):
        records.append(
            make_record(
                timestamp=_BASE_TS + 10_000 + j,
                voyage_cache_hit=_is_hit(j),
                voyage_cache_mode="on",
                correlation_id=f"new-{j}",
            )
        )
    return records


def _expected(predicate) -> Set[str]:
    ids = {f"old-{i}" for i in range(_OLD_ROWS) if predicate(_is_hit(i))}
    ids |= {f"new-{j}" for j in range(_NEW_ROWS) if predicate(_is_hit(j))}
    return ids


@pytest.fixture(scope="module", params=_KINDS)
def large_store(request, tmp_path_factory) -> Iterator[RealStore]:
    with open_store(request.param, tmp_path_factory.mktemp("large")) as store:
        store.bulk_insert(_large_records())
        yield store


@pytest.fixture(params=_KINDS)
def real_store(request, tmp_path) -> Iterator[RealStore]:
    with open_store(request.param, tmp_path) as store:
        yield store


def _run(store: RealStore, tmp_path: Path, filters: Dict[str, Any], backend=None):
    service = QueryAnalyticsExportService(
        backend=store.export_backend, golden_repos_dir=str(tmp_path)
    )
    export_id = str(uuid.uuid4())
    service.run_export(
        export_id=export_id,
        filters=filters,
        initiated_by="admin",
        export_retention_days=7,
        search_event_log_backend=backend or store.backend,
    )
    [record] = service.list_exports(export_id=export_id)
    return service, export_id, record


def _sheets(file_path: str) -> List[List[tuple]]:
    """Return every worksheet's rows (header included), in workbook order."""
    wb = openpyxl.load_workbook(file_path, read_only=True)
    try:
        return [list(ws.iter_rows(values_only=True)) for ws in wb.worksheets]
    finally:
        wb.close()


_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _column_values(file_path: str, column: str) -> List[str]:
    """Return one column's data values across every worksheet, in order.

    Streams the sheet XML (openpyxl's reader takes ~10 s for 50k rows).
    Handles inline and shared strings; skips each sheet's header row.
    """
    with zipfile.ZipFile(file_path) as z:
        names = z.namelist()
        shared: List[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            shared = [
                "".join(t.text or "" for t in si.iter(f"{_XLSX_NS}t"))
                for si in root.iter(f"{_XLSX_NS}si")
            ]
        sheets = sorted(
            (n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
            key=lambda n: int(re.findall(r"\d+", n)[-1]),
        )
        values: List[str] = []
        for name in sheets:
            with z.open(name) as fh:
                for _, el in ET.iterparse(fh, events=("end",)):
                    if el.tag != f"{_XLSX_NS}c":
                        continue
                    ref = el.get("r", "")
                    if re.fullmatch(rf"{column}\d+", ref) and ref != f"{column}1":
                        text = "".join(t.text or "" for t in el.iter(f"{_XLSX_NS}t"))
                        if el.get("t") == "s":
                            text = shared[int(el.findtext(f"{_XLSX_NS}v") or "")]
                        values.append(text)
                    el.clear()
    return values


def _data_rows(file_path: str) -> List[tuple]:
    rows: List[tuple] = []
    for sheet in _sheets(file_path):
        assert list(sheet[0]) == qae._EXCEL_COLUMNS
        rows.extend(sheet[1:])
    return rows


# ---------------------------------------------------------------------------
# Beyond the former 50,000-row cap
# ---------------------------------------------------------------------------


class TestEveryMatchingRowIsExported:
    @pytest.mark.slow
    @pytest.mark.parametrize(
        "cache_hit_filter, predicate",
        [
            ("all", lambda hit: True),
            ("hits_only", lambda hit: hit),
            ("misses_only", lambda hit: not hit),
        ],
    )
    def test_export_equals_filtering_the_complete_set(
        self, large_store, tmp_path, cache_hit_filter, predicate
    ):
        _, _, record = _run(
            large_store, tmp_path, {"cache_hit_filter": cache_hit_filter}
        )

        assert record["status"] == "completed", record["error_message"]
        # correlation_id is spreadsheet column P (Bug #2114 added
        # query_text_truncated after query_text).
        assert _CORRELATION_COL == ord("P") - ord("A")
        exported = _column_values(record["file_path"], "P")
        expected = _expected(predicate)
        assert len(exported) == len(set(exported)), "a row was exported twice"
        assert set(exported) == expected
        assert record["row_count"] == len(exported) == len(expected)


# ---------------------------------------------------------------------------
# Bounded batches, sheets, snapshot, order (small data)
# ---------------------------------------------------------------------------


class TestBoundedExport:
    def test_rows_split_across_sheets_beyond_sheet_limit(
        self, real_store, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(qae, "_EXCEL_MAX_DATA_ROWS_PER_SHEET", 10, raising=False)
        real_store.bulk_insert(
            [
                make_record(timestamp=_BASE_TS + i, correlation_id=f"r{i}")
                for i in range(25)
            ]
        )

        _, _, record = _run(real_store, tmp_path, {})

        sheets = _sheets(record["file_path"])
        assert [len(s) - 1 for s in sheets] == [10, 10, 5]
        assert all(list(s[0]) == qae._EXCEL_COLUMNS for s in sheets)
        assert record["row_count"] == 25

    def test_equal_timestamps_across_batch_boundary_exported_once(
        self, real_store, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(qae, "_EXPORT_BATCH_SIZE", 5, raising=False)
        same = [
            make_record(timestamp=_BASE_TS, correlation_id=f"s{i}") for i in range(23)
        ]
        later = [
            make_record(timestamp=_BASE_TS + 1 + i, correlation_id=f"l{i}")
            for i in range(4)
        ]
        real_store.bulk_insert(same + later)

        _, _, record = _run(real_store, tmp_path, {})

        exported = [r[_CORRELATION_COL] for r in _data_rows(record["file_path"])]
        assert sorted(exported) == sorted(
            [f"s{i}" for i in range(23)] + [f"l{i}" for i in range(4)]
        )
        assert record["row_count"] == 27

    def test_rows_are_exported_oldest_first(self, real_store, tmp_path):
        real_store.bulk_insert(
            [make_record(timestamp=_BASE_TS + i) for i in (5, 1, 9, 3)]
        )

        _, _, record = _run(real_store, tmp_path, {})

        stamps = [r[_TIMESTAMP_COL] for r in _data_rows(record["file_path"])]
        assert stamps == sorted(stamps) == [_BASE_TS + i for i in (1, 3, 5, 9)]

    def test_rows_recorded_after_export_start_are_excluded(self, real_store, tmp_path):
        now = time.time()
        real_store.bulk_insert(
            [
                make_record(timestamp=now - 60, correlation_id="before"),
                make_record(timestamp=now + 3_600, correlation_id="after"),
            ]
        )

        _, _, record = _run(real_store, tmp_path, {})

        exported = [r[_CORRELATION_COL] for r in _data_rows(record["file_path"])]
        assert exported == ["before"]


class TestKeysetWithIdsOutOfTimestampOrder:
    """Review P4-A of #2106: ids need not grow with timestamps.

    The later-timestamp rows are inserted FIRST, so they get the LOWEST ids.
    A batch boundary inside the same-timestamp block leaves a keyset whose id
    is above every later row's id, so a keyset of `timestamp >= ? AND id > ?`
    would skip all the later rows.
    """

    def test_lower_id_later_rows_survive_batch_boundary(
        self, real_store, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(qae, "_EXPORT_BATCH_SIZE", 5, raising=False)
        real_store.bulk_insert(
            [
                make_record(timestamp=_BASE_TS + 1 + i, correlation_id=f"late{i}")
                for i in range(4)
            ]
        )
        real_store.bulk_insert(
            [
                make_record(timestamp=_BASE_TS, correlation_id=f"same{i}")
                for i in range(23)
            ]
        )

        _, _, exported_record = _run(real_store, tmp_path, {})

        order = [r[_CORRELATION_COL] for r in _data_rows(exported_record["file_path"])]
        want = [f"same{i}" for i in range(23)] + [f"late{i}" for i in range(4)]
        assert order == want
        assert exported_record["row_count"] == len(want)


class _DropsTableAfterFirstBatch:
    """Real store wrapper: the second export read finds the table gone."""

    def __init__(self, store: RealStore) -> None:
        self._store = store
        self.reads = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store.backend, name)

    def query_for_export(self, *args: Any, **kwargs: Any) -> Any:
        self.reads += 1
        if self.reads == 2:
            self._store.drop_table()
        return self._store.backend.query_for_export(*args, **kwargs)


class TestReadFailureMidExport:
    def test_failure_after_first_batch_fails_export_and_leaves_no_file(
        self, real_store, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(qae, "_EXPORT_BATCH_SIZE", 5, raising=False)
        real_store.bulk_insert([make_record(timestamp=_BASE_TS + i) for i in range(12)])
        wrapper = _DropsTableAfterFirstBatch(real_store)
        service = QueryAnalyticsExportService(
            backend=real_store.export_backend, golden_repos_dir=str(tmp_path)
        )
        export_id = str(uuid.uuid4())

        with pytest.raises(Exception):
            service.run_export(
                export_id=export_id,
                filters={},
                initiated_by="admin",
                export_retention_days=7,
                search_event_log_backend=wrapper,
            )

        [record] = service.list_exports(export_id=export_id)
        assert record["status"] == "failed"
        assert "search_event_log" in (record["error_message"] or "")
        assert record["row_count"] is None
        assert not service.export_path(export_id).exists()


class _ReconcilesDuringFirstRead:
    """Real store wrapper: a peer's startup reconcile runs mid-export.

    The real reconcile_orphaned_exports (threshold in the past, so the
    in-flight row counts as stale) marks the running export failed with its
    "interrupted" message while the export is still reading.
    """

    def __init__(self, store: RealStore) -> None:
        self._store = store
        self.reconciled = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store.backend, name)

    def query_for_export(self, *args: Any, **kwargs: Any) -> Any:
        if not self.reconciled:
            self.reconciled = self._store.export_backend.reconcile_orphaned_exports(
                threshold_seconds=-60
            )
        return self._store.backend.query_for_export(*args, **kwargs)


class TestCompletedExportAfterPeerReconcile:
    def test_completed_export_clears_error_set_by_peer_reconcile(
        self, real_store, tmp_path
    ):
        real_store.bulk_insert([make_record(timestamp=_BASE_TS)])
        wrapper = _ReconcilesDuringFirstRead(real_store)

        _, _, record = _run(real_store, tmp_path, {}, backend=wrapper)

        assert wrapper.reconciled == 1
        assert record["status"] == "completed"
        assert record["error_message"] is None
        assert record["row_count"] == 1


def _export_plan(store: RealStore, filters: Dict[str, Any], after) -> List[str]:
    """EXPLAIN the exact statement the backend runs for one export batch."""
    from code_indexer.server.services.search_event_log_writer import _export_select

    if store.kind == "sqlite":
        sql, params = _export_select(filters, after, 10, "?", "1", "0")
    else:
        sql, params = _export_select(filters, after, 10, "%s", "TRUE", "FALSE")
    return store.explain(sql, params)


class TestKeysetReadIsSeekable:
    """Each batch after the first must seek the timestamp index to its keyset.

    A keyset the index cannot seek turns a full export into a quadratic scan.
    """

    def test_batch_after_keyset_is_an_index_range_search(self, real_store):
        plan = _export_plan(real_store, {}, after=(_BASE_TS, 5))

        if real_store.kind == "sqlite":
            assert any(
                re.search(r"SEARCH .*idx_sel_timestamp \(timestamp>", line)
                for line in plan
            ), plan
        else:
            assert any(
                "Index Cond" in line and '"timestamp" >=' in line for line in plan
            ), plan


class TestUserFilteredReadIsSeekable:
    """A user-filtered batch must seek (username, timestamp) WITHOUT statistics.

    Production is solo SQLite and nothing runs ANALYZE; without the composite
    index SQLite picks idx_sel_user and re-sorts all of the user's rows on
    every batch (quadratic in that user's row count).
    """

    def test_user_filtered_batch_seeks_user_timestamp_index(self, real_store):
        plan = _export_plan(real_store, {"user": "alice"}, after=(_BASE_TS, 5))

        if real_store.kind == "sqlite":
            assert any(
                "USING INDEX idx_sel_user_ts (username=? AND timestamp>?)" in line
                for line in plan
            ), plan
            assert not any("TEMP B-TREE" in line for line in plan), plan
        else:
            cond = [line for line in plan if "Index Cond" in line]
            assert any("idx_sel_user_ts" in line for line in plan), plan
            assert any(
                "username =" in line and '"timestamp" >=' in line for line in cond
            ), plan


def _sqlite_index_columns(db_path: Path, index: str) -> List[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [r[2] for r in conn.execute(f"PRAGMA index_info({index})")]
    finally:
        conn.close()


class TestUserTimestampIndexIsProvisioned:
    def test_sqlite_startup_schema_creates_index_on_fresh_db(self, tmp_path):
        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = tmp_path / "cidx_server.db"
        DatabaseSchema(str(db_path)).initialize_database()

        assert _sqlite_index_columns(db_path, "idx_sel_user_ts") == [
            "username",
            "timestamp",
        ]

    def test_sqlite_startup_schema_adds_index_to_existing_db(self, tmp_path):
        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = tmp_path / "cidx_server.db"
        schema = DatabaseSchema(str(db_path))
        conn = sqlite3.connect(str(db_path))
        try:  # a database created before the index existed
            conn.execute(schema.CREATE_SEARCH_EVENT_LOG_TABLE)
            conn.execute(schema.CREATE_IDX_SEL_TIMESTAMP)
            conn.execute(schema.CREATE_IDX_SEL_USER)
            conn.commit()
        finally:
            conn.close()
        assert _sqlite_index_columns(db_path, "idx_sel_user_ts") == []

        schema.initialize_database()

        assert _sqlite_index_columns(db_path, "idx_sel_user_ts") == [
            "username",
            "timestamp",
        ]

    @pytest.mark.skipif(not PG_AVAILABLE, reason=PG_SKIP_REASON)
    def test_postgres_migrations_create_index(self):
        import psycopg
        from psycopg.conninfo import make_conninfo

        from code_indexer.server.storage.postgres.migrations.runner import (
            MigrationRunner,
        )

        name = f"t071_{uuid.uuid4().hex[:12]}"
        admin = psycopg.connect(TEST_DSN, autocommit=True)
        try:
            admin.execute(f"CREATE DATABASE {name}")
            try:
                dsn = make_conninfo(TEST_DSN, dbname=name)
                with MigrationRunner(dsn) as runner:
                    runner.run()
                with psycopg.connect(dsn) as conn:
                    row = conn.execute(
                        "SELECT indexdef FROM pg_indexes "
                        "WHERE indexname = 'idx_sel_user_ts'"
                    ).fetchone()
            finally:
                admin.execute(f"DROP DATABASE {name} WITH (FORCE)")
        finally:
            admin.close()

        assert row is not None, "migration did not create idx_sel_user_ts"
        assert 'search_event_log USING btree (username, "timestamp")' in row[0]


class TestQueryForExportReadContract:
    def test_rejects_unknown_cache_hit_filter(self, real_store):
        real_store.bulk_insert([make_record(timestamp=_BASE_TS)])

        with pytest.raises(ValueError, match="hit_only"):
            real_store.backend.query_for_export(
                {"cache_hit_filter": "hit_only"}, limit=10
            )

    def test_raises_when_table_missing(self, real_store):
        real_store.drop_table()

        with pytest.raises(Exception) as excinfo:
            real_store.backend.query_for_export({}, limit=10)

        assert "search_event_log" in str(excinfo.value)
