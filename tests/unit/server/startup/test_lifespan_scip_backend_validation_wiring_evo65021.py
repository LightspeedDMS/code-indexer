"""EVO-65021 regression guard: lifespan eagerly validates scip_backend config.

The module ``scip_backend_wiring.py`` documents a fail-loud policy, but the only
production caller (``scip_lifecycle.funnel_scip_generation``) swallows a config
``RuntimeError`` per-job as a fail-soft SKIP. This guard asserts that startup
wires an eager ``validate_scip_backend_config(...)`` call (mirroring the
``build_snapshot_manager`` clone-backend wiring) so a misconfiguration is named
LOUDLY once at boot rather than vanishing into per-job skips.

Source-text + source-order guards (mirroring
test_lifespan_temporal_metadata_backend_wiring_1313.py). They MUST fail before
the wiring and pass after.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


class TestLifespanScipBackendValidationWiring:
    def test_validate_call_present_in_startup(self):
        source = _LIFESPAN_PATH.read_text()
        assert "validate_scip_backend_config(" in source, (
            "lifespan.py must call validate_scip_backend_config(...) at startup "
            "so a misconfigured scip_backend fails loud at boot"
        )

    def test_validation_runs_during_startup_before_yield(self):
        source = _LIFESPAN_PATH.read_text()
        yield_pos = source.find("yield  # Server is now running")
        call_pos = source.find("validate_scip_backend_config(")

        assert yield_pos != -1, "could not locate the lifespan yield boundary"
        assert call_pos != -1
        assert call_pos < yield_pos, (
            "validate_scip_backend_config(...) must run during STARTUP "
            "(before the yield)"
        )

    def test_wiring_is_still_non_fatal_for_server_startup(self):
        # lifespan.py documents this validation as "Non-fatal (ERROR-logged,
        # never crashes startup)": scip_backend is opt-in and defaults to local,
        # so a misconfiguration must be named LOUDLY yet must NOT abort query
        # serving. This load-bearing property is guarded here (mirroring the
        # non-fatal assertion in
        # test_lifespan_temporal_metadata_backend_wiring_1313.py): the validate
        # call must stay wrapped in try/except Exception, so a boot-crash
        # regression cannot slip through.
        source = _LIFESPAN_PATH.read_text()
        call_pos = source.find("validate_scip_backend_config(get_config")
        assert (
            call_pos != -1
        ), "could not locate the validate_scip_backend_config(...) call site"
        try_pos = source.rfind("try:", 0, call_pos)
        except_pos = source.find("except Exception", call_pos)
        assert try_pos != -1 and except_pos != -1 and try_pos < call_pos < except_pos, (
            "the scip_backend startup validation must stay wrapped in "
            "try/except Exception so a misconfigured scip_backend is "
            "ERROR-logged but never aborts server startup (non-fatal guarantee)"
        )
