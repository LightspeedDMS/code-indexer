"""A real app for the SIEM Web operator routes and their REST twins.

REAL auth components (users, web sessions, TOTP, elevation windows, JWTs)
from ``self_service_elevation_harness``; the real audit-attribution
middleware; the real Web and REST SIEM routers; a REAL scheduler over the
test backend (SQLite or PostgreSQL) whose only addition is a call counter,
so "the service was not called" is observable without mocking it.
"""

from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import User
from code_indexer.server.fault_injection.http_client_factory import HttpClientFactory
from code_indexer.server.services.siem_delivery.credential import SiemCredentialStore
from code_indexer.server.services.siem_delivery.scheduler import (
    CycleView,
    SiemDeliveryScheduler,
)
from code_indexer.server.services.siem_delivery.timings import HARNESS_TIMINGS
from tests.fixtures.secops_sidecar.harness import SidecarHandle
from tests.unit.server.self_service_elevation_harness import (
    SelfServiceStack,
    build_stack,
    enforcement,
)

from .backends import SiemBackendHarness

# conftest.py registers the ``ops`` fixture from this module, so the conftest
# helpers are imported inside the functions below (no import cycle).


class CountingScheduler(SiemDeliveryScheduler):
    """Counts every committed-config read: every admin action and every
    operator read starts with one, so ``calls == 0`` proves no service ran."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.calls = 0

    def committed_view(self) -> CycleView:
        self.calls += 1
        return super().committed_view()


def build_app(
    scheduler: Optional[SiemDeliveryScheduler], startup_error: Optional[str] = None
) -> FastAPI:
    from code_indexer.server.middleware.audit_request_context import (
        AuditRequestContextMiddleware,
    )
    from code_indexer.server.routers.siem_delivery_admin import router as rest_router
    from code_indexer.server.web.elevation_web_routes import (
        router as elevation_web_router,
    )
    from code_indexer.server.web.siem_delivery_routes import (
        siem_delivery_web_router,
    )

    app = FastAPI()
    app.add_middleware(AuditRequestContextMiddleware)
    app.include_router(siem_delivery_web_router, prefix="/admin")
    app.include_router(rest_router)
    app.include_router(elevation_web_router)
    app.state.siem_delivery_scheduler = scheduler
    app.state.siem_delivery_startup_error = startup_error
    return app


@dataclass
class OpsEnv:
    app: FastAPI
    stack: SelfServiceStack
    backend: SiemBackendHarness
    scheduler: CountingScheduler
    config: Any  # the committed-config collaborator (test_admin_parity)
    sidecar: SidecarHandle
    totp_secret: str  # ADMIN's enrolled TOTP secret (real codes for elevation)

    def web(self, user: User, elevated: bool = False) -> Tuple[TestClient, str]:
        """A browser: real signed session + CSRF cookies; returns the token."""
        from code_indexer.server.web import routes as web_routes
        from code_indexer.server.web.auth import SESSION_COOKIE_NAME

        client = TestClient(self.app, follow_redirects=False)
        session = self.stack.session_cookie(user)
        client.cookies.set(SESSION_COOKIE_NAME, session)
        token = secrets.token_urlsafe(16)  # one per browser session
        signed = web_routes._get_csrf_serializer().dumps(token, salt="csrf-login")
        client.cookies.set(web_routes.CSRF_COOKIE_NAME, signed)
        if elevated:
            self.stack.elevate(session, user.username)
        return client, token

    def rest(self, user: User, elevated: bool = False) -> TestClient:
        token, jti = self.stack.bearer(user)
        client = TestClient(self.app, headers={"Authorization": f"Bearer {token}"})
        if elevated:
            self.stack.elevate(jti, user.username)
        return client

    def user(self, name: str) -> User:
        found = self.stack.user_manager.get_user(name)
        if found is None:
            raise LookupError(f"test user {name!r} was not created")
        return found

    @property
    def users(self) -> Dict[str, User]:
        return {name: self.user(name) for name in (ADMIN, ADMIN_NO_TOTP, NORMAL)}

    def audit_rows(self, action_type: str) -> List[Dict[str, Any]]:
        return self.backend.db.read(
            lambda tx: tx.query(
                "SELECT * FROM audit_logs WHERE action_type = ? ORDER BY id",
                (action_type,),
            )
        )


# action -> the audit type its shared service writes (spec section 5)
ACTIONS = {
    "canary": "siem_canary_sent",
    "confirm-visible": "siem_canary_visibility_confirmed",
    "resume": "siem_delivery_resumed",
    "requeue": "siem_quarantine_requeued",
    "acknowledge": "siem_batch_acknowledged",
    "rebatch": "siem_batch_rebatched",
    "retarget": "siem_destination_retargeted",
    "abandon": "siem_destination_abandoned",
}
WEB, REST = "/admin/siem-delivery", "/api/admin/siem-delivery"


def twin_calls(
    ops: "OpsEnv", action: str, tag: str
) -> Tuple[str, Dict[str, Any], str, Optional[Dict[str, Any]]]:
    """Fresh state for ONE door's call, then (web path, web form fields,
    REST path, REST JSON body)."""
    from code_indexer.server.services.siem_delivery import admin

    from .test_ops_documents import _halt_on
    from .test_ops_pages import _destination, _seed_batch, _seed_queue

    b, key, batch = ops.backend, f"harness:{tag}", f"batch-{tag}"
    if action == "confirm-visible":
        run = admin.run_canary(ops.scheduler, ADMIN)
        ids = run["expected_product_log_ids"]
        path = "canary/confirm-visible"
        web = {"canary_run_id": run["canary_run_id"], "visible_id": ids}
        rest = {"canary_run_id": run["canary_run_id"], "visible_product_log_ids": ids}
        return f"{WEB}/{path}", web, f"{REST}/{path}", rest
    if action == "requeue":
        uuids = _seed_queue(b, 2, status="quarantined")
        path = "quarantine/requeue"
        return (
            f"{WEB}/{path}",
            {"event_uuid": uuids},
            f"{REST}/{path}",
            {"event_uuids": uuids},
        )
    if action in ("acknowledge", "rebatch", "resume"):
        dest = ops.scheduler.committed_view().destination
        assert dest is not None
        _seed_batch(b, batch, seconds=0, dest=dest.key)
        _seed_queue(b, 3, dest=dest.key, status="batched", batch_id=batch)
        _halt_on(b, batch)
        if action == "resume":
            return f"{WEB}/resume", {}, f"{REST}/resume", None
        path = f"batches/{batch}/{action}"
        return f"{WEB}/{path}", {}, f"{REST}/{path}", None
    if action in ("retarget", "abandon"):
        _destination(b, key)
        _seed_queue(b, 3, dest=key)
        path = f"destinations/{key}/{action}"
        web = {"confirm_word": "ABANDON"} if action == "abandon" else {}
        return f"{WEB}/{path}", web, f"{REST}/{path}", None
    return f"{WEB}/canary", {}, f"{REST}/canary", None


def make_scheduler(backend: SiemBackendHarness, config: Any) -> CountingScheduler:
    """One server process's scheduler over *backend*: registered, one loop
    cycle run, call counter reset."""
    from .conftest import TEST_ENCRYPTION_KEY
    from .test_admin_parity import _NoJobs

    scheduler = CountingScheduler(
        db=backend.db,
        config_service=config,
        background_job_manager=_NoJobs(),
        http_client_factory=HttpClientFactory(fault_injection_service=None),
        harness_active=True,
        node_id=None,
        credential_store=SiemCredentialStore(backend.db, TEST_ENCRYPTION_KEY),
        timings=HARNESS_TIMINGS,
    )
    scheduler.register_process()
    scheduler.run_cycle()
    scheduler.calls = 0
    return scheduler


ADMIN = "admin-example"
ADMIN_NO_TOTP = "admin-nototp"
NORMAL = "user-example"


@pytest.fixture()
def ops(
    siem_backend: SiemBackendHarness,
    siem_sidecar: SidecarHandle,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[OpsEnv]:
    from code_indexer.server.auth.user_manager import UserRole
    from code_indexer.server.services import audit_capture
    from code_indexer.server.services.siem_delivery import capture

    from .conftest import harness_section, seeded_store
    from .test_admin_parity import _CommittedConfig

    stack = build_stack(tmp_path, monkeypatch)
    stack.create_user(ADMIN, UserRole.ADMIN)
    secret = stack.enroll_mfa(ADMIN)
    stack.create_user(ADMIN_NO_TOTP, UserRole.ADMIN)
    stack.create_user(NORMAL)
    capture.reset_capture_state_for_tests()
    audit_capture.mark_server_process()
    audit_capture.bind_audit_service(siem_backend.audit, node_id=None)
    config = _CommittedConfig(asdict(harness_section(siem_sidecar)))
    seeded_store(siem_backend.db, siem_sidecar)
    scheduler = make_scheduler(siem_backend, config)
    app = build_app(scheduler)
    try:
        with enforcement(True):
            yield OpsEnv(
                app, stack, siem_backend, scheduler, config, siem_sidecar, secret
            )
    finally:
        audit_capture.clear_audit_service()
        audit_capture.reset_server_process_mark()
        capture.reset_capture_state_for_tests()
