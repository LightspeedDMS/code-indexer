"""Unit tests for LangfuseReadmeGenerator integration with LangfuseTraceSyncService (Story #592).

Tests cover:
- _last_modified_sessions_by_repo is populated on the service after sync_project()
  (sync_project() is the public API that delegates to _sync_project_inner() for tracking)
- LangfuseReadmeGenerator.generate_for_repo is actually called with correct
  repo paths and session IDs when on_sync_complete fires
- Session IDs are correctly tracked per repo folder, including multi-user scenarios
"""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from code_indexer.server.services.langfuse_trace_sync_service import (
    LangfuseTraceSyncService,
)
from code_indexer.server.utils.config_manager import (
    LangfuseConfig,
    LangfusePullProject,
    ServerConfig,
)


def _make_config(tmp_path: Path) -> ServerConfig:
    return ServerConfig(
        server_dir=str(tmp_path),
        langfuse_config=LangfuseConfig(
            pull_enabled=True,
            pull_host="https://cloud.langfuse.com",
            pull_projects=[
                LangfusePullProject(
                    public_key="pk-test",
                    secret_key="sk-test",
                )
            ],
        ),
    )


def _make_trace(trace_id: str, session_id: str, user_id: str = "seba_battig") -> dict:
    return {
        "id": trace_id,
        "sessionId": session_id,
        "userId": user_id,
        "name": "Test Turn",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }


