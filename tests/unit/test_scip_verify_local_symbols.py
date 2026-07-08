"""Regression test for the SCIP verification false-negative on local symbols.

Local symbols ("local N") have no structured descriptor, so the DB builder derives
their display_name from the occurrence identifier -- which legitimately differs
from the protobuf symbol_info.display_name. The verifier used to flag that as a
mismatch and mark an otherwise-valid, fully-queryable database as FAILED.
"""

from pathlib import Path

try:
    from pysqlite3 import dbapi2 as sqlite3
except ImportError:
    import sqlite3

from code_indexer.scip.database.verify import SCIPDatabaseVerifier
from code_indexer.scip.database.builder import SCIPDatabaseBuilder
from code_indexer.scip.database.schema import DatabaseManager
from code_indexer.scip.protobuf import scip_pb2


def _build(tmp_path):
    index = scip_pb2.Index()
    # A global symbol whose display_name must still be verified.
    g = index.external_symbols.add()
    g.symbol = "test.py::Foo#"
    g.display_name = "Foo"
    g.kind = scip_pb2.SymbolInformation.Class
    # A local symbol -- verifier must NOT compare its display_name.
    loc = index.external_symbols.add()
    loc.symbol = "local 0"
    loc.display_name = "name"
    loc.kind = scip_pb2.SymbolInformation.Class

    scip_file = tmp_path / "t.scip"
    scip_file.write_bytes(index.SerializeToString())
    manager = DatabaseManager(scip_file)
    manager.create_schema()
    SCIPDatabaseBuilder().build(scip_file, manager.db_path)
    return manager.db_path, scip_file


def test_local_symbol_display_name_mismatch_does_not_fail(tmp_path: Path):
    db_path, scip_file = _build(tmp_path)
    # Simulate the real builder-vs-protobuf divergence for the local: the DB
    # stores the occurrence identifier ("args"), the protobuf had "name".
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE symbols SET display_name = 'args' WHERE name = 'local 0'")
    conn.commit()
    conn.close()

    result = SCIPDatabaseVerifier(db_path, scip_file).verify()

    assert result.symbol_sample_verified is True
    assert not any("local 0" in e for e in result.errors)


def test_non_local_display_name_mismatch_still_fails(tmp_path: Path):
    # Guard against over-skipping: a real (global) symbol mismatch must still fail.
    db_path, scip_file = _build(tmp_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE symbols SET display_name = 'Wrong' WHERE name = 'test.py::Foo#'"
    )
    conn.commit()
    conn.close()

    result = SCIPDatabaseVerifier(db_path, scip_file).verify()

    assert result.symbol_sample_verified is False
    assert any("test.py::Foo#" in e for e in result.errors)
