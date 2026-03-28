"""
TDD tests for Phase 2 of app.py modularization (Story #409): AppState.

Verifies that AppState typed class and get_app_state dependency are
importable from code_indexer.server.app_state.

Written FIRST (TDD red phase) to drive the implementation.
"""


class TestAppStateImportable:
    """AppState must be importable from code_indexer.server.app_state."""

    def test_app_state_class_importable(self):
        from code_indexer.server.app_state import AppState

        assert AppState is not None

    def test_get_app_state_importable(self):
        from code_indexer.server.app_state import get_app_state

        assert get_app_state is not None


class TestAppStateAttributes:
    """AppState must have typed attributes for all services stored on app.state."""

    def test_app_state_has_golden_repo_manager(self):
        from code_indexer.server.app_state import AppState

        hints = {}
        for cls in type.mro(AppState):
            if cls is object:
                continue
            hints.update(cls.__annotations__ if hasattr(cls, "__annotations__") else {})
        assert "golden_repo_manager" in hints

    def test_app_state_has_background_job_manager(self):
        from code_indexer.server.app_state import AppState

        hints = AppState.__annotations__
        assert "background_job_manager" in hints

    def test_app_state_has_activated_repo_manager(self):
        from code_indexer.server.app_state import AppState

        assert "activated_repo_manager" in AppState.__annotations__

    def test_app_state_has_repository_listing_manager(self):
        from code_indexer.server.app_state import AppState

        assert "repository_listing_manager" in AppState.__annotations__

    def test_app_state_has_semantic_query_manager(self):
        from code_indexer.server.app_state import AppState

        assert "semantic_query_manager" in AppState.__annotations__

    def test_app_state_has_workspace_cleanup_service(self):
        from code_indexer.server.app_state import AppState

        assert "workspace_cleanup_service" in AppState.__annotations__

    def test_app_state_has_group_manager(self):
        from code_indexer.server.app_state import AppState

        assert "group_manager" in AppState.__annotations__

    def test_app_state_has_audit_service(self):
        from code_indexer.server.app_state import AppState

        assert "audit_service" in AppState.__annotations__

    def test_app_state_has_access_filtering_service(self):
        from code_indexer.server.app_state import AppState

        assert "access_filtering_service" in AppState.__annotations__

    def test_app_state_has_global_lifecycle_manager(self):
        from code_indexer.server.app_state import AppState

        assert "global_lifecycle_manager" in AppState.__annotations__

    def test_app_state_has_query_tracker(self):
        from code_indexer.server.app_state import AppState

        assert "query_tracker" in AppState.__annotations__

    def test_app_state_has_golden_repos_dir(self):
        from code_indexer.server.app_state import AppState

        assert "golden_repos_dir" in AppState.__annotations__

    def test_app_state_has_payload_cache(self):
        from code_indexer.server.app_state import AppState

        assert "payload_cache" in AppState.__annotations__

    def test_app_state_has_llm_lifecycle_service(self):
        from code_indexer.server.app_state import AppState

        assert "llm_lifecycle_service" in AppState.__annotations__

    def test_app_state_has_scheduled_catchup_service(self):
        from code_indexer.server.app_state import AppState

        assert "scheduled_catchup_service" in AppState.__annotations__

    def test_app_state_has_cidx_meta_debouncer(self):
        from code_indexer.server.app_state import AppState

        assert "cidx_meta_debouncer" in AppState.__annotations__

    def test_app_state_has_description_refresh_scheduler(self):
        from code_indexer.server.app_state import AppState

        assert "description_refresh_scheduler" in AppState.__annotations__

    def test_app_state_has_data_retention_scheduler(self):
        from code_indexer.server.app_state import AppState

        assert "data_retention_scheduler" in AppState.__annotations__

    def test_app_state_has_dependency_map_service(self):
        from code_indexer.server.app_state import AppState

        assert "dependency_map_service" in AppState.__annotations__

    def test_app_state_has_self_monitoring_service(self):
        from code_indexer.server.app_state import AppState

        assert "self_monitoring_service" in AppState.__annotations__

    def test_app_state_has_self_monitoring_repo_root(self):
        from code_indexer.server.app_state import AppState

        assert "self_monitoring_repo_root" in AppState.__annotations__

    def test_app_state_has_self_monitoring_github_repo(self):
        from code_indexer.server.app_state import AppState

        assert "self_monitoring_github_repo" in AppState.__annotations__

    def test_app_state_has_telemetry_manager(self):
        from code_indexer.server.app_state import AppState

        assert "telemetry_manager" in AppState.__annotations__

    def test_app_state_has_machine_metrics_exporter(self):
        from code_indexer.server.app_state import AppState

        assert "machine_metrics_exporter" in AppState.__annotations__

    def test_app_state_has_langfuse_sync_service(self):
        from code_indexer.server.app_state import AppState

        assert "langfuse_sync_service" in AppState.__annotations__

    def test_app_state_has_ssh_migration_result(self):
        from code_indexer.server.app_state import AppState

        assert "ssh_migration_result" in AppState.__annotations__

    def test_app_state_has_log_db_path(self):
        from code_indexer.server.app_state import AppState

        assert "log_db_path" in AppState.__annotations__


class TestAppStateInstantiation:
    """AppState must be instantiable with all-None defaults."""

    def test_app_state_instantiates_with_no_args(self):
        from code_indexer.server.app_state import AppState

        state = AppState()
        assert state is not None

    def test_app_state_defaults_all_to_none(self):
        from code_indexer.server.app_state import AppState

        state = AppState()
        assert state.golden_repo_manager is None
        assert state.background_job_manager is None
        assert state.activated_repo_manager is None
        assert state.semantic_query_manager is None

    def test_app_state_allows_attribute_assignment(self):
        from code_indexer.server.app_state import AppState

        state = AppState()
        state.golden_repos_dir = "/some/path"
        assert state.golden_repos_dir == "/some/path"


class TestGetAppStateDependency:
    """get_app_state must work as a FastAPI dependency extracting AppState from request."""

    def test_get_app_state_is_callable(self):
        from code_indexer.server.app_state import get_app_state

        assert callable(get_app_state)

    def test_get_app_state_accepts_request_parameter(self):
        from code_indexer.server.app_state import get_app_state
        import inspect

        sig = inspect.signature(get_app_state)
        assert "request" in sig.parameters

    def test_get_app_state_extracts_state_from_request(self):
        from code_indexer.server.app_state import get_app_state, AppState

        # Simulate a FastAPI request with app.state set
        class MockState:
            pass

        class MockApp:
            state = MockState()

        class MockRequest:
            app = MockApp()

        # Attach AppState instance to mock app.state
        app_state = AppState()
        app_state.golden_repos_dir = "/test/path"
        MockRequest.app.state.app_state = app_state

        result = get_app_state(MockRequest())
        assert result is app_state
        assert result.golden_repos_dir == "/test/path"