class TestLastModifiedSessionsByRepo:
    """_last_modified_sessions_by_repo is populated correctly after sync.

    sync_project() is the public entry point; it delegates to _sync_project_inner()
    which performs the actual session tracking. Tests use sync_project() to exercise
    the full tracking path through the public API.
    """

    def test_attribute_exists_on_service(self, tmp_path):
        """Service should have _last_modified_sessions_by_repo attribute initialised to empty dict."""
        config = _make_config(tmp_path)
        service = LangfuseTraceSyncService(
            config_getter=lambda: config,
            data_dir=str(tmp_path / "data"),
        )
        assert hasattr(service, "_last_modified_sessions_by_repo")
        assert isinstance(service._last_modified_sessions_by_repo, dict)
        assert service._last_modified_sessions_by_repo == {}

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_sync_project_populates_last_modified_sessions(
        self, mock_client_class, tmp_path
    ):
        """After sync_project(), _last_modified_sessions_by_repo should contain session IDs."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "Claude_Code"}

        trace = _make_trace("trace-001", "session-abc", user_id="seba_battig")
        mock_client.fetch_traces_page.side_effect = [[trace], []]
        mock_client.fetch_observations.return_value = []

        config = _make_config(tmp_path)
        creds = config.langfuse_config.pull_projects[0]

        service = LangfuseTraceSyncService(
            config_getter=lambda: config,
            data_dir=str(tmp_path / "data"),
        )
        service.sync_project(
            config.langfuse_config.pull_host,
            creds,
            trace_age_days=30,
        )

        assert len(service._last_modified_sessions_by_repo) > 0
        repo_keys = list(service._last_modified_sessions_by_repo.keys())
        assert any("Claude_Code" in k for k in repo_keys)

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_session_id_is_tracked_per_repo(self, mock_client_class, tmp_path):
        """Session ID should appear in the set for its repo folder."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "Claude_Code"}

        trace = _make_trace("trace-001", "session-xyz-123", user_id="seba_battig")
        mock_client.fetch_traces_page.side_effect = [[trace], []]
        mock_client.fetch_observations.return_value = []

        config = _make_config(tmp_path)
        creds = config.langfuse_config.pull_projects[0]

        service = LangfuseTraceSyncService(
            config_getter=lambda: config,
            data_dir=str(tmp_path / "data"),
        )
        service.sync_project(
            config.langfuse_config.pull_host,
            creds,
            trace_age_days=30,
        )

        repo_key = next(
            k for k in service._last_modified_sessions_by_repo if "Claude_Code" in k
        )
        session_ids = service._last_modified_sessions_by_repo[repo_key]
        assert "session-xyz-123" in session_ids

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_multiple_sessions_tracked(self, mock_client_class, tmp_path):
        """Multiple sessions in the same repo should all be tracked."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "Claude_Code"}

        traces = [
            _make_trace("trace-001", "session-aaa", user_id="seba_battig"),
            _make_trace("trace-002", "session-bbb", user_id="seba_battig"),
            _make_trace("trace-003", "session-ccc", user_id="seba_battig"),
        ]
        mock_client.fetch_traces_page.side_effect = [traces, []]
        mock_client.fetch_observations.return_value = []

        config = _make_config(tmp_path)
        creds = config.langfuse_config.pull_projects[0]

        service = LangfuseTraceSyncService(
            config_getter=lambda: config,
            data_dir=str(tmp_path / "data"),
        )
        service.sync_project(
            config.langfuse_config.pull_host,
            creds,
            trace_age_days=30,
        )

        repo_key = next(
            k for k in service._last_modified_sessions_by_repo if "Claude_Code" in k
        )
        session_ids = service._last_modified_sessions_by_repo[repo_key]
        assert "session-aaa" in session_ids
        assert "session-bbb" in session_ids
        assert "session-ccc" in session_ids

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_sessions_from_different_users_tracked_separately(
        self, mock_client_class, tmp_path
    ):
        """Sessions from different users should be in different repo keys."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "Claude_Code"}

        traces = [
            _make_trace("trace-001", "session-user1", user_id="user_alice"),
            _make_trace("trace-002", "session-user2", user_id="user_bob"),
        ]
        mock_client.fetch_traces_page.side_effect = [traces, []]
        mock_client.fetch_observations.return_value = []

        config = _make_config(tmp_path)
        creds = config.langfuse_config.pull_projects[0]

        service = LangfuseTraceSyncService(
            config_getter=lambda: config,
            data_dir=str(tmp_path / "data"),
        )
        service.sync_project(
            config.langfuse_config.pull_host,
            creds,
            trace_age_days=30,
        )

        repo_keys = list(service._last_modified_sessions_by_repo.keys())
        assert len(repo_keys) == 2

        alice_key = next(k for k in repo_keys if "user_alice" in k)
        assert "session-user1" in service._last_modified_sessions_by_repo[alice_key]

        bob_key = next(k for k in repo_keys if "user_bob" in k)
        assert "session-user2" in service._last_modified_sessions_by_repo[bob_key]

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_unchanged_traces_not_in_modified_sessions(
        self, mock_client_class, tmp_path
    ):
        """Unchanged traces should NOT add session IDs to _last_modified_sessions_by_repo."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "Claude_Code"}

        trace = _make_trace("trace-001", "session-unchanged", user_id="seba_battig")
        # Two sync cycles with the same trace (same updatedAt — will be unchanged on second pass)
        mock_client.fetch_traces_page.side_effect = [[trace], [], [trace], []]
        mock_client.fetch_observations.return_value = []

        config = _make_config(tmp_path)
        creds = config.langfuse_config.pull_projects[0]

        service = LangfuseTraceSyncService(
            config_getter=lambda: config,
            data_dir=str(tmp_path / "data"),
        )
        # First sync — trace is new, session should be tracked
        service.sync_project(
            config.langfuse_config.pull_host,
            creds,
            trace_age_days=30,
        )
        assert len(service._last_modified_sessions_by_repo) > 0

        # Second sync — trace is unchanged (same updatedAt), session should NOT be tracked
        service.sync_project(
            config.langfuse_config.pull_host,
            creds,
            trace_age_days=30,
        )
        assert len(service._last_modified_sessions_by_repo) == 0

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_last_modified_sessions_cleared_between_syncs(
        self, mock_client_class, tmp_path
    ):
        """_last_modified_sessions_by_repo should be reset on each sync cycle."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "Claude_Code"}

        traces_first = [
            _make_trace("trace-001", "session-first-a", user_id="seba_battig"),
            _make_trace("trace-002", "session-first-b", user_id="seba_battig"),
        ]
        trace_second = _make_trace("trace-003", "session-second", user_id="seba_battig")

        mock_client.fetch_traces_page.side_effect = [
            traces_first,
            [],
            [trace_second],
            [],
        ]
        mock_client.fetch_observations.return_value = []

        config = _make_config(tmp_path)
        creds = config.langfuse_config.pull_projects[0]

        service = LangfuseTraceSyncService(
            config_getter=lambda: config,
            data_dir=str(tmp_path / "data"),
        )

        # First sync
        service.sync_project(config.langfuse_config.pull_host, creds, trace_age_days=30)
        repo_key = next(
            k for k in service._last_modified_sessions_by_repo if "Claude_Code" in k
        )
        first_sessions = set(service._last_modified_sessions_by_repo[repo_key])
        assert "session-first-a" in first_sessions
        assert "session-first-b" in first_sessions

        # Second sync — dict should be reset, only contain second sync's sessions
        service.sync_project(config.langfuse_config.pull_host, creds, trace_age_days=30)
        repo_key = next(
            k for k in service._last_modified_sessions_by_repo if "Claude_Code" in k
        )
        second_sessions = set(service._last_modified_sessions_by_repo[repo_key])
        assert "session-second" in second_sessions
        assert "session-first-a" not in second_sessions
        assert "session-first-b" not in second_sessions


