"""The server's account deletion path removes every row keyed to the name.

The app module's ``user_manager`` is built by ``initialize_services()`` (here
inside the isolated per-session server home).  It must carry the account data
purger for the configured storage mode, so the REST and Web deletion doors,
which both call ``delete_user_audited``, run the purge.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi import FastAPI

from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.services.account_data_purge import SqliteAccountDataPurger
from tests.unit.server._account_rows import (
    OTHER_PASSWORD,
    PASSWORD,
    assert_account_has_no_inherited_rows,
    build_stores,
    seed_account_rows,
)


def test_server_user_manager_purges_account_rows_on_delete() -> None:
    import code_indexer.server.app as app_module

    user_manager = app_module.user_manager
    assert user_manager is not None
    purger = user_manager.account_data_purger

    assert isinstance(purger, SqliteAccountDataPurger)
    assert purger._server_data_dir == Path(os.environ["CIDX_SERVER_DATA_DIR"])


def test_server_user_manager_wires_account_activations() -> None:
    """Wiring check: the server's user manager holds an AccountActivations
    bound to the server's own activated-repository manager (the behaviour is
    covered in tests/unit/server/auth/test_account_deletion_activations.py)."""
    import code_indexer.server.app as app_module
    from code_indexer.server.services.account_activations import (
        AccountActivations,
    )

    user_manager = app_module.user_manager
    assert user_manager is not None
    activations = user_manager.account_activations

    assert isinstance(activations, AccountActivations)
    assert activations._arm is app_module.activated_repo_manager


@pytest.mark.asyncio
async def test_startup_schedules_orphan_sweep_off_the_loop(tmp_path: Path) -> None:
    """Start-up schedules a background sweep that removes rows left by
    earlier deletions, using the server user manager's purger."""
    from code_indexer.server.startup.lifespan import _start_account_orphan_sweep

    stores = build_stores(tmp_path)
    stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
    alice = seed_account_rows(stores, "alice")
    assert stores.user_manager.delete_user("alice")  # leaves the rows behind
    app = FastAPI()

    _start_account_orphan_sweep(app, stores.user_manager)
    await app.state.account_orphan_sweep_task

    stores.user_manager.create_user("alice", OTHER_PASSWORD, UserRole.NORMAL_USER)
    assert_account_has_no_inherited_rows(stores, "alice", alice)


@pytest.mark.asyncio
async def test_sso_links_live_in_the_configured_store(
    tmp_path: Path, home_in_tmp: Path
) -> None:
    """The server's SSO manager links identities in the configured store, the
    same store account deletion purges (never a node-local file)."""
    from code_indexer.server.auth.jwt_manager import JWTManager
    from code_indexer.server.startup.lifespan import _build_oidc_manager
    from code_indexer.server.utils.config_manager import OIDCProviderConfig

    stores = build_stores(tmp_path)
    stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
    config = OIDCProviderConfig(
        enabled=True,
        issuer_url="https://idp.example.com",
        client_id="example-client-id",
        client_secret="example-client-secret",
    )
    manager = _build_oidc_manager(
        config,
        stores.user_manager,
        JWTManager(secret_key="example-secret-key-for-tests"),
        stores.registry,
    )
    await manager.initialize()

    await manager.link_oidc_identity(
        username="alice", subject="example-subject", email="alice@example.com"
    )
    assert stores.registry.oauth.get_oidc_identity("example-subject") is not None

    assert stores.user_manager.delete_user_audited("alice", actor="admin")
    assert stores.registry.oauth.get_oidc_identity("example-subject") is None


def test_startup_without_purger_schedules_nothing(tmp_path: Path) -> None:
    from code_indexer.server.auth.user_manager import UserManager
    from code_indexer.server.startup.lifespan import _start_account_orphan_sweep

    app = FastAPI()
    _start_account_orphan_sweep(
        app, UserManager(users_file_path=str(tmp_path / "users.json"))
    )

    assert getattr(app.state, "account_orphan_sweep_task", None) is None
