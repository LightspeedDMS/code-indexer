"""Reset to Defaults resets runtime settings only (public Bug #2094).

The reset must keep every bootstrap key (config.json: storage mode, cluster
identity, clone backend, ...), the launch settings (host/port/workers/
log_level) and every stored credential; every other runtime setting returns
to its default.  The reset goes through the normal compare-and-set change
path (Bug #2017), so a FRESH ConfigService (a restarted process) re-reading
the committed store and config.json must see the same result.

Real SQLite and real PostgreSQL (when TEST_POSTGRES_DSN is set); no mocks.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any, Iterator, List, Set, Tuple

import pytest

from code_indexer.server.services import audit_capture
from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.services.siem_delivery import capture
from code_indexer.server.utils.config_manager import (
    LangfusePullProject,
    ServerConfig,
)
from tests.unit.server.siem.backends import (  # noqa: F401  (fixtures)
    SiemBackendHarness,
    _pg_session_pool,
    pg_pool,
    siem_backend,
)

_BOOTSTRAP = {
    "storage_mode": "postgres",
    "postgres_dsn": "postgresql://example.invalid/cidx_example",
    "cluster": {"node_id": "example-node-1"},
    "clone_backend": "cow-daemon",
    "cow_daemon": {
        "daemon_url": "http://192.0.2.10:8081",
        "api_key": "example-daemon-key",
        "mount_point": "/mnt/example-cow",
    },
    "server_threadpool_size": 64,
    "enable_malloc_trim": False,
}

_LAUNCH = {"host": "0.0.0.0", "port": 8123, "workers": 3, "log_level": "DEBUG"}


def _set_non_defaults(cfg: ServerConfig) -> None:
    for key, value in _LAUNCH.items():
        setattr(cfg, key, value)
    claude = cfg.claude_integration_config
    assert claude is not None
    claude.anthropic_api_key = "example-anthropic-key"
    claude.voyageai_api_key = "example-voyage-key"
    claude.cohere_api_key = "example-cohere-key"
    claude.llm_creds_provider_api_key = "example-lcp-key"
    claude.max_concurrent_claude_cli = 7
    assert cfg.codex_integration_config is not None
    cfg.codex_integration_config.api_key = "example-openai-key"
    assert cfg.oidc_provider_config is not None
    cfg.oidc_provider_config.client_secret = "example-oidc-secret"
    lf = cfg.langfuse_config
    assert lf is not None
    lf.enabled = True
    lf.public_key = "pk-example"
    lf.secret_key = "sk-example"
    lf.pull_projects = [LangfusePullProject("pk-pull-example", "sk-pull-example")]
    assert cfg.mcp_self_registration is not None
    cfg.mcp_self_registration.client_id = "example-client"
    cfg.mcp_self_registration.client_secret = "example-client-secret"
    assert cfg.data_retention_config is not None
    cfg.data_retention_config.audit_logs_retention_hours = 100


def _secrets(cfg: ServerConfig) -> Tuple[Any, ...]:
    claude, lf = cfg.claude_integration_config, cfg.langfuse_config
    assert claude is not None and lf is not None
    assert cfg.codex_integration_config is not None
    assert cfg.oidc_provider_config is not None
    assert cfg.mcp_self_registration is not None
    return (
        claude.anthropic_api_key,
        claude.voyageai_api_key,
        claude.cohere_api_key,
        claude.llm_creds_provider_api_key,
        cfg.codex_integration_config.api_key,
        cfg.oidc_provider_config.client_secret,
        lf.public_key,
        lf.secret_key,
        [(p.public_key, p.secret_key) for p in lf.pull_projects],
        cfg.mcp_self_registration.client_id,
        cfg.mcp_self_registration.client_secret,
    )


_EXPECTED_SECRETS = (
    "example-anthropic-key",
    "example-voyage-key",
    "example-cohere-key",
    "example-lcp-key",
    "example-openai-key",
    "example-oidc-secret",
    "pk-example",
    "sk-example",
    [("pk-pull-example", "sk-pull-example")],
    "example-client",
    "example-client-secret",
)


def _attach(service: ConfigService, b: SiemBackendHarness, db_path: Path) -> None:
    service.load_config()
    if b.name == "postgres":
        service.set_connection_pool(b.pool)
    else:
        service.initialize_runtime_db(str(db_path))


@pytest.fixture()
def node(
    siem_backend: SiemBackendHarness,  # noqa: F811  (the imported fixture)
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Tuple[ConfigService, SiemBackendHarness, Path]]:
    from code_indexer.server.services import config_service as config_service_module
    from code_indexer.server.storage.database_manager import DatabaseSchema

    server_dir = tmp_path / "node"
    server_dir.mkdir()
    # launch.json for this node only (read back by the launch assertions).
    monkeypatch.setattr(
        config_service_module, "LAUNCH_CONFIG_PATH", server_dir / "launch.json"
    )
    db_path = server_dir / "cidx_server.db"
    if siem_backend.name == "sqlite":
        DatabaseSchema(str(db_path)).initialize_database()
    (server_dir / "config.json").write_text(
        json.dumps({"server_dir": str(server_dir), **_BOOTSTRAP})
    )
    service = ConfigService(server_dir_path=str(server_dir))
    _attach(service, siem_backend, db_path)
    capture.reset_capture_state_for_tests()
    audit_capture.mark_server_process()
    audit_capture.bind_audit_service(siem_backend.audit, node_id=None)
    try:
        yield service, siem_backend, db_path
    finally:
        audit_capture.clear_audit_service()
        audit_capture.reset_server_process_mark()
        capture.reset_capture_state_for_tests()


def _assert_reset(cfg: ServerConfig) -> None:
    defaults = ServerConfig(server_dir=cfg.server_dir)
    # Bootstrap keys keep their values.
    assert cfg.storage_mode == "postgres"
    assert cfg.postgres_dsn == _BOOTSTRAP["postgres_dsn"]
    assert cfg.cluster is not None and cfg.cluster.node_id == "example-node-1"
    assert cfg.clone_backend == "cow-daemon"
    assert cfg.cow_daemon is not None
    assert cfg.cow_daemon.api_key == "example-daemon-key"
    assert cfg.server_threadpool_size == 64
    assert cfg.enable_malloc_trim is False
    # Launch settings keep their values.
    assert {k: getattr(cfg, k) for k in _LAUNCH} == _LAUNCH
    # Stored credentials keep their values.
    assert _secrets(cfg) == _EXPECTED_SECRETS
    # Runtime settings return to their defaults, secrets' sections included.
    assert cfg.data_retention_config == defaults.data_retention_config
    assert cfg.claude_integration_config is not None
    assert cfg.claude_integration_config.max_concurrent_claude_cli == 2
    assert cfg.langfuse_config is not None
    assert cfg.langfuse_config.enabled is False


_CREDENTIAL_NAME = re.compile(
    r"(^|_)(api_key|secret|secret_key|password|client_id|public_key|"
    r"private_key|access_token|auth_token)$"
)


def test_every_runtime_credential_field_is_kept_by_reset() -> None:
    """A credential field added to a runtime section later must be added to
    STORED_CREDENTIAL_FIELDS too, or a reset would silently clear it."""
    from code_indexer.server.services.config_reset import STORED_CREDENTIAL_FIELDS
    from code_indexer.server.services.config_service import BOOTSTRAP_KEYS

    cfg = ServerConfig(server_dir="/srv/example")
    found: Set[Tuple[str, str]] = set()
    for top in dataclasses.fields(cfg):
        section = getattr(cfg, top.name)
        if top.name in BOOTSTRAP_KEYS or not dataclasses.is_dataclass(section):
            assert top.name in BOOTSTRAP_KEYS or not _CREDENTIAL_NAME.search(
                top.name
            ), top.name
            continue
        found.update(
            (top.name, f.name)
            for f in dataclasses.fields(section)
            if _CREDENTIAL_NAME.search(f.name)
        )
    assert found  # the scan sees the known credentials
    assert found <= set(STORED_CREDENTIAL_FIELDS), found - set(STORED_CREDENTIAL_FIELDS)


_SECURITY_SECTIONS = frozenset(
    {
        "web_security_config",
        "password_security",
        "password_expiry_config",
        "oidc_provider_config",
        "mcp_session_config",
        "siem_delivery_config",
    }
)
_SECURITY_NAME = re.compile(
    r"(elevation|mfa|totp|sso|oidc|auth|login|session|password|restrict|"
    r"allowed|cidr|jwt|registration|csrf|pkce|role|permission|siem|credential)"
)
# Names that typically say WHICH deployment this is, rather than how it behaves.
_IDENTITY_NAME = re.compile(
    r"(_name$|^name$|environment|_url$|^url$|issuer|endpoint|(^|_)host$|label|"
    r"instance|display_name|vendor|consumer_id|_mode$|project|(^|_)location$|"
    r"region)"
)


def test_every_security_or_identity_field_is_a_recorded_reset_decision() -> None:
    """Every field of an auth/web-security section, and every runtime field
    with an auth/security/credential or deployment-identity name, is either
    kept by a reset or explicitly allow-listed as resettable -- nothing
    slips through unrecorded."""
    from code_indexer.server.services import config_reset as cr
    from code_indexer.server.services.config_service import BOOTSTRAP_KEYS

    preserved = (
        set(cr.PRESERVED_SECURITY_FIELDS)
        | set(cr.PRESERVED_IDENTITY_FIELDS)
        | set(cr.STORED_CREDENTIAL_FIELDS)
        | {(None, key) for key in cr.LAUNCH_KEYS}
    )
    resettable = set(cr.RESETTABLE_SECURITY_FIELDS) | set(cr.RESETTABLE_IDENTITY_FIELDS)
    assert not preserved & resettable
    whole = {section for section, f in preserved if f == cr.WHOLE_SECTION}

    def decided(section: Any, name: str) -> bool:
        key = (section, name)
        return section in whole or key in preserved or key in resettable

    def flagged(name: str) -> bool:
        return bool(_SECURITY_NAME.search(name) or _IDENTITY_NAME.search(name))

    cfg = ServerConfig(server_dir="/srv/example")
    undecided = []
    for top in dataclasses.fields(cfg):
        if top.name in BOOTSTRAP_KEYS:
            continue
        section = getattr(cfg, top.name)
        if not dataclasses.is_dataclass(section):
            if flagged(top.name) and not decided(None, top.name):
                undecided.append(top.name)
            continue
        for f in dataclasses.fields(section):
            guarded = top.name in _SECURITY_SECTIONS
            if (guarded or flagged(f.name)) and not decided(top.name, f.name):
                undecided.append(f"{top.name}.{f.name}")
    assert undecided == []


# (dotted path, non-default value): security controls a reset must not weaken.
_SECURITY_SETTINGS: List[Tuple[str, Any]] = [
    ("elevation_enforcement_enabled", True),
    ("elevation_idle_timeout_seconds", 600),
    ("elevation_max_age_seconds", 3600),
    ("jwt_expiration_minutes", 30),
    ("web_security_config.web_session_timeout_seconds", 3600),
    ("web_security_config.admin_session_timeout_seconds", 900),
    ("web_security_config.restrict_non_sso_to_web_ui", True),
    ("web_security_config.self_registration_enabled", True),
    ("mcp_session_config.session_ttl_seconds", 900),
    ("password_security.min_length", 16),
    ("password_expiry_config.enabled", True),
    ("password_expiry_config.max_age_days", 30),
    ("oidc_provider_config.issuer_url", "https://sso.example.com"),
    ("oidc_provider_config.default_role", "power_user"),
    ("oidc_provider_config.require_email_verification", False),
    ("siem_delivery_config.project_id", "example-project"),
    ("claude_integration_config.ra_curl_allowed_cidrs", ["192.0.2.0/24"]),
    # How stored credentials are used.
    ("claude_integration_config.claude_auth_mode", "subscription"),
    ("codex_integration_config.credential_mode", "api_key"),
]

# (dotted path, non-default value): which deployment this is.
_IDENTITY_SETTINGS: List[Tuple[str, Any]] = [
    ("service_display_name", "Example Search"),
    ("telemetry_config.service_name", "example-cidx"),
    ("telemetry_config.deployment_environment", "production"),
    ("telemetry_config.collector_endpoint", "http://192.0.2.20:4317"),
    ("claude_integration_config.llm_creds_provider_url", "http://192.0.2.30:8080"),
    ("claude_integration_config.llm_creds_provider_consumer_id", "example-node"),
    ("codex_integration_config.lcp_url", "http://192.0.2.30:8080"),
    ("codex_integration_config.lcp_vendor", "example-vendor"),
    ("langfuse_config.host", "https://langfuse.example.com"),
    ("langfuse_config.pull_host", "https://langfuse.example.com"),
    ("cidx_meta_backup_config.remote_url", "https://git.example.com/org/meta.git"),
]


def _get_path(cfg: Any, path: str) -> Any:
    for part in path.split("."):
        cfg = getattr(cfg, part)
    return cfg


def _set_path(cfg: Any, path: str, value: Any) -> None:
    *sections, field_name = path.split(".")
    for part in sections:
        cfg = getattr(cfg, part)
    setattr(cfg, field_name, value)


@pytest.mark.parametrize("path,value", _SECURITY_SETTINGS + _IDENTITY_SETTINGS)
def test_reset_keeps_setting(node, path: str, value: Any) -> None:
    service, backend, db_path = node
    default = _get_path(ServerConfig(server_dir="/srv/example"), path)
    assert value != default  # the case proves something
    service.apply_system_change(lambda cfg: _set_path(cfg, path, value))

    service.reset_to_defaults_audited(actor="example-admin")

    assert _get_path(service.get_config(), path) == value
    restarted = ConfigService(server_dir_path=str(Path(db_path).parent))
    _attach(restarted, backend, db_path)
    assert _get_path(restarted.get_config(), path) == value


def test_reset_keeps_bootstrap_launch_and_credentials(node) -> None:
    service, backend, db_path = node
    service.apply_system_change(_set_non_defaults)
    before = service._read_raw_launch_snapshot()
    assert before is not None

    service.reset_to_defaults_audited(actor="example-admin")

    _assert_reset(service.get_config())
    # Launch materialization: the committed row and launch.json carry the
    # kept launch values, and the reset requests no restart (generation
    # unchanged), so nothing is lost and no restart is spurious.
    after = service._read_raw_launch_snapshot()
    assert after == before
    assert {k: after[k] for k in _LAUNCH} == _LAUNCH
    launch = json.loads((Path(db_path).parent / "launch.json").read_text())
    assert launch == {
        **_LAUNCH,
        "target_restart_generation": before["launch_restart_generation"],
    }
    # A restarted process reads the committed row and config.json.
    restarted = ConfigService(server_dir_path=str(Path(db_path).parent))
    _attach(restarted, backend, db_path)
    _assert_reset(restarted.get_config())
    on_disk = json.loads((Path(db_path).parent / "config.json").read_text())
    for key, value in _BOOTSTRAP.items():
        if isinstance(value, dict):
            assert {k: on_disk[key][k] for k in value} == value, key
        else:
            assert on_disk[key] == value, key
