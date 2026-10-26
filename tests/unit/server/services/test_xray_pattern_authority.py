"""The X-Ray pattern write rule shared by store_xray_pattern and the
catalog file tools. Real group store and access filtering service; neutral
names only."""

from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.server.services.xray_pattern_authority import (
    SHARED_PATTERN_REFUSAL,
    PatternWriter,
    may_replace_pattern,
    may_write_scope,
    refuse_catalog_pattern_write,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    CANARY,
    GAMMA,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)

CATALOG_REFUSAL = "This path is not available for writing in this repository."


@pytest.fixture
def fx(tmp_path: Path) -> CidxMetaFixture:
    return build_cidx_meta_fixture(tmp_path / "data")


def _writer(fx: CidxMetaFixture, name: str = RESTRICTED_USER) -> PatternWriter:
    return PatternWriter(name, admin_role=False, access=fx.access_service)


@pytest.mark.parametrize(
    "scope,admin_role,allowed",
    [
        ("__any__", False, False),
        ("__any__", True, True),
        (ALPHA, False, True),
        (ALPHA, True, True),
    ],
)
def test_only_the_admin_role_replaces_shared_patterns(
    scope: str, admin_role: bool, allowed: bool
) -> None:
    assert may_replace_pattern(scope, admin_role=admin_role) is allowed


def test_repository_scope_needs_a_grant_by_golden_name(fx: CidxMetaFixture) -> None:
    writer = _writer(fx)

    assert may_write_scope(ALPHA, writer) is True
    assert may_write_scope(f"{ALPHA}-global", writer) is True
    assert may_write_scope(CANARY, writer) is False
    assert may_write_scope(GAMMA, writer) is False
    assert may_write_scope("__any__", writer) is True


def test_admins_group_member_is_granted_every_scope(fx: CidxMetaFixture) -> None:
    assert may_write_scope(GAMMA, _writer(fx, ADMIN_USER)) is True


def test_no_access_service_refuses_every_repository_scope() -> None:
    writer = PatternWriter(RESTRICTED_USER, admin_role=True, access=None)

    assert may_write_scope(ALPHA, writer) is False
    assert may_write_scope("__any__", writer) is True


def test_pattern_writer_of_reads_the_admin_role(fx: CidxMetaFixture) -> None:
    from tests.unit.server.mcp.catalog_file_door_env import user

    admin = PatternWriter.of(user(ADMIN_USER), fx.access_service)
    writer = PatternWriter.of(user(RESTRICTED_USER), fx.access_service)

    assert (admin.admin_role, writer.admin_role) == (True, False)
    assert writer.username == RESTRICTED_USER


def test_file_gate_ignores_paths_that_are_no_pattern_file(
    fx: CidxMetaFixture, tmp_path: Path
) -> None:
    root = fx.root
    targets = [
        root / f"{ALPHA}.md",
        root / "xray-patterns" / "loose.yaml",
        tmp_path / "elsewhere" / "xray-patterns" / "__any__" / "p.yaml",
    ]

    refuse_catalog_pattern_write(root, targets, replacing=True, writer=None)


def test_file_gate_refuses_by_scope_then_by_role(fx: CidxMetaFixture) -> None:
    root = fx.root
    shared = [root / "xray-patterns" / "__any__" / "p.yaml"]
    hidden = [root / "xray-patterns" / GAMMA / "p.yaml"]

    with pytest.raises(PermissionError, match=CATALOG_REFUSAL):
        refuse_catalog_pattern_write(root, hidden, replacing=False, writer=_writer(fx))
    with pytest.raises(PermissionError) as refused:
        refuse_catalog_pattern_write(root, shared, replacing=True, writer=_writer(fx))
    assert str(refused.value) == SHARED_PATTERN_REFUSAL
    refuse_catalog_pattern_write(root, shared, replacing=False, writer=_writer(fx))


def test_unnormalisable_pattern_path_is_refused(fx: CidxMetaFixture) -> None:
    admin = PatternWriter(ADMIN_USER, admin_role=True, access=fx.access_service)
    target = fx.root / "xray-patterns" / "__any__" / "a\\b.yaml"

    with pytest.raises(PermissionError, match=CATALOG_REFUSAL):
        refuse_catalog_pattern_write(fx.root, [target], replacing=False, writer=admin)
