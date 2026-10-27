"""Real X-Ray front doors (MCP handlers, REST route) over a given repository.

Shared by the #2212 and #2211 door tests. The X-Ray engine, xray-cli and the
search-event writer (SQLite) are real; only job infrastructure not under test
is the recording harness's BackgroundJobManager/JobTracker fakes.
"""

from __future__ import annotations

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator
from unittest.mock import patch

from tests.unit.server.services.search_event_recording_harness import (
    FakeBackgroundJobManager,
    FakeJobTracker,
    make_user,
    recorder,
)

_XRAY_MODULES = (
    "code_indexer.server.mcp.handlers.xray._search",
    "code_indexer.server.mcp.handlers.xray._explore",
    "code_indexer.server.mcp.handlers.xray_batch",
    "code_indexer.server.routes.xray_routes",
)
REPO_ALIAS = "example-repo-global"


class DoorEnv(SimpleNamespace):
    bjm: FakeBackgroundJobManager
    state: SimpleNamespace
    cidx_meta: Path

    def mcp(self, handler: Any, args: Dict[str, Any], username: str) -> Dict[str, Any]:
        loop = asyncio.new_event_loop()
        try:
            response = loop.run_until_complete(handler(dict(args), make_user(username)))
        finally:
            loop.close()
        return dict(json.loads(response["content"][0]["text"]))

    def rest_search(self, body: Dict[str, Any], username: str) -> Dict[str, Any]:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from code_indexer.server.auth.dependencies import get_current_user
        from code_indexer.server.routes.xray_routes import router

        app = FastAPI()
        app.include_router(router)
        app.state.search_event_log_writer = self.state.search_event_log_writer
        app.state.access_filtering_service = self.state.access_filtering_service
        app.dependency_overrides[get_current_user] = lambda: make_user(username)
        response = TestClient(app).post("/api/xray/search", json=body)
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]
        self.bjm.run_pending()
        return dict(self.bjm.results[job_id])


@contextmanager
def xray_door_env(tmp_path: Path, repo: Path) -> Iterator[DoorEnv]:
    cidx_meta = tmp_path / "golden-repos" / "cidx-meta"
    cidx_meta.mkdir(parents=True)
    with ExitStack() as stack:
        # ^ why lazy trigram build off, not on; its daemon thread outlives teardown => logs to a closed capture stream
        stack.enter_context(patch.dict(os.environ, {"CIDX_TRIGRAM_LAZY_BUILD": "0"}))
        executor = stack.enter_context(ThreadPoolExecutor(max_workers=2))
        rec = stack.enter_context(recorder("sqlite", tmp_path))
        bjm = FakeBackgroundJobManager()
        state = SimpleNamespace(
            search_event_log_writer=rec.writer,
            xray_executor=executor,
            xray_cell_limiter=None,
            payload_cache=None,
            access_filtering_service=SimpleNamespace(is_admin_user=lambda u: True),
        )
        app_module = SimpleNamespace(
            app=SimpleNamespace(state=state),
            background_job_manager=bjm,
            job_tracker=FakeJobTracker(),
            activated_repo_manager=None,
            golden_repo_manager=SimpleNamespace(golden_repos_dir=str(tmp_path)),
        )
        stack.enter_context(
            patch("code_indexer.server.mcp.handlers._utils.app_module", app_module)
        )
        stack.enter_context(
            patch(
                "code_indexer.server.mcp.handlers.xray._infra._get_cidx_meta_path",
                return_value=cidx_meta,
            )
        )
        stack.enter_context(
            patch(
                "code_indexer.server.mcp.handlers.xray_batch._get_cidx_meta_path",
                return_value=cidx_meta,
            )
        )
        # ^ why seeds marked ensured; seeding commits to git, not under test here
        stack.enter_context(
            patch("code_indexer.server.mcp.handlers.xray._infra._seeds_ensured", True)
        )
        for mod in _XRAY_MODULES:
            stack.enter_context(
                patch(f"{mod}._resolve_repo_path", return_value=str(repo))
            )
        yield DoorEnv(bjm=bjm, state=state, cidx_meta=cidx_meta)
