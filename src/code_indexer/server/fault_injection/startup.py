"""
Fault injection harness startup wiring.

Story #746 — Phase E startup guards.

This module contains the guard logic and wiring for the fault injection harness.
It is intentionally separate from lifespan.py so it can be unit-tested without
spinning up the full server lifecycle.

Four startup scenarios (Story #746 Scenarios 1-4):

  Scenario 1 (harness disabled — default):
    fault_injection_enabled=false in config.json
    -> service NOT instantiated, router NOT registered, endpoints return 404.

  Scenario 2 (enabled but missing nonprod ack):
    fault_injection_enabled=true, fault_injection_nonprod_ack=false
    -> log CRITICAL + sys.exit(1)

  Scenario 3 (enabled on production):
    fault_injection_enabled=true, deployment_environment="production"
    -> log CRITICAL + sys.exit(1)

  Scenario 4 (enabled, ack present, non-production):
    fault_injection_enabled=true, fault_injection_nonprod_ack=true,
    deployment_environment != "production"
    -> instantiate FaultInjectionService with random.SystemRandom(),
       create HttpClientFactory wrapping the service (stored on app.state),
       register /admin/fault-injection/* router,
       log WARNING "FAULT INJECTION HARNESS ACTIVE (non-prod mode)"
"""

from __future__ import annotations

import logging
import random
import sys
from typing import Any, Optional

logger = logging.getLogger(__name__)


def wire_fault_injection(app: Any, config: Any) -> Optional[Any]:
    """
    Evaluate fault injection bootstrap config and wire the harness if enabled.

    Parameters
    ----------
    app:
        The FastAPI application instance.  The function sets
        ``app.state.fault_injection_service`` (to None or the active service)
        and ``app.state.http_client_factory``.  Conditionally registers the
        admin router.
    config:
        A ``ServerConfig`` instance (or any object with
        ``fault_injection_enabled``, ``fault_injection_nonprod_ack``, and
        ``telemetry_config.deployment_environment`` attributes).

    Returns
    -------
    FaultInjectionService | None
        The active service instance when the harness is enabled and all guards
        pass.  None otherwise.

    Side Effects
    ------------
    - Sets ``app.state.fault_injection_service`` unconditionally (None or service).
    - Sets ``app.state.http_client_factory`` (plain factory or fault-aware factory).
    - Registers the ``/admin/fault-injection/*`` router on *app* when enabled.
    - Logs WARNING when harness is active.
    - Calls ``sys.exit(1)`` when production guard or ack guard fires.
    """
    from code_indexer.server.fault_injection.fault_injection_service import (
        FaultInjectionService,
    )
    from code_indexer.server.fault_injection.http_client_factory import (
        HttpClientFactory,
    )
    from code_indexer.server.fault_injection.router import (
        router as fault_router,
        set_service_on_app_state,
    )

    # Scenario 1: harness disabled (default path — no-op)
    if not config.fault_injection_enabled:
        app.state.fault_injection_service = None
        app.state.http_client_factory = HttpClientFactory(fault_injection_service=None)
        return None

    # Scenario 3: production hard-fail (checked before ack — unambiguous signal)
    deployment_env = ""
    if config.telemetry_config is not None:
        deployment_env = (config.telemetry_config.deployment_environment or "").lower()
    if deployment_env == "production":
        logger.critical(
            "FAULT INJECTION HARNESS REFUSED: fault_injection_enabled=true is FORBIDDEN "
            "when deployment_environment=production. Fix config.json and restart."
        )
        sys.exit(1)

    # Scenario 2: ack missing
    if not config.fault_injection_nonprod_ack:
        logger.critical(
            "FAULT INJECTION HARNESS REFUSED: fault_injection_enabled=true requires "
            "fault_injection_nonprod_ack=true to confirm this is a non-production "
            "server. Add both keys to config.json and restart."
        )
        sys.exit(1)

    # Scenario 4: all guards pass — instantiate and wire
    svc = FaultInjectionService(enabled=True, rng=random.SystemRandom())
    set_service_on_app_state(app, svc)

    factory = HttpClientFactory(fault_injection_service=svc)
    app.state.http_client_factory = factory

    # Register the admin router onto the already-created FastAPI app.
    app.include_router(fault_router)

    logger.warning(
        "FAULT INJECTION HARNESS ACTIVE (non-prod mode) — "
        "all outbound provider requests may be intercepted."
    )
    return svc
