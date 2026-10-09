"""Deleting an account removes every stored row keyed to its name (SQLite).

Runs against the real production stores in a temporary server data directory
(see ``tests/unit/server/_account_rows.py``).  A later account created with
the same name must start with nothing from the earlier one.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import List

import pytest

from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.services.account_data_purge import (
    ACCOUNTS_DB,
    DELETED_ACCOUNT_WHERE,
    SQLITE_STORES,
    PostgresAccountDataPurger,
    SqliteAccountDataPurger,
    build_account_data_purger,
    run_startup_orphan_sweep,
    sweep_orphaned_account_data,
)
from tests.unit.server._account_rows import (
    OTHER_PASSWORD,
    PASSWORD,
    Stores,
    assert_account_has_no_inherited_rows,
    assert_account_keeps_its_rows,
    build_stores,
    issue_refresh_token,
    seed_account_rows,
)

PURGE_LOGGER = "code_indexer.server.services.account_data_purge"
USER_MANAGER_LOGGER = "code_indexer.server.auth.user_manager"


@pytest.fixture
def stores(tmp_path: Path) -> Stores:
    return build_stores(tmp_path)


class TestRefreshTokenRequiresLiveAccount:
    def test_refresh_token_of_deleted_account_mints_nothing(
        self, stores: Stores
    ) -> None:
        """A refresh token whose account no longer exists mints no access."""
        stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
        token = issue_refresh_token(stores, "alice", "admin")
        assert stores.user_manager.delete_user("alice") is True

        result = stores.refresh_tokens.validate_and_rotate_refresh_token(
            refresh_token=token, user_manager=stores.user_manager
        )

        assert result["valid"] is False
        assert "new_access_token" not in result

    def test_family_of_earlier_account_never_mints_for_recreated_account(
        self, stores: Stores
    ) -> None:
        """A refresh family that outlived its account (a race with the
        cleanup) never mints access for a later account with the name."""
        stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
        token = issue_refresh_token(stores, "alice", "admin")
        assert stores.user_manager.delete_user("alice")
        # Re-created straight in the accounts store: the family survives.
        stores.registry.users.create_user(
            username="alice", password_hash="example-hash", role="normal_user"
        )

        result = stores.refresh_tokens.validate_and_rotate_refresh_token(
            refresh_token=token, user_manager=stores.user_manager
        )

        assert result["valid"] is False
        assert "new_access_token" not in result

    def test_rotation_keeps_the_original_authentication_time(
        self, stores: Stores
    ) -> None:
        stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
        family = stores.refresh_tokens.create_token_family("alice")
        issued = stores.refresh_tokens.create_initial_refresh_token(
            family_id=family,
            username="alice",
            user_data={"username": "alice", "role": "admin"},
        )
        record = stores.registry.refresh_tokens.get_token_family(family)
        assert record is not None
        signed_in = datetime.fromisoformat(record["created_at"]).timestamp()

        result = stores.refresh_tokens.validate_and_rotate_refresh_token(
            refresh_token=issued["refresh_token"], user_manager=stores.user_manager
        )

        claims = stores.refresh_tokens.jwt_manager.validate_token(
            result["new_access_token"]
        )
        assert claims["auth_time"] == pytest.approx(signed_in, abs=1e-3)
        assert claims["auth_time"] < claims["iat"]


class TestRecreatedAccountStartsClean:
    def test_recreated_account_inherits_nothing(self, stores: Stores) -> None:
        """Deleting an account removes all rows keyed to its name, so an
        account later created with that name inherits none of them."""
        stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
        seeded = seed_account_rows(stores, "alice")

        assert stores.user_manager.delete_user_audited("alice", actor="admin")
        stores.user_manager.create_user("alice", OTHER_PASSWORD, UserRole.NORMAL_USER)

        assert_account_has_no_inherited_rows(stores, "alice", seeded)

    def test_purge_failing_part_way_never_leaves_a_live_weakened_account(
        self,
        stores: Stores,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The account row is deleted before its keyed rows: a purge that
        fails after removing the MFA rows leaves no live account behind (the
        leftovers are removed by the next purge or the start-up sweep)."""

        class _FailingAfterAccountsStore(SqliteAccountDataPurger):
            def purge_deleted(self, username: str) -> int:
                for relative, tables in SQLITE_STORES:
                    if relative == ACCOUNTS_DB:
                        self._delete_in_store(
                            relative, tables, DELETED_ACCOUNT_WHERE, (username,)
                        )
                raise OSError("example store failure")

        stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
        seed_account_rows(stores, "alice")
        monkeypatch.setattr(
            stores.user_manager,
            "_account_data_purger",
            _FailingAfterAccountsStore(stores.server_dir),
        )

        with caplog.at_level(logging.ERROR, logger=USER_MANAGER_LOGGER):
            deleted = stores.user_manager.delete_user_audited("alice", actor="admin")

        assert deleted is True
        assert stores.user_manager.get_user("alice") is None
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    def test_late_purge_never_removes_a_recreated_accounts_rows(
        self, stores: Stores
    ) -> None:
        """The cleanup that follows a deletion removes rows only while no
        account with the name exists: an account re-created in between keeps
        everything it was given."""
        stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
        seed_account_rows(stores, "alice")
        assert stores.user_manager.delete_user("alice")  # cleanup still pending
        stores.user_manager.create_user("alice", OTHER_PASSWORD, UserRole.ADMIN)
        new_alice = seed_account_rows(stores, "alice")

        stores.user_manager._clean_up_after_delete("alice", "admin")

        assert_account_keeps_its_rows(stores, "alice", new_alice)

    def test_account_row_delete_failure_leaves_every_keyed_row(
        self, stores: Stores, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the account row cannot be deleted, nothing keyed to it was
        removed: the live account keeps its MFA and its group."""
        stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
        seed_account_rows(stores, "alice")

        def _refuse(username: str) -> bool:
            raise OSError("example accounts store failure")

        monkeypatch.setattr(stores.registry.users, "delete_user", _refuse)

        with pytest.raises(OSError):
            stores.user_manager.delete_user_audited("alice", actor="admin")
        assert stores.user_manager.get_user("alice") is not None
        assert stores.totp.is_mfa_enabled("alice") is True
        assert stores.registry.groups.get_user_group("alice") is not None

    def test_deleting_one_account_keeps_other_accounts_rows(
        self, stores: Stores
    ) -> None:
        """Only rows keyed to the deleted name are removed."""
        stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
        stores.user_manager.create_user("bob", PASSWORD, UserRole.ADMIN)
        seed_account_rows(stores, "alice")
        bob = seed_account_rows(stores, "bob")

        assert stores.user_manager.delete_user_audited("alice", actor="admin")

        assert stores.registry.groups.get_user_group("bob") is not None
        assert stores.totp.is_mfa_enabled("bob") is True
        assert stores.registry.oauth.get_oidc_identity(bob.sso_subject) is not None
        assert len(stores.registry.git_credentials.list_credentials("bob")) == 1
        assert len(stores.user_manager.get_api_keys("bob")) == 1
        assert stores.registry.oauth.validate_token(bob.oauth_access_token)


class TestCreationStartsClean:
    def test_create_user_removes_leftovers_of_an_earlier_account(
        self, stores: Stores
    ) -> None:
        """Rows left keyed to a name are removed before the name is created."""
        stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
        alice = seed_account_rows(stores, "alice")
        assert stores.user_manager.delete_user("alice")  # leaves rows behind

        stores.user_manager.create_user("alice", OTHER_PASSWORD, UserRole.NORMAL_USER)

        assert_account_has_no_inherited_rows(stores, "alice", alice)

    def test_sso_provisioned_account_removes_leftovers(self, stores: Stores) -> None:
        stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
        alice = seed_account_rows(stores, "alice")
        assert stores.user_manager.delete_user("alice")

        stores.user_manager.create_oidc_user(
            username="alice",
            role=UserRole.NORMAL_USER,
            email="alice@example.com",
            oidc_identity=None,
        )

        assert_account_has_no_inherited_rows(stores, "alice", alice)

    def test_seeded_admin_removes_leftovers(self, stores: Stores) -> None:
        """The bootstrap admin re-created at start-up inherits nothing."""
        stores.user_manager.create_user("admin", PASSWORD, UserRole.ADMIN)
        admin = seed_account_rows(stores, "admin")
        assert stores.user_manager.delete_user("admin")

        stores.user_manager.seed_initial_admin()

        assert stores.user_manager.get_user("admin") is not None
        assert_account_has_no_inherited_rows(stores, "admin", admin)

    def test_creation_refused_when_leftovers_cannot_be_removed(
        self, stores: Stores, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _FailingPurger(SqliteAccountDataPurger):
            def purge(self, username: str) -> int:
                raise OSError("example store failure")

        monkeypatch.setattr(
            stores.user_manager,
            "_account_data_purger",
            _FailingPurger(stores.server_dir),
        )

        with pytest.raises(OSError):
            stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
        assert stores.user_manager.get_user("alice") is None


class TestPurgerInputs:
    @pytest.mark.parametrize("name", ["", "   "])
    def test_purge_refuses_blank_account_name(self, stores: Stores, name: str) -> None:
        stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
        seed_account_rows(stores, "alice")
        purger = build_account_data_purger("sqlite", stores.server_dir, None)

        with pytest.raises(ValueError):
            purger.purge(name)
        assert stores.registry.groups.get_user_group("alice") is not None

    def test_postgres_purger_requires_a_pool(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            build_account_data_purger("postgres", tmp_path, None)
        with pytest.raises(ValueError):
            PostgresAccountDataPurger(None)  # type: ignore[arg-type]

    def test_unknown_storage_mode_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            build_account_data_purger("example-mode", tmp_path, None)

    def test_sqlite_purger_requires_a_data_dir(self) -> None:
        with pytest.raises(ValueError):
            SqliteAccountDataPurger(None)  # type: ignore[arg-type]

    def test_deleted_mode_without_accounts_store_fails_and_removes_nothing(
        self, tmp_path: Path
    ) -> None:
        """The live-account check never passes against a missing accounts
        store: the post-deletion purge fails loudly and deletes nothing."""
        from code_indexer.server.services.group_access_manager import (
            GroupAccessManager,
        )

        groups = GroupAccessManager(tmp_path / "groups.db")
        admins = groups.get_group_by_name("admins")
        assert admins is not None
        groups.assign_user_to_group("alice", admins.id, "admin")

        with pytest.raises(sqlite3.OperationalError):
            SqliteAccountDataPurger(tmp_path).purge_deleted("alice")
        assert groups.get_user_group("alice") is not None
        assert not (tmp_path / ACCOUNTS_DB).exists()

    def test_purge_needs_only_the_stores_holding_rows(self, tmp_path: Path) -> None:
        """A per-account purge works with just groups.db present (no accounts
        file, no token stores) and creates no store file."""
        from code_indexer.server.services.group_access_manager import (
            GroupAccessManager,
        )

        groups = GroupAccessManager(tmp_path / "groups.db")
        admins = groups.get_group_by_name("admins")
        assert admins is not None
        groups.assign_user_to_group("alice", admins.id, "admin")

        removed = SqliteAccountDataPurger(tmp_path).purge("alice")

        assert removed == 1
        assert groups.get_user_group("alice") is None
        assert not (tmp_path / "oauth.db").exists()
        assert not (tmp_path / "data").exists()


class TestStartupOrphanSweep:
    def test_sweep_removes_rows_whose_account_is_gone(
        self, stores: Stores, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Rows keyed to a name with no account are removed at start-up;
        rows of existing accounts are kept."""
        stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
        stores.user_manager.create_user("bob", PASSWORD, UserRole.ADMIN)
        alice = seed_account_rows(stores, "alice")
        bob = seed_account_rows(stores, "bob")
        # The unaudited primitive removes only the account row: rows left
        # without an owning account.
        assert stores.user_manager.delete_user("alice")

        purger = build_account_data_purger("sqlite", stores.server_dir, None)
        with caplog.at_level(logging.INFO, logger=PURGE_LOGGER):
            sweep_orphaned_account_data(purger)

        stores.user_manager.create_user("alice", OTHER_PASSWORD, UserRole.NORMAL_USER)
        assert_account_has_no_inherited_rows(stores, "alice", alice)
        assert stores.registry.groups.get_user_group("bob") is not None
        assert stores.totp.is_mfa_enabled("bob") is True
        assert stores.registry.oauth.get_oidc_identity(bob.sso_subject) is not None
        assert len(stores.registry.git_credentials.list_credentials("bob")) == 1
        swept = [r for r in caplog.records if r.name == PURGE_LOGGER]
        assert any(
            r.levelno == logging.INFO and "removed" in r.getMessage() for r in swept
        )

    def test_sweep_is_idempotent(self, stores: Stores) -> None:
        """A second sweep finds nothing left to remove."""
        stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
        seed_account_rows(stores, "alice")
        assert stores.user_manager.delete_user("alice")
        purger = build_account_data_purger("sqlite", stores.server_dir, None)

        assert purger.purge_orphans() > 0
        assert purger.purge_orphans() == 0

    @pytest.mark.asyncio
    async def test_startup_sweep_runs_off_the_event_loop(self, stores: Stores) -> None:
        """The start-up sweep removes leftovers on a worker thread, never on
        the event-loop thread."""
        stores.user_manager.create_user("alice", PASSWORD, UserRole.ADMIN)
        alice = seed_account_rows(stores, "alice")
        assert stores.user_manager.delete_user("alice")
        real = build_account_data_purger("sqlite", stores.server_dir, None)
        sweep_threads: List[int] = []

        class _RecordingPurger:
            def purge(self, username: str) -> int:
                return real.purge(username)

            def purge_deleted(self, username: str) -> int:
                return real.purge_deleted(username)

            def purge_orphans(self) -> int:
                sweep_threads.append(threading.get_ident())
                return real.purge_orphans()

        await run_startup_orphan_sweep(_RecordingPurger())

        assert sweep_threads and sweep_threads[0] != threading.get_ident()
        stores.user_manager.create_user("alice", OTHER_PASSWORD, UserRole.NORMAL_USER)
        assert_account_has_no_inherited_rows(stores, "alice", alice)

    def test_sweep_failure_is_logged_and_never_raises(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A sweep that cannot read the accounts store logs ERROR, no raise."""
        purger = build_account_data_purger("sqlite", tmp_path / "missing", None)
        with caplog.at_level(logging.INFO, logger=PURGE_LOGGER):
            sweep_orphaned_account_data(purger)

        swept = [r for r in caplog.records if r.name == PURGE_LOGGER]
        assert [r.levelno for r in swept] == [logging.ERROR]
