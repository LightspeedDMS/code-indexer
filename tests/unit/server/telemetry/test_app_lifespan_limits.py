"""Every test that runs the app's lifespan in process uses ONE shared,
realistic time limit (tests/unit/server/telemetry/_app_lifespan.py).

asgi_lifespan's default 5 s startup and shutdown limits are below what app
startup plus shutdown takes on an idle machine, so a direct
``LifespanManager(app)`` fails under load. Production startup has no such
limit; the helper only keeps a genuine hang bounded.
"""

from __future__ import annotations

from pathlib import Path

from tests.unit.server.telemetry._app_lifespan import (
    APP_SHUTDOWN_TIMEOUT_SECONDS,
    APP_STARTUP_TIMEOUT_SECONDS,
)

_TESTS_ROOT = Path(__file__).resolve().parents[3]
_HELPER = Path(__file__).resolve().parent / "_app_lifespan.py"
_SELF = Path(__file__).resolve()


def test_only_the_shared_helper_constructs_a_lifespan_manager() -> None:
    offenders = [
        str(path.relative_to(_TESTS_ROOT))
        for path in sorted(_TESTS_ROOT.rglob("*.py"))
        if path not in (_HELPER, _SELF) and "LifespanManager(" in path.read_text()
    ]
    assert offenders == [], offenders


def test_shared_limits_cover_a_loaded_machine() -> None:
    assert APP_STARTUP_TIMEOUT_SECONDS >= 30
    assert APP_SHUTDOWN_TIMEOUT_SECONDS >= 30
