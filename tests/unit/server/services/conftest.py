"""
Shared fixtures for JobTracker unit tests.

Story #310: JobTracker Class, TrackedJob Dataclass, Schema Migration (Epic #261 Story 1A)
"""

import sqlite3
from unittest.mock import patch

import pytest

from code_indexer.server.services.job_tracker import JobTracker


@pytest.fixture
def db_path(tmp_path):
    """Create a temporary database with the full background_jobs table schema."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS background_jobs (
        job_id TEXT PRIMARY KEY NOT NULL,
        operation_type TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        result TEXT,
        error TEXT,
        progress INTEGER NOT NULL DEFAULT 0,
        username TEXT NOT NULL,
        is_admin INTEGER NOT NULL DEFAULT 0,
        cancelled INTEGER NOT NULL DEFAULT 0,
        repo_alias TEXT,
        resolution_attempts INTEGER NOT NULL DEFAULT 0,
        claude_actions TEXT,
        failure_reason TEXT,
        extended_error TEXT,
        language_resolution_status TEXT,
        progress_info TEXT,
        metadata TEXT,
        actor_username TEXT
    )"""
    )
    conn.commit()
    conn.close()
    return str(db)


@pytest.fixture
def tracker(db_path):
    """Create a JobTracker connected to the temporary database."""
    return JobTracker(db_path)


@pytest.fixture(autouse=True)
def _disable_pace_maker_guard():
    with patch("code_indexer.server.services.claude_invoker.enforce_pace_maker_config"):
        with patch(
            "code_indexer.server.services.research_assistant_service.enforce_pace_maker_config"
        ):
            yield


@pytest.fixture
def isolated_research_base_dir(tmp_path_factory):
    """
    Bug #1085: isolated root for Research Assistant session workspaces.

    Every research test (service, security-flags, cli-injection) is redirected
    here via the autouse ``_isolate_research_home`` fixture so that NO test ever
    writes into the developer's real ``~/.cidx-server/research``.

    The root is allocated via ``tmp_path_factory`` in its OWN pytest-managed temp
    directory (NOT the per-test ``tmp_path``), so the autouse redirect never
    pollutes the ``tmp_path`` that unrelated tests inspect. pytest removes the
    factory dirs automatically, so workspaces are torn down after the run.
    """
    return tmp_path_factory.mktemp("research_home")


@pytest.fixture(autouse=True)
def _isolate_research_home(isolated_research_base_dir):
    """
    Bug #1085 (autouse): redirect the Research Assistant default base dir to the
    isolated temp dir for the duration of every test in this package.

    This patches the single ``_default_research_base_dir`` seam so even services
    constructed the legacy way (``ResearchAssistantService(db_path=temp_db)``
    with no explicit ``research_base_dir``) land under the per-test tmp dir and
    are auto-removed by pytest -- the root cause of the 22k leaked dirs.

    Under ``PYTHONPATH=./src`` the service is importable under two distinct
    module objects (``code_indexer...`` and ``src.code_indexer...``); test files
    import via the ``src.`` namespace, so both aliases must be patched.
    """
    import contextlib
    import importlib

    module_names = [
        "code_indexer.server.services.research_assistant_service",
        "src.code_indexer.server.services.research_assistant_service",
    ]
    with contextlib.ExitStack() as stack:
        for module_name in module_names:
            try:
                importlib.import_module(module_name)
            except ImportError:
                continue
            stack.enter_context(
                patch(
                    f"{module_name}._default_research_base_dir",
                    return_value=isolated_research_base_dir,
                )
            )
        yield
