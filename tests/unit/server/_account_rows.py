"""Real SQLite stores for account-deletion tests, laid out like a server.

Every store is the real production class pointed at a temporary server data
directory laid out exactly as ``StorageFactory`` and service start-up lay it
out (``data/cidx_server.db``, ``groups.db``, ``oauth.db``,
``refresh_tokens.db``).  ``UserManager`` is wired with the account data purger
the same way ``initialize_services()`` wires it.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Tuple

import pyotp
import pytest

from code_indexer.server.auth import dependencies
from code_indexer.server.auth.jwt_manager import JWTManager
from code_indexer.server.auth.refresh_token_manager import RefreshTokenManager
from code_indexer.server.auth.totp_service import TOTPService
from code_indexer.server.auth.user_manager import UserManager, UserRole
from code_indexer.server.services.account_data_purge import (
    build_account_data_purger,
)
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.factory import BackendRegistry, StorageFactory

PASSWORD = "Example-Passw0rd-For-Tests!"
OTHER_PASSWORD = "Another-Example-Passw0rd-9!"
REDIRECT_URI = "https://app.example.com/cb"


@dataclass
class Stores:
    server_dir: Path
    registry: BackendRegistry
    user_manager: UserManager
    refresh_tokens: RefreshTokenManager
    totp: TOTPService


@dataclass
class SeededRows:
    refresh_token: str
    oauth_access_token: str
    sso_subject: str


def build_stores(tmp_path: Path) -> Stores:
    server_dir = tmp_path / "server"
    data_dir = server_dir / "data"
    data_dir.mkdir(parents=True)
    accounts_db = data_dir / "cidx_server.db"
    DatabaseSchema(str(accounts_db)).initialize_database()
    registry = StorageFactory.create_backends(
        config={"storage_mode": "sqlite"}, data_dir=str(data_dir)
    )
    user_manager = UserManager(
        use_sqlite=True,
        db_path=str(accounts_db),
        storage_backend=registry.users,
        account_data_purger=build_account_data_purger("sqlite", server_dir, None),
    )
    refresh_tokens = RefreshTokenManager(
        jwt_manager=JWTManager(secret_key="example-secret-key-for-tests"),
        storage_backend=registry.refresh_tokens,
    )
    totp = TOTPService(db_path=str(accounts_db))
    return Stores(server_dir, registry, user_manager, refresh_tokens, totp)


def install_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, names: Iterable[str]
) -> Stores:
    """Real stores whose accounts are *names*, installed as the server's
    ``dependencies.user_manager`` for the duration of the test."""
    stores = build_stores(tmp_path)
    for name in names:
        stores.user_manager.create_user(name, PASSWORD, UserRole.NORMAL_USER)
    monkeypatch.setattr(dependencies, "user_manager", stores.user_manager)
    return stores


def issue_refresh_token(stores: Stores, username: str, role: str) -> str:
    family = stores.refresh_tokens.create_token_family(username)
    issued: Any = stores.refresh_tokens.create_initial_refresh_token(
        family_id=family,
        username=username,
        user_data={"username": username, "role": role},
    )
    return str(issued["refresh_token"])


def _pkce_pair() -> Tuple[str, str]:
    verifier = "example-code-verifier-" + "v" * 40
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def issue_oauth_token(stores: Stores, username: str) -> str:
    oauth = stores.registry.oauth
    client = oauth.register_client(
        client_name="example-client", redirect_uris=[REDIRECT_URI]
    )
    verifier, challenge = _pkce_pair()
    code = oauth.generate_authorization_code(
        client_id=client["client_id"],
        user_id=username,
        code_challenge=challenge,
        redirect_uri=REDIRECT_URI,
        state="example-state",
    )
    tokens = oauth.exchange_code_for_token(code, verifier, client["client_id"])
    return str(tokens["access_token"])


def seed_account_rows(stores: Stores, username: str) -> SeededRows:
    """Give *username* a row in every store keyed to an account name."""
    groups = stores.registry.groups
    admins = groups.get_group_by_name("admins")
    assert admins is not None
    groups.assign_user_to_group(username, admins.id, "admin")

    secret = stores.totp.generate_secret(username)
    assert stores.totp.activate_mfa(username, pyotp.TOTP(secret).now())
    stores.totp.generate_recovery_codes(username)

    subject = f"example-subject-{username}"
    stores.registry.oauth.link_oidc_identity(
        username=username, subject=subject, email=f"{username}@example.com"
    )
    stores.registry.git_credentials.upsert_credential(
        credential_id=f"cred-{username}",
        username=username,
        forge_type="github",
        forge_host="git.example.com",
        encrypted_token="example-encrypted-token",
    )
    stores.user_manager.add_api_key(
        username=username,
        key_id=f"key-{username}",
        key_hash="example-key-hash",
        key_prefix="cidx_sk_exam",
        name="example-key",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    return SeededRows(
        refresh_token=issue_refresh_token(stores, username, "admin"),
        oauth_access_token=issue_oauth_token(stores, username),
        sso_subject=subject,
    )


def assert_account_keeps_its_rows(
    stores: Stores, username: str, seeded: SeededRows
) -> None:
    """Every row seeded for the live account *username* is still in place."""
    assert stores.registry.groups.get_user_group(username) is not None
    assert stores.totp.is_mfa_enabled(username) is True
    assert stores.registry.oauth.get_oidc_identity(seeded.sso_subject) is not None
    assert len(stores.registry.git_credentials.list_credentials(username)) == 1
    assert len(stores.user_manager.get_api_keys(username)) == 1
    assert stores.registry.oauth.validate_token(seeded.oauth_access_token)
    refreshed = stores.refresh_tokens.validate_and_rotate_refresh_token(
        refresh_token=seeded.refresh_token, user_manager=stores.user_manager
    )
    assert refreshed["valid"] is True


def assert_account_has_no_inherited_rows(
    stores: Stores, username: str, seeded: SeededRows
) -> None:
    assert stores.registry.groups.get_user_group(username) is None
    assert stores.totp.is_mfa_enabled(username) is False
    assert stores.totp._get_secret(username) is None
    assert stores.registry.oauth.get_oidc_identity(seeded.sso_subject) is None
    assert stores.registry.git_credentials.list_credentials(username) == []
    assert stores.user_manager.get_api_keys(username) == []
    assert stores.registry.oauth.validate_token(seeded.oauth_access_token) is None
    refreshed = stores.refresh_tokens.validate_and_rotate_refresh_token(
        refresh_token=seeded.refresh_token, user_manager=stores.user_manager
    )
    assert refreshed["valid"] is False
