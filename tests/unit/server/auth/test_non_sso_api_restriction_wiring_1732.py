"""
Bug #1732 (Finding 2, follow-up to #1727): Story #563's non-SSO API
restriction (`_check_non_sso_api_restriction` in
`server/auth/dependencies.py`) was fully inert -- `server_config` (declared
at `dependencies.py:46`, in the SAME "Global instances" block the #1727
fixture resets) was NEVER assigned anywhere in `src/` or `tests/`, so the
function's `if server_config is None: return` early-return always fired
regardless of any admin configuration.

Root cause confirmed via `git show 6a2aec18` (Story #563's original commit):
it added the `server_config` global, the check function, and the
`WebSecurityConfig.restrict_non_sso_to_web_ui` config field, but never
touched `app_wiring.py` (or its pre-split predecessor `app.py`) to actually
assign `dependencies.server_config = server_config` -- unlike the other 5
`dependencies.*` globals, which `create_fastapi_app()` has always set
(app_wiring.py:220-227). This is the identical "registered-but-unwired"
shape as Bug #1667 (`WikiCacheInvalidator`) -- a real object existed and a
real consumer read it, but nothing ever connected the two. This file
follows that established test pattern
(`test_app_wiring_wiki_cache_invalidator_1667.py`): structural source
inspection for the wiring-exists assertion, plus a genuine end-to-end round
trip against REAL `UserManager`/`ServerConfig`/`WebSecurityConfig` objects
(no mocks) to prove the underlying restriction mechanism itself now
actually enforces once wired.

Because this file lives under tests/unit/server/, the tree-wide autouse
`_snapshot_restore_auth_dependencies` fixture (conftest.py) saves and
restores every attribute this file mutates on `dependencies` --
`user_manager` (already tracked) and `server_config` (added to
`_AUTH_DEPENDENCIES_ATTRS` by this same #1732 fix, precisely to avoid
reintroducing the exact leak class #1727 fixed: without that addition,
wiring `dependencies.server_config` in production code would make every
real `create_app()`/`create_fastapi_app()` test leak it into later tests,
unprotected).
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from fastapi import HTTPException

import code_indexer.server.auth.dependencies as _auth_dependencies_module
from code_indexer.server.auth.dependencies import _check_non_sso_api_restriction
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.startup import app_wiring
from code_indexer.server.utils.config_manager import ServerConfig, WebSecurityConfig

# Not a real credential -- local-only test fixture password for a throwaway
# UserManager backed by a JSON file under pytest's tmp_path, discarded at
# test teardown. Mirrors the identical literal already used by the
# established precedent test_sso_password_change_protection.py.
_TEST_FIXTURE_PASSWORD = "SecurePass123!@#"  # nosec: not a secret, test-only


class TestAppWiringWiresServerConfig:
    """Structural verification that create_fastapi_app wires server_config
    onto the auth-dependencies module, mirroring
    TestAppWiringWiresWikiCacheInvalidator in
    test_app_wiring_wiki_cache_invalidator_1667.py.
    """

    def test_app_wiring_calls_sets_dependencies_server_config(self) -> None:
        source = inspect.getsource(app_wiring.create_fastapi_app)
        assert "dependencies.server_config = server_config" in source, (
            "Bug #1732 Finding 2: create_fastapi_app must assign "
            "dependencies.server_config = server_config alongside the other "
            "5 dependencies.* assignments, or Story #563's non-SSO API "
            "restriction remains permanently inert."
        )


def _make_user(tmp_path: Path, username: str, *, sso: bool) -> UserManager:
    """Real (no-mock) UserManager, JSON-backed in tmp_path, with one user
    created and (optionally) linked to a real OIDC identity. Returns the
    manager -- the caller reads the User back via manager.get_user().
    """
    users_file = str(tmp_path / f"{username}-users.json")
    manager = UserManager(users_file_path=users_file)
    manager.create_user(username, _TEST_FIXTURE_PASSWORD, UserRole.NORMAL_USER)
    if sso:
        manager.set_oidc_identity(
            username,
            {
                "subject": "oidc-12345",
                "email": "sso@example.com",
                "linked_at": "2025-01-15T10:30:00Z",
            },
        )
    return manager


class TestNonSsoApiRestrictionRealRoundTrip:
    """Real (no-mock) proof that, once server_config is wired,
    _check_non_sso_api_restriction actually enforces Story #563's
    restriction -- using a real UserManager, real ServerConfig, and real
    WebSecurityConfig, not test doubles standing in for any of them.
    """

    @pytest.mark.parametrize(
        "sso, restrict_enabled, server_config_present, expect_rejected",
        [
            pytest.param(False, True, True, True, id="non_sso-restriction_on-rejected"),
            pytest.param(True, True, True, False, id="sso-restriction_on-not_rejected"),
            pytest.param(
                False, False, True, False, id="non_sso-restriction_off-not_rejected"
            ),
            pytest.param(
                False, True, False, False, id="non_sso-server_config_unset-not_rejected"
            ),
        ],
    )
    def test_restriction_enforcement_matrix(
        self,
        tmp_path: Path,
        sso: bool,
        restrict_enabled: bool,
        server_config_present: bool,
        expect_rejected: bool,
    ) -> None:
        manager = _make_user(tmp_path, "testuser", sso=sso)
        user = manager.get_user("testuser")
        assert user is not None

        _auth_dependencies_module.user_manager = manager
        _auth_dependencies_module.server_config = (
            ServerConfig(
                server_dir=str(tmp_path),
                web_security_config=WebSecurityConfig(
                    restrict_non_sso_to_web_ui=restrict_enabled
                ),
            )
            if server_config_present
            else None
        )

        if expect_rejected:
            with pytest.raises(HTTPException) as exc_info:
                _check_non_sso_api_restriction(user)
            assert exc_info.value.status_code == 403
        else:
            _check_non_sso_api_restriction(user)  # must not raise