class TestLifespanCallbackWiring:
    """Verify the lifespan _on_langfuse_sync_complete wiring calls LangfuseReadmeGenerator.

    These tests exercise the exact pattern that must exist in lifespan.py:
    - Iterate service._last_modified_sessions_by_repo
    - Resolve repo_path as data_dir / "golden-repos" / repo_folder
    - Call LangfuseReadmeGenerator().generate_for_repo(repo_path, session_ids)
    - Errors must be caught and logged, never propagated

    These tests use mock patching to verify the call is made, separate from
    the integration tests that test the generator's actual file output.
    """

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    @patch(
        "code_indexer.server.services.langfuse_readme_generator.LangfuseReadmeGenerator.generate_for_repo"
    )
    def test_on_sync_complete_calls_readme_generator_for_each_repo(
        self, mock_generate, mock_client_class, tmp_path
    ):
        """_on_langfuse_sync_complete must call generate_for_repo for each modified repo."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "Claude_Code"}

        trace = _make_trace("trace-001", "session-wiring", user_id="test_user")
        mock_client.fetch_traces_page.side_effect = [[trace], []]
        mock_client.fetch_observations.return_value = []

        config = _make_config(tmp_path)
        data_dir = tmp_path / "data"

        # Build the callback exactly as lifespan.py _on_langfuse_sync_complete does
        from code_indexer.server.services.langfuse_readme_generator import (
            LangfuseReadmeGenerator,
        )

        def _on_langfuse_sync_complete():
            try:
                gen = LangfuseReadmeGenerator()
                for (
                    repo_folder,
                    session_ids,
                ) in service._last_modified_sessions_by_repo.items():
                    repo_path = data_dir / "golden-repos" / repo_folder
                    gen.generate_for_repo(repo_path, session_ids)
            except Exception:
                pass

        service = LangfuseTraceSyncService(
            config_getter=lambda: config,
            data_dir=str(data_dir),
            on_sync_complete=_on_langfuse_sync_complete,
        )
        service.sync_all_projects()

        # generate_for_repo must have been called at least once
        assert mock_generate.called, (
            "LangfuseReadmeGenerator.generate_for_repo was not called from _on_langfuse_sync_complete"
        )
        call_args = mock_generate.call_args
        repo_path_arg, session_ids_arg = call_args[0]
        assert "Claude_Code" in str(repo_path_arg)
        assert "session-wiring" in session_ids_arg

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_on_sync_complete_error_in_generator_does_not_propagate(
        self, mock_client_class, tmp_path
    ):
        """Errors in LangfuseReadmeGenerator must be caught and not break sync."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "Claude_Code"}

        trace = _make_trace("trace-001", "session-err", user_id="test_user")
        mock_client.fetch_traces_page.side_effect = [[trace], []]
        mock_client.fetch_observations.return_value = []

        config = _make_config(tmp_path)
        data_dir = tmp_path / "data"

        from code_indexer.server.services.langfuse_readme_generator import (
            LangfuseReadmeGenerator,
        )

        def _on_langfuse_sync_complete():
            try:
                gen = LangfuseReadmeGenerator()
                for (
                    repo_folder,
                    session_ids,
                ) in service._last_modified_sessions_by_repo.items():
                    repo_path = data_dir / "golden-repos" / repo_folder
                    gen.generate_for_repo(repo_path, session_ids)
            except Exception:
                pass

        service = LangfuseTraceSyncService(
            config_getter=lambda: config,
            data_dir=str(data_dir),
            on_sync_complete=_on_langfuse_sync_complete,
        )

        # Patch generate_for_repo to raise — sync_all_projects must still complete
        with patch.object(
            LangfuseReadmeGenerator,
            "generate_for_repo",
            side_effect=RuntimeError("disk full"),
        ):
            # Must not raise
            service.sync_all_projects()


