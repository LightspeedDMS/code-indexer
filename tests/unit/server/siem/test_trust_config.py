"""The trusted CA through the audited configuration path, on a real
ConfigService over SQLite AND PostgreSQL: persisted in the committed
``siem_delivery`` section, recorded in the config-change audit with its
fingerprint, picked up by the scheduler through the committed-config read,
and settable only through the elevated CA routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

from code_indexer.server.services import audit_capture
from code_indexer.server.services import config_service as config_service_mod
from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.services.siem_delivery import capture
from code_indexer.server.services.siem_delivery.trust import (
    SiemTrustInvalid,
    remove_trusted_ca,
    set_trusted_ca,
)
from code_indexer.server.storage.json_column import parse_json_column

from .backends import SiemBackendHarness
from .tls_fixtures import make_ca, make_leaf

FP_KEY = "siem_delivery_config.trusted_ca_fingerprint"


@pytest.fixture()
def svc(siem_backend: SiemBackendHarness, tmp_path: Path) -> Iterator[ConfigService]:
    server_dir = tmp_path / "server"
    server_dir.mkdir()
    service = ConfigService(server_dir_path=str(server_dir))
    service.load_config()
    if siem_backend.name == "postgres":
        service.set_connection_pool(siem_backend.pool)
    else:
        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = server_dir / "cidx_server.db"
        DatabaseSchema(str(db_path)).initialize_database()
        service.initialize_runtime_db(str(db_path))
    capture.reset_capture_state_for_tests()
    audit_capture.mark_server_process()
    audit_capture.bind_audit_service(siem_backend.audit, node_id=None)
    try:
        yield service
    finally:
        audit_capture.clear_audit_service()
        audit_capture.reset_server_process_mark()
        capture.reset_capture_state_for_tests()


def _config_rows(b: SiemBackendHarness) -> List[Dict[str, Any]]:
    rows = b.db.read(
        lambda tx: tx.query(
            "SELECT target_id, details FROM audit_logs WHERE action_type = ? "
            "ORDER BY id",
            ("config_changed",),
        )
    )
    out = []
    for row in rows:
        details = parse_json_column(row["details"], dict, "details")
        assert details is not None
        out.append({"target_id": row["target_id"], **details})
    return out


def test_set_replace_remove_persist_and_audit_the_fingerprint(
    svc: ConfigService, siem_backend: SiemBackendHarness
) -> None:
    first, second = make_ca("Example CA One"), make_ca("Example CA Two")
    one = set_trusted_ca(svc, "alice", first.pem)
    assert one["change"] == "set"
    _version, section = svc.read_committed_section("siem_delivery_config")
    assert section["trusted_ca_pem"] == first.pem
    assert section["trusted_ca_fingerprint"] == one["fingerprint"]
    assert one["certificates"][0]["subject"] == "CN=Example CA One"

    two = set_trusted_ca(svc, "bob", second.pem)
    assert two["change"] == "replaced"
    removed = remove_trusted_ca(svc, "carol")
    assert removed["change"] == "removed"
    _version, section = svc.read_committed_section("siem_delivery_config")
    assert section["trusted_ca_pem"] == "" and section["trusted_ca_fingerprint"] == ""
    with pytest.raises(SiemTrustInvalid):
        remove_trusted_ca(svc, "carol")

    rows = [r for r in _config_rows(siem_backend) if FP_KEY in r.get("values", {})]
    assert [r["values"][FP_KEY] for r in rows] == [
        ["", one["fingerprint"]],
        [one["fingerprint"], two["fingerprint"]],
        [two["fingerprint"], ""],
    ]
    assert all(r["target_id"] == "siem_delivery" for r in rows)


def test_invalid_bundles_are_rejected_and_nothing_is_saved(
    svc: ConfigService, siem_backend: SiemBackendHarness
) -> None:
    ca = make_ca()
    for bad in (
        make_leaf(ca).pem,
        make_ca(expired=True).pem,
        make_ca(is_ca=False).pem,
        "not a certificate",
    ):
        with pytest.raises(SiemTrustInvalid):
            set_trusted_ca(svc, "alice", bad)
    _version, section = svc.read_committed_section("siem_delivery_config")
    assert section.get("trusted_ca_pem", "") == ""
    assert not [r for r in _config_rows(siem_backend) if FP_KEY in r.get("values", {})]


def test_the_generic_section_form_cannot_set_the_ca(svc: ConfigService) -> None:
    for key in ("trusted_ca_pem", "trusted_ca_fingerprint"):
        with pytest.raises(ValueError):
            svc.update_settings_atomic([("siem_delivery", key, make_ca().pem)])


def test_the_scheduler_destination_picks_up_the_committed_ca(
    svc: ConfigService, siem_backend: SiemBackendHarness
) -> None:
    from code_indexer.server.fault_injection.http_client_factory import (
        HttpClientFactory,
    )
    from code_indexer.server.services.siem_delivery.credential import (
        SiemCredentialStore,
    )
    from code_indexer.server.services.siem_delivery.scheduler import (
        SiemDeliveryScheduler,
    )

    from .conftest import TEST_ENCRYPTION_KEY

    svc.update_settings_atomic(
        [
            ("siem_delivery", "region", "us"),
            ("siem_delivery", "project_id", "example-project"),
            ("siem_delivery", "location", "us"),
            ("siem_delivery", "instance_id", "example-instance"),
        ]
    )
    scheduler = SiemDeliveryScheduler(
        db=siem_backend.db,
        config_service=svc,
        background_job_manager=None,
        http_client_factory=HttpClientFactory(fault_injection_service=None),
        harness_active=False,
        node_id=None,
        credential_store=SiemCredentialStore(siem_backend.db, TEST_ENCRYPTION_KEY),
    )
    ctx = scheduler.committed_context()
    assert ctx is not None and ctx.destination.trusted_ca_pem == ""
    key_before = ctx.destination.key
    ca = make_ca()
    set_trusted_ca(svc, "alice", ca.pem)
    ctx = scheduler.committed_context()
    assert ctx is not None and ctx.destination.trusted_ca_pem == ca.pem
    assert ctx.destination.key == key_before  # the tenant identity is unchanged


@pytest.mark.parametrize(
    "path",
    [
        "/config/siem_delivery/trusted_ca",
        "/config/siem_delivery/trusted_ca/remove",
        "/config/siem_delivery/credential",
        "/config/siem_delivery/credential/remove",
    ],
)
def test_ca_and_credential_routes_require_elevation(path: str) -> None:
    from code_indexer.server.web.routes import web_router

    routes = [r for r in web_router.routes if getattr(r, "path", None) == path]
    assert len(routes) == 1, path
    deps = [d.dependency for d in routes[0].dependencies]  # type: ignore[attr-defined]
    assert any("require_elevation" in getattr(d, "__qualname__", "") for d in deps)


@pytest.fixture(autouse=True)
def _reset_global_config_service() -> Iterator[None]:
    yield
    config_service_mod.reset_config_service()
