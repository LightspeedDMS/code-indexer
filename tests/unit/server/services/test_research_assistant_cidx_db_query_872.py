"""
Unit tests for Story #872: Research Agent SQLite/PostgreSQL Database Access.

Tests verify:
- _allow_rules() includes cidx-db-query.sh with a fully-qualified absolute path
- _run_claude_background() injects CIDX_SERVER_DATA_DIR and CIDX_REPO_ROOT into
  the subprocess environment

Following TDD methodology: Tests written FIRST before implementing (RED phase).
"""

import json
import threading
import tempfile
import shutil
import pytest
from unittest.mock import patch, MagicMock

BACKGROUND_THREAD_WAIT_SECONDS = 5.0


@pytest.fixture
def temp_db():
    """Create temporary SQLite database file path for ResearchAssistantService."""
    import os

    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test.db")
    yield db_path
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def research_service(temp_db):
    """Create ResearchAssistantService with temporary database."""
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.services.research_assistant_service import (
        ResearchAssistantService,
    )

    schema = DatabaseSchema(db_path=temp_db)
    schema.initialize_database()
    return ResearchAssistantService(db_path=temp_db)


def _run_and_capture(research_service, capture_mode="calls"):
    """
    Execute a prompt and capture subprocess.run calls or env dicts.

    Args:
        research_service: Service under test.
        capture_mode: 'calls' to capture cmd lists, 'envs' to capture env dicts.

    Returns:
        List of captured items (cmd lists or env dicts).
    """
    session = research_service.create_session()
    session_id = session["id"]

    captured = []
    done_event = threading.Event()

    def capture_run(cmd, **kwargs):
        if capture_mode == "calls":
            captured.append(list(cmd))
        else:
            captured.append(kwargs.get("env", {}))
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Claude response"
        mock_result.stderr = ""
        done_event.set()
        return mock_result

    target = "code_indexer.server.services.research_assistant_service.subprocess.run"
    with patch(target, side_effect=capture_run):
        research_service.execute_prompt(session_id, "Test question")
        done_event.wait(timeout=BACKGROUND_THREAD_WAIT_SECONDS)

    return captured


def _get_settings(cmd):
    """Extract and parse --settings JSON from a command list."""
    settings_idx = cmd.index("--settings")
    return json.loads(cmd[settings_idx + 1])


@pytest.mark.slow
def test_db_query_script_present_in_allow_rules(research_service):
    """
    AC5: _allow_rules() must include a Bash rule for cidx-db-query.sh using a
    fully-qualified absolute path, mirroring the cidx-meta-cleanup.sh precedent.
    The rule must be in the form: Bash(/absolute/path/scripts/cidx-db-query.sh *)
    """
    calls = _run_and_capture(research_service, capture_mode="calls")
    assert len(calls) >= 1, "subprocess.run must have been called"

    allow_list = _get_settings(calls[0])["permissions"]["allow"]
    db_query_rules = [r for r in allow_list if "cidx-db-query.sh" in r]

    assert len(db_query_rules) >= 1, (
        f"Allow list must contain a cidx-db-query.sh rule. Got allow list: {allow_list}"
    )
    for rule in db_query_rules:
        assert rule.startswith("Bash(/"), (
            f"cidx-db-query.sh rule must use a fully-qualified absolute path. "
            f"Got: {rule!r}. Expected form: Bash(/path/to/scripts/cidx-db-query.sh *)"
        )
        assert "/scripts/cidx-db-query.sh" in rule, (
            f"Rule must reference scripts/cidx-db-query.sh. Got: {rule!r}"
        )


@pytest.mark.slow
def test_cidx_server_data_dir_and_repo_root_in_subprocess_env(research_service):
    """
    Story #872: Both CIDX_SERVER_DATA_DIR and CIDX_REPO_ROOT must be present in
    the subprocess environment so cidx-db-query.sh can auto-detect the database.
    """
    envs = _run_and_capture(research_service, capture_mode="envs")
    assert len(envs) >= 1, "subprocess.run must have been called"

    env = envs[0]
    cidx_keys = [k for k in env if "CIDX" in k]

    assert "CIDX_SERVER_DATA_DIR" in env, (
        "Story #872: CIDX_SERVER_DATA_DIR must be set in subprocess env "
        "so cidx-db-query.sh can locate config.json and the SQLite database. "
        f"Got CIDX env keys: {cidx_keys}"
    )
    assert "CIDX_REPO_ROOT" in env, (
        "Story #872: CIDX_REPO_ROOT must be set in subprocess env "
        "so cidx-db-query.sh can be located by its absolute path. "
        f"Got CIDX env keys: {cidx_keys}"
    )
