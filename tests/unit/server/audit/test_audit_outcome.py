"""The shared outcome recorder used by every audited account operation."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator

import pytest

from _audit_accounts_support import (
    CAPTURE_LOGGER,
    AuditStore,
    bound_audit_store,
    capture_errors,
)
from code_indexer.server.services.audit_events import (
    UNKNOWN_ACCOUNT_ACTOR,
    SystemComponent,
)
from code_indexer.server.services.audit_outcome import (
    UNRESOLVED_TARGET,
    audit_target_id,
    conforming_details,
    record_outcome,
    record_outcome_async,
)


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[AuditStore]:
    yield from bound_audit_store(tmp_path / "groups.db")


def test_success_row_carries_actor_target_and_details(store) -> None:
    record_outcome(
        actor="example-admin",
        action_type="user_deleted",
        target_type="user",
        target_id="example-user",
        outcome="success",
        details={"deleted_role": "normal_user"},
    )
    (row,) = store.rows("user_")
    assert (row.actor, row.target_id, row.outcome) == (
        "example-admin",
        "example-user",
        "success",
    )
    assert row.details == {"deleted_role": "normal_user"}
    assert row.actor_is_system == 0


def test_non_conforming_user_target_becomes_the_unknown_placeholder(
    store, caplog
) -> None:
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    record_outcome(
        actor="example-admin",
        action_type="user_deleted",
        target_type="user",
        target_id="../not-a-name",
        outcome="failure",
    )
    (row,) = store.rows("user_")
    assert row.target_id == UNKNOWN_ACCOUNT_ACTOR
    assert capture_errors(caplog) == []


def test_non_conforming_opaque_target_becomes_unresolved(store) -> None:
    record_outcome(
        actor="example-admin",
        action_type="ssh_key_deleted",
        target_type="ssh_key",
        target_id="bad name with spaces",
        outcome="failure",
    )
    (row,) = store.rows("ssh_")
    assert row.target_id == UNRESOLVED_TARGET


def test_system_component_actor_is_a_system_row(store) -> None:
    record_outcome(
        actor=SystemComponent.SELF_REGISTRATION,
        action_type="user_created",
        target_type="user",
        target_id="example-user",
        outcome="success",
        details={"role": "normal_user", "provisioning": "self_registration"},
    )
    (row,) = store.rows("user_")
    assert row.actor == "system:self-registration"
    assert row.actor_is_system == 1
    assert row.source == "system"


def test_audit_target_id_keeps_conforming_values() -> None:
    assert audit_target_id("user", "example-user") == "example-user"
    assert audit_target_id("ssh_key", "example_key") == "example_key"
    assert audit_target_id("group", "*") == "*"
    assert audit_target_id("api_key", None) == UNRESOLVED_TARGET


def test_conforming_details_drops_values_outside_the_allowlist_types() -> None:
    details = conforming_details(
        "git_credential_configured",
        platform="github",
        forge_host="user:secret@example.com",
    )
    assert details == {"platform": "github"}
    details = conforming_details(
        "ssh_key_host_assigned", key_name="example_key", host="git.example.com"
    )
    assert details == {"key_name": "example_key", "host": "git.example.com"}
    assert conforming_details("git_credential_deleted", platform=None) == {}


def test_conforming_details_rejects_a_field_the_catalog_does_not_allow() -> None:
    with pytest.raises(KeyError):
        conforming_details("git_credential_configured", token="x")


async def test_async_variant_writes_off_the_event_loop(store, caplog) -> None:
    caplog.set_level(logging.ERROR, logger=CAPTURE_LOGGER)
    await record_outcome_async(
        actor="example-user",
        action_type="git_credential_deleted",
        target_type="git_credential",
        target_id="3f1c2a8e-0000-4000-8000-000000000001",
        outcome="success",
    )
    (row,) = store.rows("git_")
    assert row.outcome == "success"
    assert capture_errors(caplog) == []
