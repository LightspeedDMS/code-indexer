"""Wiring guards for Story #1198: launch.json materialization in lifespan and service_init.

TDD: source-text and source-order guards following the established pattern
in this directory to avoid transitive fastapi import failures.

Verifies:
  AC4  — service_init.py calls materialize_launch_config() after initialize_runtime_db.
  AC2/FIX-3 — lifespan.py cluster branch has register_on_change_callback and
               materialize_launch_config both appearing BEFORE _config_svc.start_config_reload.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)
_SERVICE_INIT_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "service_init.py"
)

# Scan window (chars) after initialize_runtime_db for materialize call
_SERVICE_INIT_SCAN_WINDOW_CHARS = 500


class TestServiceInitMaterializeWiring:
    """AC4: service_init.py calls materialize_launch_config() after initialize_runtime_db."""

    def test_materialize_call_after_initialize_runtime_db(self) -> None:
        """service_init.py must have concrete materialize_launch_config() call after init_db."""
        source = _SERVICE_INIT_PATH.read_text()
        init_db_pos = source.find("initialize_runtime_db")
        assert init_db_pos != -1, "initialize_runtime_db not found in service_init.py"
        after_init = source[init_db_pos : init_db_pos + _SERVICE_INIT_SCAN_WINDOW_CHARS]
        assert "materialize_launch_config()" in after_init, (
            "service_init.py must call materialize_launch_config() "
            "in the 500 chars following initialize_runtime_db (Story #1198 AC4)"
        )


class TestLifespanCb1Wiring:
    """AC2/FIX-3: cluster branch registers materialize_launch_config as cb1."""

    def _cluster_region(self) -> str:
        """Source text of lifespan.py from _config_svc.set_connection_pool to _config_svc.start_config_reload."""
        source = _LIFESPAN_PATH.read_text()
        # Use the specific ConfigService pool assignment, not the generic pattern
        pool_pos = source.find("_config_svc.set_connection_pool(_cluster_pool)")
        assert pool_pos != -1, (
            "_config_svc.set_connection_pool(_cluster_pool) not found in lifespan.py"
        )
        reload_pos = source.find("_config_svc.start_config_reload", pool_pos)
        assert reload_pos != -1, (
            "_config_svc.start_config_reload not found after _config_svc.set_connection_pool"
        )
        return source[pool_pos:reload_pos]

    def test_register_callback_and_materialize_both_in_cluster_region(self) -> None:
        """Cluster region must contain both register_on_change_callback and materialize_launch_config."""
        region = self._cluster_region()
        assert "register_on_change_callback" in region, (
            "lifespan.py cluster branch must call register_on_change_callback "
            "BEFORE _config_svc.start_config_reload (Story #1198 AC2/FIX-3)"
        )
        assert "materialize_launch_config" in region, (
            "lifespan.py cluster branch must register materialize_launch_config "
            "BEFORE _config_svc.start_config_reload (Story #1198 AC2/FIX-3)"
        )

    def test_materialize_registration_before_start_config_reload(self) -> None:
        """materialize_launch_config callback registration must be before _config_svc.start_config_reload."""
        source = _LIFESPAN_PATH.read_text()
        pool_pos = source.find("_config_svc.set_connection_pool(_cluster_pool)")
        assert pool_pos != -1, (
            "_config_svc.set_connection_pool(_cluster_pool) not found"
        )
        reload_pos = source.find("_config_svc.start_config_reload", pool_pos)
        assert reload_pos != -1, (
            "_config_svc.start_config_reload not found after _config_svc.set_connection_pool"
        )
        # register_on_change_callback call must appear before start_config_reload
        register_pos = source.find("register_on_change_callback", pool_pos)
        assert register_pos != -1, (
            "register_on_change_callback not found after _config_svc.set_connection_pool"
        )
        materialize_pos = source.find("materialize_launch_config", register_pos)
        assert materialize_pos != -1, (
            "materialize_launch_config not found after register_on_change_callback"
        )
        assert materialize_pos < reload_pos, (
            "materialize_launch_config registration must appear BEFORE "
            "_config_svc.start_config_reload "
            f"(found at {materialize_pos}, reload at {reload_pos}) (Story #1198 FIX-3)"
        )
