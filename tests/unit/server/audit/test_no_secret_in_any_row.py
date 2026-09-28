"""No secret value reaches any audit row, for any capability class.

Realistic secret-bearing payloads (passwords, raw API keys, MCP client
secrets, SSH private keys, forge PATs, provider API keys, CI tokens, the SSO
client secret, TOTP secrets and codes, recovery codes, elevation session
keys, credentials embedded in a clone URL, and free-text names) are driven
through the REAL audited entry points of every capability class -- and
through the real REST / Web doors for the configuration secrets -- into ONE
real SQLite audit store.  Every column of every row is then read back with
direct SQL and scanned for every sentinel, both as stored and inside the
decoded ``details`` JSON.

Doubles: the forge API client (network boundary) and the harness doubles of
``_audit_front_doors`` (background job runner, Web CSRF, elevation switch).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Sequence, Tuple

import pyotp
import pytest

from _audit_accounts_support import CAPTURE_LOGGER, capture_errors, make_user_manager
from _audit_front_doors import ACTING_ADMIN, DoorsEnv, front_door_env
from code_indexer.server.auth.user_manager import UserRole

_USER = "example-user"
_PASSWORD = "SecureP@ssw0rd!XyZ789"
# Columns the store generates itself; a short numeric code may collide with
# their digits by chance, so only those columns are skipped for such codes.
_GENERATED_COLUMNS = frozenset({"id", "timestamp", "event_uuid", "correlation_id"})
# Action types the drivers produce only as refusals; every other expected
# type must also have a success row, so no secret-bearing path stopped early.
_FAILURE_ONLY = frozenset({"authentication_failure", "elevation_failed"})


@dataclass
class Secrets:
    """Sentinel secrets per capability class, and the rows each must yield."""

    values: Dict[str, List[str]] = field(default_factory=dict)
    expected: Dict[str, Tuple[str, ...]] = field(default_factory=dict)

    def add(self, capability: str, *values: str) -> None:
        for value in values:
            assert value, capability  # an empty sentinel matches everything
            self.values.setdefault(capability, []).append(value)


# ---------------------------------------------------------------------------
# Reading and scanning the store
# ---------------------------------------------------------------------------


def all_rows(db_path: Path) -> List[Dict[str, Any]]:
    """Every row of ``audit_logs``, every column, via direct SQL."""
    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.execute("SELECT * FROM audit_logs ORDER BY id")
        columns = [c[0] for c in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def _decoded_strings(raw: Any) -> List[str]:
    """Every string (keys and values) inside a JSON ``details`` document."""
    if not isinstance(raw, str) or not raw:
        return []
    try:
        document = json.loads(raw)
    except ValueError:
        return []
    found: List[str] = []
    stack: List[Any] = [document]
    while stack:  # bounded by the document's size
        item = stack.pop()
        if isinstance(item, dict):
            found.extend(str(k) for k in item)
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
        elif item is not None:
            found.append(str(item))
    return found


def leaks(
    rows: Sequence[Mapping[str, Any]], secrets: Mapping[str, Sequence[str]]
) -> List[str]:
    """``capability: action_type.column`` for every sentinel found in a row."""
    found: List[str] = []
    for row in rows:
        cells = {name: str(value) for name, value in row.items() if value is not None}
        decoded = " ".join(_decoded_strings(row.get("details")))
        for capability, values in secrets.items():
            for value in values:
                short_numeric = value.isdigit() and len(value) <= 8
                for name, text in cells.items():
                    if short_numeric and name in _GENERATED_COLUMNS:
                        continue
                    if value in text:
                        found.append(f"{capability}: {row['action_type']}.{name}")
                if value in decoded:
                    found.append(f"{capability}: {row['action_type']}.details(decoded)")
    return sorted(set(found))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path: Path, monkeypatch) -> Iterator[DoorsEnv]:
    from code_indexer.server.auth.oidc import routes as oidc_routes

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(oidc_routes, "oidc_manager", oidc_routes.oidc_manager)
    monkeypatch.setattr(oidc_routes, "state_manager", oidc_routes.state_manager)
    yield from front_door_env(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def _no_capture_errors(caplog) -> Iterator[None]:
    """A rejected or dropped event would make the scan vacuous: none allowed."""
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    yield
    assert capture_errors(caplog, phases=["setup", "call"]) == []


# ---------------------------------------------------------------------------
# Drivers: one per capability class
# ---------------------------------------------------------------------------


def _accounts(tmp_path: Path):
    directory = tmp_path / "accounts"
    directory.mkdir(exist_ok=True)
    users = make_user_manager(directory)
    if users.get_user(ACTING_ADMIN) is None:
        users.create_user(ACTING_ADMIN, _PASSWORD, UserRole.ADMIN)
    return users


def drive_users_and_passwords(env: DoorsEnv, tmp_path: Path, s: Secrets) -> None:
    from code_indexer.server.auth.login_outcome import complete_login, reject_login

    capability = "users/passwords"
    users = _accounts(tmp_path)
    initial, reset, weak, weak_reset = (
        "Sentinel-Initial-Pass-7f3a!Qx9",
        "Sentinel-Reset-Pass-7f3a!Zk4",
        "sntl7f3aweak",
        "sntl7f3awk2",
    )
    email = "sentinel-7f3a-person@example.com"
    typed_as_username = "Sentinel-Typed-Pass-7f3a!Mv2"
    issued_token = "eyJSentinelIssuedToken7f3a.payload.signature"
    s.add(capability, initial, reset, weak, weak_reset, email, typed_as_username)
    s.add(capability, issued_token)

    users.create_user_audited(_USER, initial, UserRole.NORMAL_USER, actor=ACTING_ADMIN)
    with pytest.raises(ValueError):
        users.create_user_audited(
            "example-user-2", weak, UserRole.ADMIN, actor=ACTING_ADMIN
        )
    users.change_password_audited(_USER, reset, actor=ACTING_ADMIN)
    with pytest.raises(ValueError):
        users.change_password_audited(_USER, weak_reset, actor=ACTING_ADMIN)
    users.update_user_role_audited(_USER, UserRole.POWER_USER, actor=ACTING_ADMIN)
    users.update_user_email_audited(_USER, email, actor=ACTING_ADMIN)
    assert users.delete_user_audited(_USER, actor=ACTING_ADMIN)
    # A password typed into the username field of a refused login.
    reject_login(
        typed_as_username,
        account_exists=False,
        method="password",
        stage="credentials",
        reason="bad_credentials",
    )
    assert (
        complete_login(
            ACTING_ADMIN,
            method="password",
            mfa="not_enrolled",
            flow="rest_token",
            issue=lambda: issued_token,
        )
        == issued_token
    )
    s.expected[capability] = (
        "user_created",
        "user_password_reset_by_admin",
        "user_role_changed",
        "user_email_changed",
        "user_deleted",
        "authentication_failure",
        "authentication_success",
    )


def drive_api_keys(env: DoorsEnv, tmp_path: Path, s: Secrets) -> None:
    from code_indexer.server.auth.api_key_manager import ApiKeyManager

    capability = "api keys"
    users = _accounts(tmp_path)
    name = "Sentinel-Key-Name-7f3a"
    raw_key, key_id = ApiKeyManager(users).generate_key_audited(
        ACTING_ADMIN, name=name, actor=ACTING_ADMIN
    )
    raw_key_shaped_id = "cidx_sk_" + "7f3a0123456789ab" * 2
    s.add(capability, raw_key, name, raw_key_shaped_id)
    assert users.delete_api_key_audited(ACTING_ADMIN, key_id, actor=ACTING_ADMIN)
    assert not users.delete_api_key_audited(
        ACTING_ADMIN, raw_key_shaped_id, actor=ACTING_ADMIN
    )
    s.expected[capability] = ("api_key_created", "api_key_deleted")


def drive_mcp_credentials(env: DoorsEnv, tmp_path: Path, s: Secrets) -> None:
    from code_indexer.server.auth.mcp_credential_manager import MCPCredentialManager

    capability = "mcp credentials"
    manager = MCPCredentialManager(_accounts(tmp_path))
    name = "Sentinel-Cred-Name-7f3a"
    credential = manager.generate_credential_audited(
        ACTING_ADMIN, name, actor=ACTING_ADMIN
    )
    secret_shaped_id = "mcp_sec_" + "7f3a" * 16
    s.add(capability, credential["client_secret"], name, secret_shaped_id)
    assert manager.revoke_credential_audited(
        ACTING_ADMIN, credential["credential_id"], actor=ACTING_ADMIN
    )
    assert not manager.revoke_credential_audited(
        ACTING_ADMIN, secret_shaped_id, actor=ACTING_ADMIN
    )
    s.expected[capability] = ("mcp_credential_created", "mcp_credential_revoked")


def drive_ssh_keys(env: DoorsEnv, tmp_path: Path, s: Secrets) -> None:
    from code_indexer.server.services.ssh_key_manager import SSHKeyManager

    capability = "ssh keys"
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir(mode=0o700)
    manager = SSHKeyManager(
        ssh_dir=ssh_dir,
        metadata_dir=tmp_path / "ssh-meta",
        config_path=ssh_dir / "config",
    )
    name, email, description = (
        "sentinel_key_name_7f3a",
        "sentinel-7f3a-key@example.com",
        "Sentinel key description 7f3a",
    )
    metadata = manager.create_key_audited(
        name,
        key_type="ed25519",
        email=email,
        description=description,
        actor=ACTING_ADMIN,
    )
    private_body = [
        line
        for line in Path(metadata.private_path).read_text().splitlines()
        if line and not line.startswith("-----")
    ]
    s.add(capability, name, email, description, *private_body)
    manager.assign_key_to_host_audited(name, "git.example.com", actor=ACTING_ADMIN)
    assert manager.delete_key_audited(name, actor=ACTING_ADMIN)
    s.expected[capability] = (
        "ssh_key_created",
        "ssh_key_host_assigned",
        "ssh_key_deleted",
    )


class _ForgeClient:
    """Stands in for the external forge API (network boundary)."""

    def __init__(self, accept: bool) -> None:
        self.accept = accept

    async def validate_and_discover(self, token: str, host: str) -> Dict[str, str]:
        if not self.accept:
            raise PermissionError("forge rejected the token")
        return {"git_user_name": "Example", "forge_username": "example"}


def drive_git_credentials(env: DoorsEnv, tmp_path: Path, s: Secrets) -> None:
    from code_indexer.server.services import git_credential_manager as gcm
    from code_indexer.server.storage.database_manager import DatabaseSchema

    capability = "git credentials / PATs"
    db_path = str(tmp_path / "git-creds.db")
    DatabaseSchema(db_path=db_path).initialize_database()
    manager = gcm.GitCredentialManager(db_path=db_path)
    token, rejected, name = (
        "ghp_Sentinel7f3aPersonalAccessToken0000",
        "glpat-Sentinel7f3aRejectedToken00",
        "Sentinel PAT name 7f3a",
    )
    s.add(capability, token, rejected, name)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(gcm, "get_forge_client", lambda _p: _ForgeClient(True))
        result = asyncio.run(
            manager.configure_credential_audited(
                _USER, "github", "github.com", token, name=name, actor=_USER
            )
        )
        patch.setattr(gcm, "get_forge_client", lambda _p: _ForgeClient(False))
        with pytest.raises(PermissionError):
            asyncio.run(
                manager.configure_credential_audited(
                    _USER, "gitlab", "gitlab.example.com", rejected, actor=_USER
                )
            )
    manager.delete_credential_audited(_USER, result["credential_id"], actor=_USER)
    s.expected[capability] = ("git_credential_configured", "git_credential_deleted")


def drive_provider_api_keys(env: DoorsEnv, tmp_path: Path, s: Secrets) -> None:
    capability = "provider api keys"
    voyage, cohere = (
        "pa-Sentinel7f3aVoyageProviderKey-0123456789",
        "co-Sentinel7f3aCohereProviderKey-0123456789",
    )
    s.add(capability, voyage, cohere)
    for provider, key in (("voyageai", voyage), ("cohere", cohere)):
        resp = env.rest("POST", f"/api/api-keys/{provider}", json={"api_key": key})
        assert resp.status_code == 200, resp.text
        resp = env.rest("DELETE", f"/api/api-keys/{provider}")
        assert resp.status_code == 200, resp.text
    s.expected[capability] = ("provider_api_key_set", "provider_api_key_cleared")


def drive_ci_tokens(env: DoorsEnv, tmp_path: Path, s: Secrets) -> None:
    capability = "ci tokens"
    token = "ghp_Sentinel7f3aCiToken" + "A" * 17
    s.add(capability, token)
    env.web(
        "POST", "/admin/config/api-keys/github", data={"token": token, "api_url": ""}
    )
    env.web("DELETE", "/admin/config/api-keys/github", headers={"X-CSRF-Token": "x"})
    s.expected[capability] = ("ci_token_set", "ci_token_deleted")


def drive_config_secrets(env: DoorsEnv, tmp_path: Path, s: Secrets) -> None:
    capability = "config secrets (SSO client secret, LLM provider key)"
    client_secret, llm_key = (
        "Sentinel-7f3a-SSO-Client-Secret",
        "Sentinel-7f3a-LLM-Provider-Key",
    )
    s.add(capability, client_secret, llm_key)
    env.web(
        "POST",
        "/admin/config/oidc",
        data={"use_pkce": "false", "client_secret": client_secret},
    )
    resp = env.rest(
        "POST",
        "/api/llm-creds/save-config",
        json={
            "claude_auth_mode": "api_key",
            "llm_creds_provider_url": "",
            "llm_creds_provider_api_key": llm_key,
            "llm_creds_provider_consumer_id": "",
        },
    )
    assert resp.status_code == 200, resp.text
    live = env.config_service.get_config()
    assert live.oidc_provider_config.client_secret == client_secret
    s.expected[capability] = ("config_changed",)


def drive_repo_url_credentials(env: DoorsEnv, tmp_path: Path, s: Secrets) -> None:
    capability = "clone URL credentials"
    pat = "Sentinel7f3aUrlEmbeddedPat"
    s.add(capability, pat, "example-url-user:")
    env.manager.add_golden_repo(
        f"https://example-url-user:{pat}@git.example.com/org/example.git",
        "example-credential-repo",
        submitter_username=ACTING_ADMIN,
        skip_pre_flight_git_validation=True,
    )
    s.expected[capability] = ("golden_repo_added",)


def _next_step_code(secret: str) -> str:
    """A valid code from the NEXT time step (the current one is spent)."""
    return str(pyotp.TOTP(secret).at(int(time.time()) + 30))


def drive_mfa_and_elevation(env: DoorsEnv, tmp_path: Path, s: Secrets) -> None:
    from code_indexer.server.auth.elevation_step_up import step_up
    from code_indexer.server.auth.login_rate_limiter import LoginRateLimiter

    capability = "totp codes / recovery codes / elevation"
    totp = env.stack.totp
    secret = totp.generate_secret(ACTING_ADMIN)
    activation_code = pyotp.TOTP(secret).now()
    codes = totp.activate_mfa_and_issue_recovery_codes(
        ACTING_ADMIN, activation_code, actor=ACTING_ADMIN
    )
    assert codes
    regenerated = totp.regenerate_recovery_codes(ACTING_ADMIN, actor=ACTING_ADMIN)
    session_key = "Sentinel7f3aWebSessionCookieValue"
    bad_recovery = "SNTL-7F3A-BAD0-C0DE"
    step_code = _next_step_code(secret)
    s.add(capability, secret, activation_code, step_code, bad_recovery, session_key)
    s.add(capability, *codes, *regenerated)
    limiter = LoginRateLimiter()
    common: Dict[str, Any] = dict(
        session_key=session_key,
        client_ip="127.0.0.1",
        totp_service=totp,
        sessions=env.stack.esm,
        limiter=limiter,
    )
    step_up(ACTING_ADMIN, totp_code=None, recovery_code=bad_recovery, **common)
    step_up(ACTING_ADMIN, totp_code=step_code, recovery_code=None, **common)
    step_up(ACTING_ADMIN, totp_code=None, recovery_code=regenerated[0], **common)
    env.stack.create_user(_USER)
    other_secret = totp.regenerate_secret_cross_user(_USER, actor=ACTING_ADMIN)
    s.add(capability, other_secret)
    totp.disable_mfa(ACTING_ADMIN, actor=ACTING_ADMIN, method="totp")
    s.expected[capability] = (
        "mfa_activated",
        "mfa_recovery_codes_regenerated",
        "elevation_failed",
        "elevation_granted",
        "mfa_secret_regenerated_cross_user",
        "mfa_disabled",
    )


_DRIVERS: Dict[str, Callable[[DoorsEnv, Path, Secrets], None]] = {
    "users/passwords": drive_users_and_passwords,
    "api keys": drive_api_keys,
    "mcp credentials": drive_mcp_credentials,
    "ssh keys": drive_ssh_keys,
    "git credentials / PATs": drive_git_credentials,
    "provider api keys": drive_provider_api_keys,
    "ci tokens": drive_ci_tokens,
    "config secrets": drive_config_secrets,
    "clone URL credentials": drive_repo_url_credentials,
    "totp / recovery codes / elevation": drive_mfa_and_elevation,
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_secret_of_any_class_reaches_any_row(env: DoorsEnv, tmp_path) -> None:
    secrets = Secrets()
    for driver in _DRIVERS.values():
        driver(env, tmp_path, secrets)
    rows = all_rows(env.store.db_path)
    recorded = {row["action_type"] for row in rows}
    missing = {
        capability: sorted(set(expected) - recorded)
        for capability, expected in secrets.expected.items()
        if set(expected) - recorded
    }
    assert missing == {}, "a capability class produced no row to scan"
    succeeded = {row["action_type"] for row in rows if row["outcome"] == "success"}
    unfinished = sorted(
        {action for expected in secrets.expected.values() for action in expected}
        - _FAILURE_ONLY
        - succeeded
    )
    assert unfinished == [], "a secret-bearing success path did not complete"
    assert set(secrets.values) == set(secrets.expected)
    assert leaks(rows, secrets.values) == []


def test_the_scan_finds_a_secret_in_any_column_or_encoding() -> None:
    """Negative controls: a planted sentinel is found wherever it hides."""
    sentinel = "Sentinel-7f3a-Planted"
    base = {"id": 1, "action_type": "config_changed", "details": None}
    assert leaks([{**base, "target_id": sentinel}], {"c": [sentinel]}) == [
        "c: config_changed.target_id"
    ]
    nested = json.dumps({"values": {"k": [sentinel, None]}})
    assert leaks([{**base, "details": nested}], {"c": [sentinel]}) == [
        "c: config_changed.details",
        "c: config_changed.details(decoded)",
    ]
    escaped = json.dumps({"k": "é" + sentinel})  # stored as \\u00e9...
    assert "c: config_changed.details(decoded)" in leaks(
        [{**base, "details": escaped}], {"c": [sentinel]}
    )
    assert leaks([{**base, "target_id": "clean"}], {"c": [sentinel]}) == []


def test_short_numeric_codes_skip_only_generated_columns() -> None:
    row = {
        "id": 123456,
        "timestamp": "2026-01-01T00:00:00.123456+00:00",
        "action_type": "elevation_failed",
        "details": json.dumps({"code": "123456"}),
    }
    assert leaks([row], {"c": ["123456"]}) == [
        "c: elevation_failed.details",
        "c: elevation_failed.details(decoded)",
    ]
