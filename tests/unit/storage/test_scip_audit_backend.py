"""Tests for SCIPAuditBackend Protocol and SCIPAuditSqliteBackend (Story #516)."""

import pytest
from pathlib import Path


class TestSCIPAuditBackendProtocol:
    def test_protocol_is_runtime_checkable(self):
        from code_indexer.server.storage.protocols import SCIPAuditBackend

        class NotABackend:
            pass

        try:
            isinstance(NotABackend(), SCIPAuditBackend)
        except TypeError:
            pytest.fail("SCIPAuditBackend is not @runtime_checkable")

    def test_protocol_has_required_methods(self):
        from code_indexer.server.storage.protocols import SCIPAuditBackend

        required = ["create_audit_record", "query_audit_records", "close"]
        for m in required:
            assert m in dir(SCIPAuditBackend), f"Missing {m}"


class TestSCIPAuditSqliteBackend:
    @pytest.fixture
    def backend(self, tmp_path):
        from code_indexer.server.storage.sqlite_backends import SCIPAuditSqliteBackend

        b = SCIPAuditSqliteBackend(str(tmp_path / "test_scip.db"))
        yield b
        b.close()

    def test_satisfies_protocol(self, backend):
        from code_indexer.server.storage.protocols import SCIPAuditBackend

        assert isinstance(backend, SCIPAuditBackend)

    def test_create_and_query_record(self, backend):
        record_id = backend.create_audit_record(
            job_id="job-001",
            repo_alias="my-repo",
            package="numpy",
            command="pip install numpy",
            project_language="python",
            username="admin",
        )
        assert isinstance(record_id, int) and record_id > 0

        records, total = backend.query_audit_records(job_id="job-001")
        assert total == 1
        assert records[0]["package"] == "numpy"
        assert records[0]["repo_alias"] == "my-repo"

    def test_query_with_filters(self, backend):
        backend.create_audit_record(
            job_id="j1",
            repo_alias="r1",
            package="p1",
            command="c1",
            project_language="python",
        )
        backend.create_audit_record(
            job_id="j2",
            repo_alias="r2",
            package="p2",
            command="c2",
            project_language="java",
        )
        backend.create_audit_record(
            job_id="j3",
            repo_alias="r1",
            package="p3",
            command="c3",
            project_language="python",
        )

        records, total = backend.query_audit_records(repo_alias="r1")
        assert total == 2

        records, total = backend.query_audit_records(project_language="java")
        assert total == 1
        assert records[0]["package"] == "p2"

    def test_query_pagination(self, backend):
        for i in range(5):
            backend.create_audit_record(
                job_id=f"j{i}", repo_alias="r", package=f"p{i}", command=f"c{i}"
            )

        records, total = backend.query_audit_records(limit=2, offset=0)
        assert total == 5
        assert len(records) == 2

    def test_create_with_node_id(self, backend):
        record_id = backend.create_audit_record(
            job_id="job-n1",
            repo_alias="repo1",
            package="pkg",
            command="cmd",
            node_id="node-001",
        )
        assert isinstance(record_id, int) and record_id > 0
        records, _ = backend.query_audit_records(job_id="job-n1")
        assert records[0].get("node_id") == "node-001"

    def test_query_returns_empty_for_no_match(self, backend):
        records, total = backend.query_audit_records(job_id="nonexistent")
        assert total == 0
        assert records == []

    def test_create_multiple_records_returns_incrementing_ids(self, backend):
        id1 = backend.create_audit_record(
            job_id="j1", repo_alias="r", package="p1", command="c1"
        )
        id2 = backend.create_audit_record(
            job_id="j2", repo_alias="r", package="p2", command="c2"
        )
        assert id2 > id1

    def test_query_all_fields_present(self, backend):
        backend.create_audit_record(
            job_id="job-full",
            repo_alias="repo-full",
            package="pkg-full",
            command="cmd-full",
            project_path="/some/path",
            project_language="python",
            project_build_system="pip",
            reasoning="test reason",
            username="user1",
            node_id="node-1",
        )
        records, _ = backend.query_audit_records(job_id="job-full")
        r = records[0]
        assert r["job_id"] == "job-full"
        assert r["repo_alias"] == "repo-full"
        assert r["package"] == "pkg-full"
        assert r["command"] == "cmd-full"
        assert r["project_path"] == "/some/path"
        assert r["project_language"] == "python"
        assert r["project_build_system"] == "pip"
        assert r["reasoning"] == "test reason"
        assert r["username"] == "user1"
        assert r["node_id"] == "node-1"


class TestBackendRegistrySCIPAudit:
    def test_registry_has_scip_audit_field(self):
        from code_indexer.server.storage.factory import BackendRegistry
        import dataclasses

        fields = {f.name for f in dataclasses.fields(BackendRegistry)}
        assert "scip_audit" in fields

    def test_factory_sqlite_creates_scip_audit_backend(self, tmp_path):
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.storage.protocols import SCIPAuditBackend

        data_dir = str(tmp_path / "data")
        Path(data_dir).mkdir(parents=True)
        # groups.db must exist at parent level
        (tmp_path / "groups.db").touch()

        registry = StorageFactory._create_sqlite_backends(data_dir)
        assert isinstance(registry.scip_audit, SCIPAuditBackend)
