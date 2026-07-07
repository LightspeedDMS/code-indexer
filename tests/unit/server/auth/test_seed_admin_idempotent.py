"""C1 worker-safe --workers: admin seeding must survive a concurrent-start race.

With multiple uvicorn workers (or pods) cold-starting a fresh cluster DB, more
than one can pass the get_user("admin") == None check and race to INSERT. The
loser must treat "admin now exists" as success instead of crashing on the
users_pkey unique-constraint violation (the old behaviour).
"""

from unittest.mock import MagicMock

import pytest

from code_indexer.server.auth.user_manager import UserManager


def _manager(backend):
    mgr = UserManager.__new__(UserManager)  # bypass heavy __init__
    mgr._use_sqlite = True
    mgr._sqlite_backend = backend
    mgr.password_manager = MagicMock()
    mgr.password_manager.hash_password.return_value = "hashed"
    return mgr


def test_creates_admin_when_absent():
    backend = MagicMock()
    backend.get_user.return_value = None
    _manager(backend).seed_initial_admin()
    backend.create_user.assert_called_once()


def test_noop_when_admin_already_exists():
    backend = MagicMock()
    backend.get_user.return_value = {"username": "admin"}
    _manager(backend).seed_initial_admin()
    backend.create_user.assert_not_called()


def test_swallows_lost_race_when_admin_appears_after_failed_insert():
    backend = MagicMock()
    # absent at the pre-check, present after the (losing) INSERT raises
    backend.get_user.side_effect = [None, {"username": "admin"}]
    backend.create_user.side_effect = Exception(
        'duplicate key value violates unique constraint "users_pkey"'
    )
    _manager(backend).seed_initial_admin()  # must NOT raise


def test_reraises_when_insert_fails_and_admin_still_absent():
    backend = MagicMock()
    backend.get_user.side_effect = [None, None]  # genuine failure, not a race
    backend.create_user.side_effect = RuntimeError("db unreachable")
    with pytest.raises(RuntimeError):
        _manager(backend).seed_initial_admin()