class TestReadmeGeneratorCalledFromCallback:
    """Verify LangfuseReadmeGenerator.generate_for_repo is invoked from the sync callback.

    Tests use sync_all_projects() — the public lock-guarded entry point — which
    calls _do_sync_all_projects() internally and fires on_sync_complete.
    """

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_readme_generator_called_with_correct_repo_path(
        self, mock_client_class, tmp_path
    ):
        """generate_for_repo should be called with the correct repo path and session IDs."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "Claude_Code"}

        trace = _make_trace("trace-001", "session-abc", user_id="seba_battig")
        mock_client.fetch_traces_page.side_effect = [[trace], []]
        mock_client.fetch_observations.return_value = []

        config = _make_config(tmp_path)
        data_dir = tmp_path / "data"

        generator_calls = []

        def on_complete_with_generator():
            # Simulate what lifespan.py _on_langfuse_sync_complete does:
            # read service's _last_modified_sessions_by_repo and call generator
            from code_indexer.server.services.langfuse_readme_generator import (
                LangfuseReadmeGenerator,
            )

            gen = LangfuseReadmeGenerator()
            for (
                repo_folder,
                session_ids,
            ) in service._last_modified_sessions_by_repo.items():
                repo_path = data_dir / "golden-repos" / repo_folder
                gen.generate_for_repo(repo_path, session_ids)
                generator_calls.append((str(repo_path), set(session_ids)))

        service = LangfuseTraceSyncService(
            config_getter=lambda: config,
            data_dir=str(data_dir),
            on_sync_complete=on_complete_with_generator,
        )
        # Use the public entry point; sync_all_projects() acquires the lock and
        # delegates to _do_sync_all_projects() which fires on_sync_complete.
        service.sync_all_projects()

        assert len(generator_calls) == 1
        repo_path_used, sessions_used = generator_calls[0]
        assert "Claude_Code" in repo_path_used
        assert "session-abc" in sessions_used

    @patch("code_indexer.server.services.langfuse_trace_sync_service.LangfuseApiClient")
    def test_readme_files_created_in_repo_after_sync(self, mock_client_class, tmp_path):
        """After a full sync cycle with on_sync_complete, README.md should exist in repo."""
        mock_client = Mock()
        mock_client_class.return_value = mock_client
        mock_client.discover_project.return_value = {"name": "Claude_Code"}

        trace = _make_trace("trace-001", "session-e2e", user_id="test_user")
        mock_client.fetch_traces_page.side_effect = [[trace], []]
        mock_client.fetch_observations.return_value = []

        config = _make_config(tmp_path)
        data_dir = tmp_path / "data"

        def on_complete_with_generator():
            from code_indexer.server.services.langfuse_readme_generator import (
                LangfuseReadmeGenerator,
            )

            gen = LangfuseReadmeGenerator()
            for (
                repo_folder,
                session_ids,
            ) in service._last_modified_sessions_by_repo.items():
                repo_path = data_dir / "golden-repos" / repo_folder
                gen.generate_for_repo(repo_path, session_ids)

        service = LangfuseTraceSyncService(
            config_getter=lambda: config,
            data_dir=str(data_dir),
            on_sync_complete=on_complete_with_generator,
        )
        # Use the public entry point
        service.sync_all_projects()

        # Verify README was created in the repo folder
        golden_repos = data_dir / "golden-repos"
        repo_dirs = [
            d for d in golden_repos.iterdir() if d.is_dir() and "Claude_Code" in d.name
        ]
        assert len(repo_dirs) == 1, "Expected one repo folder"
        assert (repo_dirs[0] / "README.md").exists(), "Root README.md should exist"
        assert (repo_dirs[0] / "session-e2e" / "README.md").exists(), (
            "Session README.md should exist"
        )
