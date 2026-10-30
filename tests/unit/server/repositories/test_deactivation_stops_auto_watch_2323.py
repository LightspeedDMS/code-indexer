"""#2323 front door: deactivating an activated repository stops its server
auto-watch before the workspace is removed, nothing recreates the workspace,
and the same alias activates again.

Real pieces, in a child with the test network guard: GoldenRepoManager,
ActivatedRepoManager with LocalCloneBackend (real `cp`, real `cidx
fix-config`), the MCP handler handle_create_file, FileCRUDService, the
AutoWatchManager singleton with its DaemonWatchManager thread and watchdog
observer. Stand-ins: the access service (the caller is an admin), an unused
background job manager (the activation/deactivation workers are called
directly), and the server-managed endpoint enforcement is wrapped -- it
still runs, then the endpoint is pointed at the local fake.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Dict

import pytest

from tests.utils.cidx_fake_provider_repo import FakeProviderRepo
from tests.unit.services.test_watch_cycle_searchable_2093 import (  # noqa: F401
    indexed_repo,
)

pytestmark = [pytest.mark.slow, pytest.mark.timeout(600)]

SCENARIO = r"""
import json, os, shutil, sys, threading, time
from datetime import datetime, timezone
from pathlib import Path

source, endpoint, data_dir = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])

import code_indexer.server.utils.server_managed_provider_settings as managed

_enforce = managed.enforce_server_managed_provider_settings


def _enforce_then_route_to_fake(config):
    _enforce(config)
    config.voyage_ai.api_endpoint = endpoint


managed.enforce_server_managed_provider_settings = _enforce_then_route_to_fake

from code_indexer.server import app as app_module
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import files
from code_indexer.server.repositories.activated_repo_manager import ActivatedRepoManager
from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.services.auto_watch_manager import auto_watch_manager
from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend

golden_path = data_dir / "golden-repos" / "example-golden"
golden_path.parent.mkdir(parents=True)
shutil.copytree(source, golden_path, symlinks=True)
golden = GoldenRepoManager(data_dir=str(data_dir))
golden._sqlite_backend.add_repo(
    alias="example-golden",
    repo_url=str(golden_path),
    default_branch="main",
    clone_path=str(golden_path),
    created_at=datetime.now(timezone.utc).isoformat(),
    enable_temporal=False,
    temporal_options=None,
)


class UnusedJobs:
    pass


arm = ActivatedRepoManager(
    data_dir=str(data_dir),
    golden_repo_manager=golden,
    background_job_manager=UnusedJobs(),
    clone_backend=LocalCloneBackend(),
)


class Access:
    def is_admin_user(self, username):
        return True


app_module.app.state.activated_repo_manager = arm
app_module.app.state.access_filtering_service = Access()
user = User(
    username="example-user",
    password_hash="unused",
    role=UserRole.ADMIN,
    created_at=datetime.now(),
)


def activate():
    return arm._do_activate_repository(
        username="example-user",
        golden_repo_alias="example-golden",
        branch_name="main",
        user_alias="example-repo",
    )


def wait_until(condition, seconds):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.1)
    return condition()


def watch_threads():
    return [t.name for t in threading.enumerate() if t.name == "DaemonWatchThread" and t.is_alive()]


state = {}
assert activate()["success"]
workspace = arm.get_activated_repo_path("example-user", "example-repo")
response = files.handle_create_file(
    {"repository_alias": "example-repo", "file_path": "src/created.py",
     "content": "def created():\n    return 'DEACTIVATIONMARKER'\n"},
    user,
)
state["create"] = json.loads(response["content"][0]["text"]).get("success")


def handler():
    entry = auto_watch_manager._watch_state.get(workspace)
    return None if entry is None else entry["watch_instance"].watch_handler


def watch_indexed():
    current = handler()
    return current is not None and bool(getattr(current, "files_processed_count", 0))


def cycle_in_flight():
    current = handler()
    return current is not None and bool(getattr(current, "processing_in_progress", False))


mode = sys.argv[4]
if mode == "idle":
    state["watch_indexed_write"] = wait_until(watch_indexed, 90)
else:
    state["watch_indexed_write"] = wait_until(cycle_in_flight, 90)

import logging

errors = []


class Errors(logging.Handler):
    def emit(self, record):
        errors.append(f"{record.name}: {record.getMessage()}")


watch_handler = handler()
logging.getLogger().addHandler(Errors(level=logging.ERROR))
started = time.monotonic()
arm._do_deactivate_repository(username="example-user", user_alias="example-repo")
state["deactivate_seconds"] = time.monotonic() - started
thread = getattr(watch_handler, "processing_thread", None)
state["processing_alive_after_deactivate"] = bool(thread and thread.is_alive())
state["watch_threads_after_deactivate"] = watch_threads()
state["watching_after_deactivate"] = auto_watch_manager.is_watching(workspace)
state["exists_after_deactivate"] = os.path.exists(workspace)
time.sleep(10)
state["exists_after_debounces"] = os.path.exists(workspace)
try:
    state["reactivated"] = activate()["success"]
except Exception as e:
    state["reactivated"] = f"{type(e).__name__}: {e}"
auto_watch_manager.shutdown()
state["errors"] = errors
print("RESULT " + json.dumps(state))
"""

HOLD_SECONDS = 8.0


@pytest.mark.parametrize("mode", ["idle", "mid_cycle"])
def test_deactivation_stops_the_auto_watch_and_alias_reactivates(
    indexed_repo: FakeProviderRepo,  # noqa: F811
    mode: str,
) -> None:
    data_dir = indexed_repo.repo.parent / "server-data"
    if mode == "mid_cycle":
        # ^ invariant HOLD_SECONDS > git_aware_watch_handler.STOP_JOIN_SECONDS => the stop's bounded join gives up mid-cycle
        indexed_repo.server.ledger.begin_run("held", hold_seconds=HOLD_SECONDS)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            SCENARIO,
            str(indexed_repo.repo),
            f"{indexed_repo.server.base_url}/v1/embeddings",
            str(data_dir),
            mode,
        ],
        cwd=indexed_repo.repo.parent,
        env=indexed_repo.env,
        capture_output=True,
        text=True,
        timeout=500,
    )
    blocked = (
        indexed_repo.blocked_network_log.read_text()
        if indexed_repo.blocked_network_log.exists()
        else ""
    )
    assert blocked == "", f"scenario attempted network access: {blocked}"
    assert proc.returncode == 0, proc.stderr[-4000:]
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
    assert lines, proc.stdout[-3000:] + proc.stderr[-3000:]
    state: Dict[str, object] = json.loads(lines[-1][len("RESULT ") :])

    assert state["create"] is True
    assert state["watch_indexed_write"] is True
    assert state["processing_alive_after_deactivate"] is False
    assert state["errors"] == []
    assert state["watch_threads_after_deactivate"] == []
    assert state["watching_after_deactivate"] is False
    assert state["exists_after_deactivate"] is False
    assert state["exists_after_debounces"] is False
    assert state["reactivated"] is True
