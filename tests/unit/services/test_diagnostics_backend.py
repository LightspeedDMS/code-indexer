import pytest


class TestDiagnosticsBackend:
    @pytest.fixture
    def backend(self, tmp_path):
        from code_indexer.server.storage.sqlite_backends import DiagnosticsSqliteBackend

        b = DiagnosticsSqliteBackend(str(tmp_path / "t.db"))
        yield b
        b.close()

    def test_satisfies_protocol(self, backend):
        from code_indexer.server.storage.protocols import DiagnosticsBackend

        assert isinstance(backend, DiagnosticsBackend)

    def test_registry_has_field(self):
        from code_indexer.server.storage.factory import BackendRegistry
        import dataclasses

        assert "diagnostics" in {f.name for f in dataclasses.fields(BackendRegistry)}
