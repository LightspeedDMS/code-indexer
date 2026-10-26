"""Story #2203 review: the Query Analytics export never fails, or leaks a cell,
over the text of a recorded search. Real stores (SQLite and PostgreSQL)."""

import logging
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator
from unittest.mock import patch

import pytest

from code_indexer.server.services.query_analytics_export_service import (
    QueryAnalyticsExportService,
)
from code_indexer.server.services.xray_evaluator_sources import evaluator_source
from tests.unit.server.services.search_event_real_stores import (
    PG_AVAILABLE,
    PG_SKIP_REASON,
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


@pytest.fixture(params=_KINDS)
def store(request, tmp_path) -> Iterator[RealStore]:
    with open_store(request.param, tmp_path) as real_store:
        yield real_store


def _xray_record(source: str, query_text: str = "def example_") -> Any:
    stored = evaluator_source(source)
    return make_record(
        search_type="xray",
        query_text=query_text,
        evaluator_sha256=stored.sha256,
        evaluator_sources=(stored,),
    )


def _service(store: RealStore, directory: Path) -> QueryAnalyticsExportService:
    return QueryAnalyticsExportService(
        backend=store.export_backend, golden_repos_dir=str(directory)
    )


def _run_export(store: RealStore, directory: Path, export_id: str) -> None:
    _service(store, directory).run_export(
        export_id=export_id,
        filters={"search_type": "xray"},
        initiated_by="admin",
        export_retention_days=7,
        search_event_log_backend=store.backend,
    )


def _record(store: RealStore, directory: Path, export_id: str) -> Dict[str, Any]:
    [record] = _service(store, directory).list_exports(export_id=export_id)
    return dict(record)


def _sheet_rows(path: str) -> list:
    import openpyxl

    sheet = openpyxl.load_workbook(path).worksheets[0]
    header, *data = [list(r) for r in sheet.iter_rows(values_only=True)]
    return [dict(zip(header, d)) for d in data]


class TestIllegalCharacters:
    def test_source_with_an_illegal_character_exports_flagged(self, store, tmp_path):
        source = "fn evaluate_node() {}\x0c// page\x01break\n"
        store.backend.insert_batch([_xray_record(source, query_text="def\x0cx")])

        export_id = str(uuid.uuid4())
        _run_export(store, tmp_path, export_id)
        record = _record(store, tmp_path, export_id)

        assert record["status"] == "completed", record
        [row] = _sheet_rows(record["file_path"])
        assert "\x0c" not in row["evaluator_source"]
        assert "\x01" not in row["evaluator_source"]
        assert row["evaluator_source"].startswith("fn evaluate_node() {}")
        assert row["evaluator_source_truncated"] is True
        assert "\x0c" not in row["query_text"]


class TestUtf16Cut:
    def test_astral_source_is_cut_by_utf16_units(self, store, tmp_path):
        source = "\U0001f600" * 20_000
        store.backend.insert_batch([_xray_record(source)])

        export_id = str(uuid.uuid4())
        _run_export(store, tmp_path, export_id)
        [row] = _sheet_rows(_record(store, tmp_path, export_id)["file_path"])

        cell = row["evaluator_source"]
        assert len(cell.encode("utf-16-le")) // 2 <= 32_767
        assert cell == "\U0001f600" * 16_383
        assert row["evaluator_source_truncated"] is True


class TestWriterTextSafety:
    """SQLite and PostgreSQL cannot store a lone surrogate (it is not valid
    UTF-8): the writer stores its backslash escape instead, so the row and
    its batch neighbours are kept. The hash is over the original code's
    surrogatepass bytes."""

    def test_lone_surrogate_row_and_its_neighbours_are_stored(self, store):
        import hashlib

        source = "fn evaluate_node() {} // \ud800 stray\n"
        expected = hashlib.sha256(source.encode("utf-8", "surrogatepass")).hexdigest()
        store.backend.insert_batch(
            [
                make_record(search_type="xray", query_text="before"),
                _xray_record(source, query_text="lone \udc00 query"),
                make_record(search_type="xray", query_text="after"),
            ]
        )

        events, total = store.backend.query(search_type="xray", limit=10)
        assert total == 3
        [odd] = [e for e in events if e["evaluator_sha256"] == expected]
        escaped_query = "lone " + "\\" + "udc00 query"
        assert odd["query_text"] == escaped_query
        stored, _cut = store.backend.evaluator_sources([expected])[expected]
        assert stored == "fn evaluate_node() {} // " + "\\" + "ud800 stray\n"

    def test_a_failed_batch_counts_its_dropped_events(self, store):
        from code_indexer.server.services.search_event_log_writer import (
            SearchEventLogWriter,
        )

        writer = SearchEventLogWriter(store.backend)
        store.drop_table()
        writer.start()
        for name in ("one", "two", "three"):
            writer.enqueue(make_record(query_text=name))
        writer.stop()

        assert writer.drop_snapshot().dropped == 3


def _writer_whose_batch_failed(store: RealStore, events: int) -> Any:
    from code_indexer.server.services.search_event_log_writer import (
        SearchEventLogWriter,
    )

    writer = SearchEventLogWriter(store.backend)
    store.drop_table()
    writer.start()
    for index in range(events):
        writer.enqueue(make_record(query_text=f"query {index}"))
    writer.stop()
    return writer


class TestDropReporting:
    def test_failed_batch_drops_appear_in_health_with_an_accurate_cause(
        self, store, tmp_path, monkeypatch
    ):
        from code_indexer.server import app as app_package
        from code_indexer.server.services.health_service import HealthCheckService

        writer = _writer_whose_batch_failed(store, events=2)
        monkeypatch.setattr(
            app_package.app.state, "search_event_log_writer", writer, raising=False
        )
        service = HealthCheckService()
        service.database_url = f"sqlite:///{tmp_path / 'cidx_server.db'}"

        reasons = [
            r
            for r in service.get_system_health().failure_reasons
            if "search event" in r.lower()
        ]

        [reason] = reasons
        assert "2" in reason
        assert "write to the store failed" in reason

    def test_failed_batch_drops_reach_the_otel_counter(self, store):
        from tests.unit.server.telemetry.otel_test_support import (
            active_application_metrics_singleton,
            find_metric,
        )

        with active_application_metrics_singleton() as (_metrics, reader):
            _writer_whose_batch_failed(store, events=3)
            metric = find_metric(reader, "cidx.search_event_log.dropped_events")

        assert metric is not None
        assert sum(dp.value for dp in metric.data.data_points) == 3


class TestBoundedLookup:
    def test_source_lookup_is_cut_in_sql(self, store):
        source = "y" * 50_000
        stored = evaluator_source(source)
        store.backend.insert_batch([_xray_record(source)])

        found = store.backend.evaluator_sources([stored.sha256], max_chars=32_768)

        assert found == {stored.sha256: (source[:32_768], False)}


_CELL_TEXT = "fn secret_marker_7f1e() {}"


class TestExportFailureIsNeutral:
    def test_export_failure_never_records_cell_text(self, store, tmp_path, caplog):
        """openpyxl's own IllegalCharacterError names the offending value;
        neither the log nor the export history may repeat it."""
        from openpyxl.utils.exceptions import IllegalCharacterError
        from openpyxl.worksheet._write_only import WriteOnlyWorksheet

        store.backend.insert_batch([_xray_record(_CELL_TEXT)])
        original_append = WriteOnlyWorksheet.append

        def _append(self: Any, row: Any) -> None:
            if any(getattr(cell, "value", cell) == _CELL_TEXT for cell in row):
                raise IllegalCharacterError(f"{_CELL_TEXT} cannot be used")
            original_append(self, row)

        export_id = str(uuid.uuid4())
        caplog.set_level(logging.DEBUG)
        with patch.object(WriteOnlyWorksheet, "append", _append):
            with pytest.raises(RuntimeError):
                _run_export(store, tmp_path, export_id)

        record = _record(store, tmp_path, export_id)
        assert record["status"] == "failed"
        assert "IllegalCharacterError" in (record["error_message"] or "")
        assert _CELL_TEXT not in (record["error_message"] or "")
        assert _CELL_TEXT not in caplog.text


def _sheet_xml(path: str) -> str:
    import zipfile

    with zipfile.ZipFile(path) as archive:
        return archive.read("xl/worksheets/sheet1.xml").decode("utf-8")


class TestFinalReviewExport:
    def test_xml_noncharacters_export_replaced_and_flagged(self, store, tmp_path):
        source = "fn evaluate_node() {} // ￾ and ￿\n"
        store.backend.insert_batch([_xray_record(source, query_text="q ￿ ￾")])

        export_id = str(uuid.uuid4())
        _run_export(store, tmp_path, export_id)
        record = _record(store, tmp_path, export_id)

        assert record["status"] == "completed", record
        [row] = _sheet_rows(record["file_path"])
        for text in (row["evaluator_source"], row["query_text"]):
            assert "￾" not in text and "￿" not in text
        assert row["evaluator_source_truncated"] is True

    def test_formula_like_text_is_exported_literally(self, store, tmp_path):
        texts = ["=1+1", "+cmd", "-x", "@x"]
        store.backend.insert_batch(
            [_xray_record("=1+1", query_text=text) for text in texts]
        )

        export_id = str(uuid.uuid4())
        _run_export(store, tmp_path, export_id)
        path = _record(store, tmp_path, export_id)["file_path"]

        assert "<f>" not in _sheet_xml(path)
        rows = _sheet_rows(path)
        assert sorted(r["query_text"] for r in rows) == sorted(texts)
        assert {r["evaluator_source"] for r in rows} == {"=1+1"}

    def test_cell_and_cleanup_failures_raise_and_log_no_cell_text(
        self, store, tmp_path, caplog
    ):
        """BackgroundJobManager stores str(exception) as the job's error."""
        from openpyxl import Workbook
        from openpyxl.utils.exceptions import IllegalCharacterError
        from openpyxl.worksheet._write_only import WriteOnlyWorksheet

        store.backend.insert_batch([_xray_record(_CELL_TEXT)])

        def _append(self: Any, row: Any) -> None:
            raise IllegalCharacterError(f"{_CELL_TEXT} cannot be used")

        def _save(self: Any, filename: Any) -> None:
            raise ValueError(f"cannot save {_CELL_TEXT}")

        export_id = str(uuid.uuid4())
        caplog.set_level(logging.DEBUG)
        with patch.object(WriteOnlyWorksheet, "append", _append):
            with patch.object(Workbook, "save", _save):
                with pytest.raises(Exception) as raised:
                    _run_export(store, tmp_path, export_id)

        assert _CELL_TEXT not in str(raised.value)
        assert "IllegalCharacterError" in str(raised.value)
        assert _CELL_TEXT not in caplog.text
        assert _CELL_TEXT not in (_record(store, tmp_path, export_id)["error_message"])

    def test_read_failure_keeps_the_database_message(self, store, tmp_path):
        store.backend.insert_batch([_xray_record(_CELL_TEXT)])
        store.drop_table()

        export_id = str(uuid.uuid4())
        with pytest.raises(Exception) as raised:
            _run_export(store, tmp_path, export_id)

        message = _record(store, tmp_path, export_id)["error_message"] or ""
        driver_text = "no such table" if store.kind == "sqlite" else "does not exist"
        assert driver_text in message and "search_event_log" in message
        assert driver_text in str(raised.value)

    def test_pruned_source_exports_blank_and_flagged(self, store, tmp_path):
        stored = evaluator_source("fn gone() {}")
        store.backend.insert_batch(
            [make_record(search_type="xray", evaluator_sha256=stored.sha256)]
        )

        export_id = str(uuid.uuid4())
        _run_export(store, tmp_path, export_id)
        [row] = _sheet_rows(_record(store, tmp_path, export_id)["file_path"])

        assert row["evaluator_source"] is None
        assert row["evaluator_source_truncated"] is True

    def test_escaped_surrogate_source_exports_flagged(self, store, tmp_path):
        store.backend.insert_batch([_xray_record("fn a() {} // \ud800\n")])

        export_id = str(uuid.uuid4())
        _run_export(store, tmp_path, export_id)
        [row] = _sheet_rows(_record(store, tmp_path, export_id)["file_path"])

        assert row["evaluator_source_truncated"] is True


_SECRET_PATH = "/srv/example-secret-dir/export.xlsx"


class TestCleanupFailureStaysNeutral:
    def test_unlink_failure_after_a_cell_failure_keeps_the_neutral_error(
        self, store, tmp_path, caplog
    ):
        from openpyxl.utils.exceptions import IllegalCharacterError
        from openpyxl.worksheet._write_only import WriteOnlyWorksheet

        store.backend.insert_batch([_xray_record(_CELL_TEXT)])

        def _append(self: Any, row: Any) -> None:
            raise IllegalCharacterError("cannot be used")

        def _unlink(self: Any, missing_ok: bool = False) -> None:
            raise PermissionError(13, "Permission denied", _SECRET_PATH)

        export_id = str(uuid.uuid4())
        caplog.set_level(logging.DEBUG)
        with patch.object(WriteOnlyWorksheet, "append", _append):
            with patch.object(Path, "unlink", _unlink):
                with pytest.raises(RuntimeError) as raised:
                    _run_export(store, tmp_path, export_id)

        assert str(raised.value) == "export failed (IllegalCharacterError)"
        message = _record(store, tmp_path, export_id)["error_message"] or ""
        assert message == "export failed (IllegalCharacterError)"
        assert _SECRET_PATH not in caplog.text
