"""Bug #1575 Part C remediation (independent re-review): all THREE `cidx
index` spawns in ActivatedRepoIndexManager (semantic, fts, temporal) funnel
through the SAME `_run_subprocess_with_telemetry` helper, which never set
CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV at all -- unlike
`_execute_post_clone_workflow`'s golden-repo Popen calls (already fixed),
this shared convergence point silently inherited the unsafe
enabled-by-default fallback even in postgres/cluster mode.

Mirrors test_activated_repo_index_manager_temporal_pg_env_wiring_1313.py's
fixtures and calling convention, parameterized over (method, storage_mode)
since one fix at the shared helper covers all three index-type methods.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.services.activated_repo_index_manager import (
    ActivatedRepoIndexManager,
)
from code_indexer.server.utils.config_manager import ServerConfig
from code_indexer.storage.shared.hnsw_sync_state import (
    CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
)
from tests.utils.env_assertions import assert_env_absent, assert_env_value

_FAKE_POSTGRES_DSN = "postgresql://not-a-real-credential@example.invalid/placeholder"


@pytest.fixture
def temp_data_dir():
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def mock_background_job_manager():
    manager = Mock(spec=BackgroundJobManager)
    manager.submit_job = Mock(return_value=str(uuid.uuid4()))
    manager.list_jobs = Mock(return_value={"jobs": [], "total": 0})
    return manager


@pytest.fixture
def mock_activated_repo_manager(temp_data_dir):
    manager = Mock()
    repo_path = str(Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo")
    manager.get_activated_repo_path = Mock(return_value=repo_path)
    return manager


@pytest.fixture
def index_manager(
    temp_data_dir, mock_background_job_manager, mock_activated_repo_manager
):
    return ActivatedRepoIndexManager(
        data_dir=temp_data_dir,
        background_job_manager=mock_background_job_manager,
        activated_repo_manager=mock_activated_repo_manager,
    )


@pytest.fixture
def initialized_repo(tmp_path):
    (tmp_path / ".code-indexer").mkdir()
    (tmp_path / ".code-indexer" / "config.json").write_text("{}")
    return tmp_path


def _run_and_capture(index_manager, method_name, repo_path, server_config, **kwargs):
    captured_calls: list = []

    def _run(args, env=None, **run_kwargs):
        captured_calls.append({"args": args, "env": env})
        return Mock(returncode=0, stdout="", stderr="")

    with (
        patch(
            "code_indexer.server.services.activated_repo_index_manager"
            ".run_cancellable_subprocess",
            side_effect=_run,
        ),
        patch(
            "code_indexer.server.services.activated_repo_index_manager.get_config_service"
        ) as mock_get_cfg_svc,
        # resolve_hnsw_sync_epoch_env_var() performs its OWN independent
        # deferred import of get_config_service from its canonical module
        # path, so the name-imported reference above does not affect it.
        patch(
            "code_indexer.server.services.config_service.get_config_service"
        ) as mock_get_cfg_svc_for_resolver,
    ):
        mock_get_cfg_svc.return_value.get_config.return_value = server_config
        mock_get_cfg_svc_for_resolver.return_value.get_config.return_value = (
            server_config
        )

        getattr(index_manager, method_name)(str(repo_path), **kwargs)

    assert len(captured_calls) == 1
    return captured_calls[0]["env"]


_POSTGRES_CONFIG = ServerConfig(
    server_dir="/opt/cidx-server",
    storage_mode="postgres",
    postgres_dsn=_FAKE_POSTGRES_DSN,
)
_SQLITE_CONFIG = ServerConfig(server_dir="/opt/cidx-server", storage_mode="sqlite")


@pytest.mark.parametrize(
    "method_name, kwargs",
    [
        ("_execute_semantic_indexing", {"clear": False}),
        ("_execute_fts_indexing", {"clear": False}),
        ("_execute_temporal_indexing", {"clear": False}),
    ],
)
@pytest.mark.parametrize(
    "server_config, expected_var",
    [
        pytest.param(_POSTGRES_CONFIG, "1", id="postgres"),
        pytest.param(_SQLITE_CONFIG, None, id="sqlite"),
    ],
)
def test_hnsw_epoch_env_matches_storage_mode(
    index_manager, initialized_repo, method_name, kwargs, server_config, expected_var
):
    env = _run_and_capture(
        index_manager, method_name, initialized_repo, server_config, **kwargs
    )

    assert env is not None
    if expected_var is None:
        assert_env_absent(
            env,
            CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
            msg=(
                f"sqlite/solo mode must never set this var on the "
                f"{method_name} child -- it defaults enabled"
            ),
        )
    else:
        assert_env_value(
            env,
            CIDX_HNSW_SYNC_EPOCH_POSTGRES_MODE_ENV,
            expected_var,
            msg=(
                f"the {method_name} child must be told it is running in "
                "postgres/cluster mode"
            ),
        )
