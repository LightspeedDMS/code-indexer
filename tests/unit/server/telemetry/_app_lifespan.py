"""The one place tests set the time limit for running the app's lifespan in
process (asgi_lifespan.LifespanManager).

App startup plus shutdown takes 3.5-5.3 s even on an idle machine, so the
library's default 5 s limits fail under test-gate load. Production startup has
no such limit; these limits only keep a genuine hang bounded.

Importable both as ``tests.unit.server.telemetry._app_lifespan`` (pytest) and
as ``_app_lifespan`` from the subprocess harnesses in this directory (a script's
own directory is first on ``sys.path``).
"""

from __future__ import annotations

from typing import Any

from asgi_lifespan import LifespanManager

APP_STARTUP_TIMEOUT_SECONDS = 30.0
APP_SHUTDOWN_TIMEOUT_SECONDS = 30.0


def app_lifespan(
    app: Any, *, shutdown_timeout: float = APP_SHUTDOWN_TIMEOUT_SECONDS
) -> LifespanManager:
    """A LifespanManager for ``app`` with the shared realistic limits.

    ``shutdown_timeout`` may only be raised, for a harness whose shutdown does
    measured extra work (e.g. flushing telemetry exporters).
    """
    if shutdown_timeout < APP_SHUTDOWN_TIMEOUT_SECONDS:
        raise ValueError(
            f"shutdown_timeout {shutdown_timeout} is below the shared limit "
            f"{APP_SHUTDOWN_TIMEOUT_SECONDS}"
        )
    return LifespanManager(
        app,
        startup_timeout=APP_STARTUP_TIMEOUT_SECONDS,
        shutdown_timeout=shutdown_timeout,
    )
