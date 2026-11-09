"""issue-65021 regression guard: ``scip_backend`` is validated at startup.

The only production caller of the scip wiring
(``lifecycle.funnel_scip_generation``) swallows a config ``RuntimeError`` per
job as a fail-soft SKIP, so startup must call ``check_scip_backend_at_startup``.
That helper owns the policy (tested in test_scip_backend_wiring.py): a removed
or unknown ``scip_backend`` -- e.g. the removed ``delegated`` -- raises and
must STOP startup; anything else is logged. Since modular phase 4d the call is
the scip feature's ``on_startup`` (``cidx_scip.server.wiring.start``); the core
refuses startup when a feature's ``on_startup`` raises.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, cast
from unittest.mock import patch

import pytest

from cidx_sdk.capabilities import CONFIG
from cidx_scip.server import runtime, wiring
from cidx_scip.server.backend_wiring import UnsupportedScipBackendError

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


class _Context:
    """A feature context whose config section holds *values* only."""

    def __init__(self, values: Dict[str, object]) -> None:
        self._values = values

    def has_capability(self, name: str) -> bool:
        return name == CONFIG

    def capability(self, name: str) -> object:
        values = self._values

        class _Config:
            def section(self) -> Dict[str, object]:
                return dict(values)

        return _Config()


@pytest.fixture(autouse=True)
def no_feature_wiring_left() -> Iterator[None]:
    yield
    wiring.stop()


class TestScipBackendValidationAtStartup:
    def test_a_removed_backend_stops_startup(self):
        ctx = cast(Any, _Context({"scip_backend": "delegated"}))
        with pytest.raises(UnsupportedScipBackendError):
            wiring.start(ctx)
        # Nothing half-started is left behind.
        assert runtime.context() is None
        assert runtime.AUDIT_REPOSITORY.get() is None

    def test_startup_validates_the_configured_backend(self):
        ctx = cast(Any, _Context({"scip_backend": "local"}))
        seen = []

        def check(get_config: Any) -> None:
            seen.append(get_config().scip_backend)
            raise UnsupportedScipBackendError("stop here")

        with (
            patch(
                "cidx_scip.server.backend_wiring.check_scip_backend_at_startup",
                side_effect=check,
            ),
            pytest.raises(UnsupportedScipBackendError),
        ):
            wiring.start(ctx)
        assert seen == ["local"]

    def test_the_old_lifespan_call_is_gone(self):
        source = _LIFESPAN_PATH.read_text()
        assert "check_scip_backend_at_startup(" not in source
        assert "validate_scip_backend_config(" not in source
