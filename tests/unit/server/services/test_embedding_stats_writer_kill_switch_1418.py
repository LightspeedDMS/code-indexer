"""Tests for the EmbeddingStatsWriter 'enabled' kill-switch (Story #1418
Phase 3).

Mirrors the memory_retrieval_enabled kill-switch pattern: when the config
says disabled, the feature must resolve to a no-op regardless of what was
previously installed/set. Unlike memory_retrieval_pipeline (which checks the
flag at the call site), get_active() itself is the check point here so that
ANY previously-installed real writer (InProcessAsyncWriter /
CrossProcessBootstrapWriter) instantly stops recording the moment the
Web UI toggle flips, without needing a new set_active() call.

Critically: the check PEEKS at an already-constructed ConfigService
singleton (via the config_service module's private _config_service global)
rather than lazily constructing one via get_config_service() -- constructing
one has a side effect (it can create a phantom config.json at whatever
directory this process's default resolves to), which would be unacceptable
to trigger from a hot-path read like get_active(). When no singleton exists
yet (e.g. inside a `cidx index` child subprocess that never calls
get_config_service() itself), the kill-switch fails OPEN (enabled=True).
"""

import pytest


@pytest.fixture(autouse=True)
def _reset_writer_and_config_service():
    from code_indexer.server.services.embedding_stats_writer import (
        EmbeddingStatsWriter,
    )
    from code_indexer.server.services.config_service import reset_config_service

    EmbeddingStatsWriter._active = None
    reset_config_service()
    yield
    EmbeddingStatsWriter._active = None
    reset_config_service()


class _StubBackend:
    def insert_batch(self, records: list) -> None:
        pass


class TestKillSwitchDisabled:
    def test_get_active_returns_noop_when_config_disabled(self, tmp_path) -> None:
        from code_indexer.server.services.config_service import (
            ConfigService,
            set_config_service,
        )
        from code_indexer.server.services.embedding_stats_writer import (
            EmbeddingStatsWriter,
            InProcessAsyncWriter,
            NoOpWriter,
        )

        svc = ConfigService(server_dir_path=str(tmp_path))
        svc.update_setting("embedding_stats", "enabled", False)
        set_config_service(svc)

        real_writer = InProcessAsyncWriter(_StubBackend())
        EmbeddingStatsWriter.set_active(real_writer)

        assert isinstance(EmbeddingStatsWriter.get_active(), NoOpWriter)

    def test_disabled_kill_switch_does_not_mutate_active_slot(self, tmp_path) -> None:
        """The real writer stays installed (_active is untouched) -- only
        get_active()'s RETURN VALUE is overridden. Re-enabling later must
        not require a fresh set_active() call."""
        from code_indexer.server.services.config_service import (
            ConfigService,
            set_config_service,
        )
        from code_indexer.server.services.embedding_stats_writer import (
            EmbeddingStatsWriter,
            InProcessAsyncWriter,
        )

        svc = ConfigService(server_dir_path=str(tmp_path))
        svc.update_setting("embedding_stats", "enabled", False)
        set_config_service(svc)

        real_writer = InProcessAsyncWriter(_StubBackend())
        EmbeddingStatsWriter.set_active(real_writer)
        EmbeddingStatsWriter.get_active()  # returns NoOpWriter, but...

        assert EmbeddingStatsWriter._active is real_writer


class TestKillSwitchEnabled:
    def test_get_active_returns_real_writer_when_config_enabled(self, tmp_path) -> None:
        from code_indexer.server.services.config_service import (
            ConfigService,
            set_config_service,
        )
        from code_indexer.server.services.embedding_stats_writer import (
            EmbeddingStatsWriter,
            InProcessAsyncWriter,
        )

        svc = ConfigService(server_dir_path=str(tmp_path))
        set_config_service(svc)  # enabled defaults to True

        real_writer = InProcessAsyncWriter(_StubBackend())
        EmbeddingStatsWriter.set_active(real_writer)

        assert EmbeddingStatsWriter.get_active() is real_writer


class TestKillSwitchFailOpen:
    def test_no_config_service_singleton_defaults_to_enabled(self) -> None:
        """No get_config_service()/set_config_service() call has ever
        happened in this process -- get_active() must NOT construct one
        (side-effect risk); it must fail open and return the installed
        writer unchanged."""
        from code_indexer.server.services.embedding_stats_writer import (
            EmbeddingStatsWriter,
            InProcessAsyncWriter,
        )

        real_writer = InProcessAsyncWriter(_StubBackend())
        EmbeddingStatsWriter.set_active(real_writer)

        assert EmbeddingStatsWriter.get_active() is real_writer

    def test_peek_never_constructs_a_config_service(
        self, tmp_path, monkeypatch
    ) -> None:
        """Regression guard for the side-effect risk: get_active() must
        never call get_config_service() (which would lazily construct one,
        potentially writing a phantom config.json)."""
        import code_indexer.server.services.config_service as config_service_module
        from code_indexer.server.services.embedding_stats_writer import (
            EmbeddingStatsWriter,
        )

        called = {"count": 0}
        original = config_service_module.get_config_service

        def _spy():
            called["count"] += 1
            return original()

        monkeypatch.setattr(config_service_module, "get_config_service", _spy)

        EmbeddingStatsWriter.get_active()

        assert called["count"] == 0

    def test_config_peek_exception_fails_open(self, tmp_path) -> None:
        """A raising get_config() on an already-installed singleton must
        not propagate -- fail open, returning the installed writer."""
        from code_indexer.server.services.config_service import (
            set_config_service,
        )
        from code_indexer.server.services.embedding_stats_writer import (
            EmbeddingStatsWriter,
            InProcessAsyncWriter,
        )

        class _BrokenConfigService:
            def get_config(self):
                raise RuntimeError("boom")

        set_config_service(_BrokenConfigService())  # type: ignore[arg-type]

        real_writer = InProcessAsyncWriter(_StubBackend())
        EmbeddingStatsWriter.set_active(real_writer)

        assert EmbeddingStatsWriter.get_active() is real_writer


if __name__ == "__main__":
    import pytest as _pytest

    _pytest.main([__file__, "-v"])
