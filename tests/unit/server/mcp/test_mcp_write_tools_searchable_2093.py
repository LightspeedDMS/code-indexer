"""#2093 front door: files written through the MCP write tools become
searchable (semantic and FTS) with their new content only, while the
server's auto-watch runs.

Real pieces, in a child with the test network guard: the MCP handlers
(handle_create_file / handle_edit_file / handle_delete_file), FileCRUDService,
_start_auto_watch_if_needed, the AutoWatchManager singleton, its
DaemonWatchManager thread and watchdog observer, and `cidx query`.
Stand-ins: the activated-repo registry and the access service on app.state
(this repo is "activated" for the caller, who is an admin), and the
server-managed endpoint enforcement is wrapped -- it still runs, then the
endpoint is pointed at the local fake (tests/utils/fake_voyage_provider.py).
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Dict

import pytest

from tests.utils.cidx_fake_provider_repo import FakeProviderRepo
from tests.unit.services.test_watch_cycle_searchable_2093 import (  # noqa: F401
    BETA,
    GAMMA,
    NEW,
    OLD,
    indexed_repo,
)

pytestmark = [pytest.mark.slow, pytest.mark.timeout(600)]

FRONT_DOOR = r"""
import hashlib, json, os, subprocess, sys, time
from datetime import datetime
from pathlib import Path

repo, endpoint = Path(sys.argv[1]), sys.argv[2]
OLD, NEW, GAMMA = sys.argv[3], sys.argv[4], sys.argv[5]

import code_indexer.server.utils.server_managed_provider_settings as managed

_enforce = managed.enforce_server_managed_provider_settings


def _enforce_then_route_to_fake(config):
    _enforce(config)
    config.voyage_ai.api_endpoint = endpoint


managed.enforce_server_managed_provider_settings = _enforce_then_route_to_fake

from code_indexer.server import app as app_module
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import files
from code_indexer.server.services.auto_watch_manager import auto_watch_manager


class Registry:
    def user_has_activated_repo(self, username, alias):
        return alias == "example-repo"

    def get_activated_repo_path(self, username, user_alias):
        return str(repo)


class Access:
    def is_admin_user(self, username):
        return True


app_module.app.state.activated_repo_manager = Registry()
app_module.app.state.access_filtering_service = Access()
user = User(
    username="example-user",
    password_hash="unused",
    role=UserRole.ADMIN,
    created_at=datetime.now(),
)


def call(handler, **params):
    data = json.loads(handler(dict(repository_alias="example-repo", **params), user)["content"][0]["text"])
    assert data.get("success"), data
    return data


def query(*args):
    proc = subprocess.run(
        [sys.executable, "-m", "code_indexer.cli", "query", *args,
         "--rerank-query", "", "--quiet", "--limit", "50"],
        cwd=repo, env=os.environ, capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        error = (
            f"QUERY-FAILED rc={proc.returncode} {args!r}: "
            f"{proc.stdout[-1500:]} {proc.stderr[-1500:]}"
        )
        query_errors.append(error)
        return error
    return proc.stdout + proc.stderr


query_errors = []
started = time.monotonic()
call(files.handle_create_file, file_path="src/gamma.py", content=GAMMA)
call(
    files.handle_edit_file,
    file_path="src/alpha.py",
    old_string=OLD,
    new_string=NEW,
    content_hash=hashlib.sha256(OLD.encode()).hexdigest(),
)
call(files.handle_delete_file, file_path="src/beta.py")


from code_indexer.services.tantivy_index_manager import TantivyIndexManager


def fts(token):
    # the server's FTS read path (semantic_query_manager): never a writer
    index = TantivyIndexManager(repo / ".code-indexer" / "tantivy_index")
    index.open_for_search()
    return json.dumps(sorted(r["path"] for r in index.search(query_text=token, limit=50)))


def snapshot():
    return {
        "semantic": query("alpha NEWCONTENTTOKEN version"),
        "fts": {t: fts(t) for t in ("NEWCONTENTTOKEN", "CREATEDTOKEN",
                                    "OLDCONTENTTOKEN", "DELETEDTOKEN")},
    }


def converged(s):
    sem, fts = s["semantic"], s["fts"]
    return ("NEWCONTENTTOKEN" in sem and "CREATEDTOKEN" in sem
            and "OLDCONTENTTOKEN" not in sem and "DELETEDTOKEN" not in sem
            and "stale" not in sem.lower()
            and "alpha.py" in fts["NEWCONTENTTOKEN"] and "gamma.py" in fts["CREATEDTOKEN"]
            and "alpha.py" not in fts["OLDCONTENTTOKEN"] and "beta.py" not in fts["DELETEDTOKEN"])


state = snapshot()
while not converged(state) and time.monotonic() - started < 90:
    time.sleep(1)
    state = snapshot()
state["seconds"] = round(time.monotonic() - started, 1)
state["query_errors"] = query_errors
state["watching"] = auto_watch_manager.is_watching(str(repo))
handler = auto_watch_manager._watch_state[str(repo)]["watch_instance"].watch_handler
state["metadata"] = [i.progressive_metadata.metadata_path.name for i in handler.indexers]
auto_watch_manager.stop_watch(str(repo))
print("RESULT " + json.dumps(state))
"""


def test_mcp_writes_are_searchable_with_new_content_only(
    indexed_repo: FakeProviderRepo,  # noqa: F811
) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            FRONT_DOOR,
            str(indexed_repo.repo),
            f"{indexed_repo.server.base_url}/v1/embeddings",
            OLD,
            NEW,
            GAMMA,
        ],
        cwd=indexed_repo.repo,
        env=indexed_repo.env,
        capture_output=True,
        text=True,
        timeout=400,
    )
    blocked = (
        indexed_repo.blocked_network_log.read_text()
        if indexed_repo.blocked_network_log.exists()
        else ""
    )
    assert blocked == "", f"front door attempted network access: {blocked}"
    assert proc.returncode == 0, proc.stderr[-4000:]
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
    assert lines, proc.stdout[-3000:] + proc.stderr[-3000:]
    state: Dict[str, object] = json.loads(lines[-1][len("RESULT ") :])
    semantic = str(state["semantic"])
    fts = state["fts"]
    assert isinstance(fts, dict)

    assert state["query_errors"] == []
    assert isinstance(state["seconds"], float) and state["seconds"] <= 30, state
    assert state["watching"] is True
    assert "NEWCONTENTTOKEN" in semantic and "CREATEDTOKEN" in semantic
    assert "OLDCONTENTTOKEN" not in semantic and "DELETEDTOKEN" not in semantic
    assert "stale" not in semantic.lower()
    assert "alpha.py" in fts["NEWCONTENTTOKEN"]
    assert "gamma.py" in fts["CREATEDTOKEN"]
    assert "alpha.py" not in fts["OLDCONTENTTOKEN"]
    assert "beta.py" not in fts["DELETEDTOKEN"]
    assert state["metadata"] == ["metadata-voyage-ai.json"]
    assert BETA  # the deleted file's content, written by the fixture
