"""Front-door harness for the golden-repository and configuration audit tests.

Drives the real REST routes and Web routers (TestClient behind the audit
request context middleware) and the real MCP JSON-RPC ``tools/call``
dispatcher, over real auth components on isolated files
(``self_service_elevation_harness``), a real GoldenRepoManager, a real
RefreshScheduler and GlobalRegistry, a real ConfigService on its own server
directory and a real bound audit store.

Doubles: the background job runner (:class:`RecordingJobManager`, records
submissions, runs nothing), the Web CSRF check (always valid) and elevation
enforcement (off at both read points).  The acting admin's name is NOT
``admin``, so a hard-coded actor cannot pass.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

from _audit_accounts_support import AuditRow, AuditStore, bound_audit_store
from _audit_repos_support import (
    EXAMPLE_ALIAS,
    RecordingJobManager,
    make_golden_repo_manager,
    make_refresh_scheduler,
    register_repo,
)
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.middleware.audit_request_context import (
    AuditRequestContextMiddleware,
    bind_audit_request_context,
    build_request_context,
    reset_audit_request_context,
)
from code_indexer.server.web.auth import SESSION_COOKIE_NAME
from tests.unit.server.self_service_elevation_harness import (
    SelfServiceStack,
    build_stack,
    enforcement,
)

ACTING_ADMIN = "example-second-admin"
_PASSWORD = "SecureP@ssw0rd!XyZ789"


class DoorsEnv:
    def __init__(
        self,
        stack: SelfServiceStack,
        store: AuditStore,
        client: TestClient,
        admin: User,
        manager: Any,
        jobs: RecordingJobManager,
        config_service: Any,
        server_dir: Path,
    ) -> None:
        self.stack = stack
        self.store = store
        self.client = client
        self.admin = admin
        self.manager = manager
        self.jobs = jobs
        self.config_service = config_service
        self.server_dir = server_dir
        self.bearer, _jti = stack.bearer(admin)
        self.cookie = stack.session_cookie(admin)
        self.git_url = _local_git_url(server_dir.parent / "remote.git")

    def rest(self, method: str, path: str, **kwargs: Any):
        headers = {"Authorization": f"Bearer {self.bearer}"}
        return self.client.request(method, path, headers=headers, **kwargs)

    def web(self, method: str, path: str, **kwargs: Any):
        self.client.cookies.set(SESSION_COOKIE_NAME, self.cookie)
        try:
            return self.client.request(method, path, follow_redirects=False, **kwargs)
        finally:
            self.client.cookies.clear()

    def mcp(self, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        from code_indexer.server.mcp.protocol import process_jsonrpc_request

        token = bind_audit_request_context(build_request_context("/mcp", "127.0.0.1"))
        try:
            response = asyncio.run(
                process_jsonrpc_request(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": tool, "arguments": arguments},
                    },
                    self.admin,
                    session_id=None,
                )
            )
        finally:
            reset_audit_request_context(token)
        assert "result" in response, response
        payload: Dict[str, Any] = json.loads(response["result"]["content"][0]["text"])
        return payload

    def rows(self, action_type: str) -> List[AuditRow]:
        return [r for r in self.store.rows(action_type) if r.action_type == action_type]

    def only_row(self, action_type: str) -> AuditRow:
        rows = self.rows(action_type)
        assert len(rows) == 1, rows
        return rows[0]


def _local_git_url(path: Path) -> str:
    """A reachable ``file://`` git remote (an empty bare repository)."""
    import subprocess

    subprocess.run(
        ["git", "init", "--bare", "--quiet", str(path)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return f"file://{path}"


def _provider_ready_repo(golden_dir: Path, server_db: Path) -> None:
    """Alias pointer, base clone config and global-registry row for the repo."""
    from code_indexer.global_repos.alias_manager import AliasManager
    from code_indexer.server.storage.sqlite_backends.global_repos_backend import (
        GlobalReposSqliteBackend,
    )

    repo_dir = golden_dir / EXAMPLE_ALIAS
    (repo_dir / ".code-indexer").mkdir(parents=True, exist_ok=True)
    (repo_dir / ".code-indexer" / "config.json").write_text(
        json.dumps({"embedding_providers": ["cohere"]})
    )
    AliasManager(str(golden_dir / "aliases")).create_alias(
        f"{EXAMPLE_ALIAS}-global", str(repo_dir), repo_name=EXAMPLE_ALIAS
    )
    GlobalReposSqliteBackend(str(server_db)).register_repo(
        alias_name=f"{EXAMPLE_ALIAS}-global",
        repo_name=EXAMPLE_ALIAS,
        repo_url="https://git.example.com/org/example.git",
        index_path=str(repo_dir),
    )


def front_door_env(tmp_path: Path, monkeypatch: Any) -> Iterator[DoorsEnv]:
    """Yield a :class:`DoorsEnv`; every patched singleton is restored after."""
    from code_indexer.server.auth.audit_logger import password_audit_logger
    from code_indexer.server.global_routes import git_settings as git_settings_routes
    from code_indexer.server.global_routes import routes as global_routes
    from code_indexer.server.mcp.handlers._utils import app_module
    from code_indexer.server.routers import api_keys as api_keys_router
    from code_indexer.server.routers import llm_creds as llm_creds_router
    from code_indexer.server.routers import provider_indexes as provider_router
    from code_indexer.server.routers.inline_admin_ops import (
        register_admin_ops_routes,
    )
    from code_indexer.server.services import config_service as config_service_module
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.web import routes as web_routes
    from code_indexer.config import ConfigManager

    stack = build_stack(tmp_path, monkeypatch)
    admin = stack.user_manager.create_user(ACTING_ADMIN, _PASSWORD, UserRole.ADMIN)

    server_dir = tmp_path / "server"
    (server_dir / "data").mkdir(parents=True)
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(server_dir))
    # Exactly one embedding provider is configured: voyage-ai.
    monkeypatch.setenv("VOYAGE_API_KEY", "example-provider-key")
    monkeypatch.delenv("CO_API_KEY", raising=False)
    config_svc = config_service_module.ConfigService(server_dir_path=str(server_dir))
    config_svc.load_config()
    monkeypatch.setattr(config_service_module, "_config_service", config_svc)
    server_db = server_dir / "data" / "cidx_server.db"
    DatabaseSchema(str(server_db)).initialize_database()

    jobs = RecordingJobManager()
    manager = make_golden_repo_manager(tmp_path, jobs)
    register_repo(manager)
    golden_dir = Path(manager.golden_repos_dir)
    _provider_ready_repo(golden_dir, server_db)
    scheduler = make_refresh_scheduler(tmp_path, jobs)
    lifecycle = SimpleNamespace(refresh_scheduler=scheduler)
    from code_indexer.server.storage.sqlite_backends.global_repos_backend import (
        GlobalReposSqliteBackend,
    )

    backend_registry = SimpleNamespace(
        global_repos=GlobalReposSqliteBackend(str(server_db))
    )
    from code_indexer.server.services.group_access_manager import GroupAccessManager

    from code_indexer.server.services.access_filtering_service import (
        AccessFilteringService,
    )

    groups = GroupAccessManager(tmp_path / "groups.db")
    admins = groups.get_group_by_name(AccessFilteringService.ADMIN_GROUP_NAME)
    assert admins is not None
    groups.assign_user_to_group(ACTING_ADMIN, admins.id, "harness-setup")
    for name, value in (
        ("group_manager", groups),
        ("access_filtering_service", AccessFilteringService(groups)),
        ("golden_repo_manager", manager),
        ("global_lifecycle_manager", lifecycle),
        ("golden_repos_dir", str(golden_dir)),
        ("background_job_manager", jobs),
        ("backend_registry", backend_registry),
    ):
        monkeypatch.setattr(app_module.app.state, name, value, raising=False)
    monkeypatch.setattr(app_module, "golden_repo_manager", manager, raising=False)
    monkeypatch.setattr(app_module, "background_job_manager", jobs, raising=False)
    monkeypatch.setattr(web_routes, "validate_login_csrf_token", lambda _r, _t: True)
    git_config_path = tmp_path / "git-settings" / ".code-indexer" / "config.json"
    git_config_path.parent.mkdir(parents=True)
    monkeypatch.setattr(
        git_settings_routes,
        "_get_config_manager",
        lambda: ConfigManager(git_config_path),
    )
    monkeypatch.setattr(global_routes, "_golden_repos_dir", str(golden_dir))

    app = FastAPI()
    app.add_middleware(AuditRequestContextMiddleware)
    app.state.global_lifecycle_manager = lifecycle
    app.state.background_job_manager = jobs
    register_admin_ops_routes(
        app,
        jwt_manager=stack.jwt_manager,
        user_manager=stack.user_manager,
        golden_repo_manager=manager,
        background_job_manager=jobs,
        workspace_cleanup_service=None,
        config_service=config_svc,
        server_config=None,
        data_dir=str(tmp_path / "golden-data"),
        job_tracker=None,
    )
    app.include_router(provider_router.router)
    app.include_router(api_keys_router.router)
    app.include_router(llm_creds_router.router)
    app.include_router(global_routes.router)
    app.include_router(git_settings_routes.router, prefix="/api")
    app.include_router(web_routes.web_router, prefix="/admin")

    for store in bound_audit_store(tmp_path / "audit.db"):
        monkeypatch.setattr(password_audit_logger, "_audit_service", store.service)
        with enforcement(False):
            client = TestClient(app, raise_server_exceptions=False)
            yield DoorsEnv(
                stack, store, client, admin, manager, jobs, config_svc, server_dir
            )


def assert_attributed(row: AuditRow, *, source: str, outcome: str = "success") -> None:
    assert (row.actor, row.source, row.outcome) == (ACTING_ADMIN, source, outcome)
    assert row.actor_is_system == 0


def optional_job(row: AuditRow) -> Optional[str]:
    job = row.details.get("job_id")
    return str(job) if job is not None else None
