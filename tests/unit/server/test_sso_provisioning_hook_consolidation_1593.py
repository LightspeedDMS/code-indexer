"""
AC7 RED/GREEN test: SSO provisioning must route through the shared
`ensure_user_group_membership()` primitive.

Story #1593 AC7 requires ONE shared, idempotent helper used by all four
user-creation paths -- REST, MCP, Web UI, and SSO/OIDC
(`services/sso_provisioning_hook.py::SSOProvisioningHook.
ensure_group_membership`). This file does NOT re-test SSO's own
group-mapping/fallback/SystemConfigurationError behavior -- that has
extensive existing coverage in test_sso_provisioning_hook.py, which
this change must leave green (verified separately by re-running that
suite). This file adds ONE narrow, additional assertion: the final
assign+audit step is delegated to
`GroupAccessManager.ensure_user_group_membership()`, not an independent
inline `assign_user_to_group()` + `log_audit()` pair.

TDD: written FIRST, against unmodified production code, where the SSO
hook calls `assign_user_to_group`/`log_audit` directly and never calls
`ensure_user_group_membership` -- the spy assertion below fails.
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.server.services.group_access_manager import GroupAccessManager
from code_indexer.server.services.sso_provisioning_hook import SSOProvisioningHook


@pytest.fixture
def temp_db_path():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


@pytest.fixture
def group_manager_with_spy(temp_db_path):
    manager = GroupAccessManager(temp_db_path)
    manager.ensure_user_group_membership = MagicMock(  # type: ignore[method-assign]
        wraps=manager.ensure_user_group_membership
    )
    return manager


def test_new_sso_user_provisioning_calls_shared_ensure_membership_primitive(
    group_manager_with_spy,
):
    hook = SSOProvisioningHook(group_manager_with_spy)

    result = hook.ensure_group_membership("sso-consolidation-user")

    assert result is True
    group_manager_with_spy.ensure_user_group_membership.assert_called_once()
    call_args = group_manager_with_spy.ensure_user_group_membership.call_args
    assert call_args.args[0] == "sso-consolidation-user"
    assert call_args.args[1].name == "users"
