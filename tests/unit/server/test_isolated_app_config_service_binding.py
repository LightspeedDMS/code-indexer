"""``isolated_app`` binds the app's ConfigService to its own server home.

``create_app()`` (``service_init``) takes the ConfigService from
``get_config_service()``, which returns ANY singleton already installed. A
module-scoped fixture runs before the tree-wide function-scoped
``_reset_config_service_singleton``, so it can enter ``isolated_app`` while a
singleton bound to some OTHER server home is still installed. The app then
boots against that other home's ``config.json``, and everything resolved
through ``get_config_service()`` during the fixture -- e.g. CI tokens saved via
``web.routes._get_token_manager()`` -- lands in the other home's database,
while requests made after the per-test reset read the isolated home's
database and find nothing (the chunk-4 failure of
``test_config_page_ci_token_masking.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.server._isolated_app import isolated_app

# Neutral, format-valid sample token (never a real credential).
_TOKEN = "ghp_" + "Isolated0Example0Sample0Value000" + "Ab12"


def _install_stale_config_service(home: Path):
    """Install a singleton bound to a previously booted server home."""
    from code_indexer.server.services import config_service as cs
    from code_indexer.server.storage.database_manager import DatabaseSchema

    (home / "data").mkdir(parents=True)
    DatabaseSchema(str(home / "data" / "cidx_server.db")).initialize_database()
    stale = cs.ConfigService(server_dir_path=str(home))
    stale.get_config()
    cs.set_config_service(stale)
    return stale


@pytest.mark.timeout(180)
def test_isolated_app_binds_config_service_to_its_own_root(tmp_path: Path) -> None:
    from code_indexer.server.services import config_service as cs

    stale = _install_stale_config_service(tmp_path / "stale-home")
    root = tmp_path / "iso"
    try:
        with isolated_app(root):
            bound = Path(cs.get_config_service().config_manager.server_dir)
            assert bound.resolve() == (root / "server").resolve(), bound
        # The caller's singleton is put back on exit.
        assert cs._config_service is stale
    finally:
        cs.reset_config_service()


@pytest.mark.timeout(180)
def test_ci_token_saved_in_body_survives_config_service_reset(
    tmp_path: Path,
) -> None:
    from code_indexer.server.services import config_service as cs

    _install_stale_config_service(tmp_path / "stale-home")
    try:
        with isolated_app(tmp_path / "iso"):
            from code_indexer.server.web.routes import _get_token_manager

            _get_token_manager().save_token("github", _TOKEN)
            # What the tree-wide autouse fixture does before every test.
            cs.reset_config_service()
            stored = _get_token_manager().get_token("github")
            assert stored is not None, "token saved outside the isolated home"
            assert stored.token == _TOKEN
    finally:
        cs.reset_config_service()
