"""MetaVisibility: who may see what of the internal catalog repository.

Real filesystem fixture, real GroupAccessManager storage (SQLite, and
PostgreSQL when TEST_POSTGRES_DSN is set). Neutral names only.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Iterator

import pytest

from code_indexer.server.services.access_filtering_service import (
    AccessFilteringService,
)
from code_indexer.server.services.cidx_meta_access import (
    MetaVisibility,
    acting_scope,
    resolve_meta_visibility,
)
from code_indexer.server.services.group_access_manager import GroupAccessManager
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    CANARY,
    GAMMA,
    OTHER_USER,
    RESTRICTED_USER,
    UNGROUPED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)


@pytest.fixture
def fx(tmp_path: Path) -> CidxMetaFixture:
    return build_cidx_meta_fixture(tmp_path)


def test_admins_group_member_is_unrestricted(fx: CidxMetaFixture) -> None:
    vis = resolve_meta_visibility(ADMIN_USER, fx.access_service)
    assert vis.unrestricted is True
    assert vis.visible == frozenset()


def test_visible_set_excludes_meta_and_ungranted(fx: CidxMetaFixture) -> None:
    vis = resolve_meta_visibility(RESTRICTED_USER, fx.access_service)
    assert vis.unrestricted is False
    assert vis.visible == frozenset({ALPHA})
    for hidden in ("cidx-meta", CANARY, GAMMA):
        assert hidden not in vis.visible


def test_user_in_no_group_sees_nothing(fx: CidxMetaFixture) -> None:
    vis = resolve_meta_visibility(UNGROUPED_USER, fx.access_service)
    assert vis.unrestricted is False
    assert vis.visible == frozenset()


def test_missing_access_service_is_empty_set() -> None:
    vis = resolve_meta_visibility(RESTRICTED_USER, None)
    assert vis.unrestricted is False
    assert vis.visible == frozenset()


def test_acting_scope_never_widens_the_callers_own_grants(
    fx: CidxMetaFixture,
) -> None:
    with acting_scope({ALPHA, CANARY}):
        vis = resolve_meta_visibility(RESTRICTED_USER, fx.access_service)
    assert vis.visible == frozenset({ALPHA})


def test_missing_access_service_fails_closed_even_with_acting_scope() -> None:
    with acting_scope({ALPHA}):
        vis = resolve_meta_visibility(ADMIN_USER, None)
    assert vis.unrestricted is False
    assert vis.visible == frozenset()


def test_grant_change_is_seen_on_the_next_resolution(fx: CidxMetaFixture) -> None:
    team = fx.group_manager.get_group_by_name("fixture-team")
    assert team is not None
    before = resolve_meta_visibility(RESTRICTED_USER, fx.access_service)
    fx.group_manager.grant_repo_access(GAMMA, team.id, granted_by="test")
    after = resolve_meta_visibility(RESTRICTED_USER, fx.access_service)
    assert before.visible == frozenset({ALPHA})
    assert after.visible == frozenset({ALPHA, GAMMA})


def test_restricted_value_normalises_global_suffix_and_drops_catalog() -> None:
    vis = MetaVisibility.restricted(
        [f"{ALPHA}-global", "cidx-meta", "cidx-meta-global"]
    )
    assert vis.visible == frozenset({ALPHA})


def test_vis_hash_is_shared_by_equal_sets_only(fx: CidxMetaFixture) -> None:
    first = resolve_meta_visibility(RESTRICTED_USER, fx.access_service)
    same_set = MetaVisibility.restricted([ALPHA])
    other = resolve_meta_visibility(OTHER_USER, fx.access_service)
    admin = resolve_meta_visibility(ADMIN_USER, fx.access_service)
    assert first.visible == same_set.visible
    assert first.vis_hash == same_set.vis_hash
    assert first.vis_hash != other.vis_hash
    assert admin.vis_hash not in (first.vis_hash, other.vis_hash)


def test_acting_scope_restricts_an_admin_and_resets(fx: CidxMetaFixture) -> None:
    with acting_scope({ALPHA, "cidx-meta"}):
        scoped = resolve_meta_visibility(ADMIN_USER, fx.access_service)
    after = resolve_meta_visibility(ADMIN_USER, fx.access_service)
    assert scoped.unrestricted is False
    assert scoped.visible == frozenset({ALPHA})
    assert after.unrestricted is True


def test_acting_scope_none_clears_an_outer_scope(fx: CidxMetaFixture) -> None:
    with acting_scope({ALPHA}):
        with acting_scope(None):
            inner = resolve_meta_visibility(ADMIN_USER, fx.access_service)
        outer = resolve_meta_visibility(ADMIN_USER, fx.access_service)
    assert inner.unrestricted is True
    assert outer.visible == frozenset({ALPHA})


def test_restricted_value_never_holds_the_catalog() -> None:
    with pytest.raises(ValueError):
        MetaVisibility(
            unrestricted=False, visible=frozenset({"cidx-meta"}), vis_hash="x"
        )


_DSN = os.environ.get("TEST_POSTGRES_DSN", "")


@pytest.fixture
def pg_group_manager() -> Iterator[GroupAccessManager]:
    if not _DSN:
        pytest.skip("No PostgreSQL available (set TEST_POSTGRES_DSN to enable)")
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
    from code_indexer.server.storage.postgres.groups_backend import (
        GroupsPostgresBackend,
    )
    from code_indexer.server.storage.postgres.migrations.runner import (
        MigrationRunner,
    )

    name = f"metavis_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(_DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    params = conninfo_to_dict(_DSN)
    params["dbname"] = name
    dsn = make_conninfo(**params)  # type: ignore[arg-type]
    pool = None
    try:
        with MigrationRunner(dsn) as runner:
            runner.run()
        pool = ConnectionPool(dsn, min_size=1, max_size=2)
        yield GroupAccessManager(
            Path("unused.db"), storage_backend=GroupsPostgresBackend(pool)
        )
    finally:
        if pool is not None:
            pool.close()
        with psycopg.connect(_DSN, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_postgres_group_store_resolves_the_same_visibility(
    pg_group_manager: GroupAccessManager,
) -> None:
    gam = pg_group_manager
    team = gam.create_group("fixture-team", "restricted test group")
    other = gam.create_group("other-team", "canary owner group")
    gam.grant_repo_access(ALPHA, team.id, granted_by="test")
    gam.grant_repo_access(CANARY, other.id, granted_by="test")
    gam.assign_user_to_group(RESTRICTED_USER, team.id, assigned_by="test")
    admins = gam.get_group_by_name("admins")
    assert admins is not None
    gam.assign_user_to_group(ADMIN_USER, admins.id, assigned_by="test")
    service = AccessFilteringService(gam)

    assert resolve_meta_visibility(RESTRICTED_USER, service).visible == {ALPHA}
    assert resolve_meta_visibility(ADMIN_USER, service).unrestricted is True
