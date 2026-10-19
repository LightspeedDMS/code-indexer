"""A user store double whose analysis-agent account has the principal's shape
(role normal_user, no email, no SSO link), as provisioning leaves it."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.services.analysis_agent_principal import (
    ANALYSIS_AGENT_USERNAME,
)


def principal_account_store() -> MagicMock:
    store = MagicMock()
    store.get_user.return_value = User(
        username=ANALYSIS_AGENT_USERNAME,
        password_hash="example-hash",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    store.is_sso_user.return_value = False
    return store
