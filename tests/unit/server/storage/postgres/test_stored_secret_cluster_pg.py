"""Cluster stored-secret key, v2 write gate and capability heartbeat on a real
PostgreSQL (skipped without TEST_POSTGRES_DSN)."""

from __future__ import annotations

import json
import os
import threading
from typing import Iterator, List
from unittest import mock

import pytest

from code_indexer.server.services.stored_secret_key import (
    TOKEN_KEY_CAPABILITY,
    cluster_v2_writes_allowed,
    load_or_create_cluster_key,
)


@pytest.fixture
def pool(migrated_scratch_pg_dsn: str) -> Iterator[object]:
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

    p = ConnectionPool(migrated_scratch_pg_dsn, min_size=1, max_size=10, name="t")
    with p.connection() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS cluster_secrets (key_name TEXT PRIMARY KEY,"
            " key_value TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            " updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
        )
        conn.execute("DELETE FROM cluster_secrets")
        conn.execute("DELETE FROM cluster_nodes")
        conn.commit()
    yield p
    p.close()


def _node(pool: object, node_id: str, metadata: object, age_seconds: int = 0) -> None:
    with pool.connection() as conn:  # type: ignore[attr-defined]
        conn.execute(
            "INSERT INTO cluster_nodes (node_id, hostname, port, status,"
            " last_heartbeat, metadata) VALUES (%s, %s, 8000, 'online',"
            " NOW() - %s * INTERVAL '1 second', %s::jsonb)",
            (
                node_id,
                "192.0.2.10",
                age_seconds,
                None if metadata is None else json.dumps(metadata),
            ),
        )
        conn.commit()


def _capable() -> dict:
    import time

    return {"capabilities": [TOKEN_KEY_CAPABILITY], "capabilities_at": time.time()}


def test_cluster_key_converges_for_concurrent_creators(pool: object) -> None:
    keys: List[bytes] = []
    barrier = threading.Barrier(6)

    def create() -> None:
        barrier.wait()
        keys.append(load_or_create_cluster_key(pool))

    threads = [threading.Thread(target=create) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(keys) == 6 and len(set(keys)) == 1


def test_cluster_key_does_not_depend_on_hostname(pool: object) -> None:
    real = os.uname()
    with mock.patch(
        "os.uname",
        return_value=os.uname_result(
            (real.sysname, "node-a.example.com", real.release, real.version, "m")
        ),
    ):
        a = load_or_create_cluster_key(pool)
    with mock.patch(
        "os.uname",
        return_value=os.uname_result(
            (real.sysname, "node-b.example.com", real.release, real.version, "m")
        ),
    ):
        b = load_or_create_cluster_key(pool)
    assert a == b


def _capable_at(age_seconds: int) -> dict:
    import time

    return {
        "capabilities": [TOKEN_KEY_CAPABILITY],
        "capabilities_at": time.time() - age_seconds,
    }


def test_gate_closed_with_zero_rows(pool: object) -> None:
    assert cluster_v2_writes_allowed(pool, "node-a") is False


def test_gate_closed_without_this_nodes_capable_row(pool: object) -> None:
    _node(pool, "node-b", _capable())
    assert cluster_v2_writes_allowed(pool, "node-a") is False
    assert cluster_v2_writes_allowed(pool, None) is False


def test_gate_closed_on_query_error(migrated_scratch_pg_dsn: str) -> None:
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

    closed = ConnectionPool(migrated_scratch_pg_dsn, min_size=0, max_size=1, name="c")
    closed.close()
    assert cluster_v2_writes_allowed(closed, "node-a") is False


def test_gate_closed_while_a_node_lacks_capability(pool: object) -> None:
    _node(pool, "node-a", _capable())
    _node(pool, "node-b", {"other": 1})
    assert cluster_v2_writes_allowed(pool, "node-a") is False


def test_gate_open_when_every_node_is_capable(pool: object) -> None:
    _node(pool, "node-a", _capable())
    _node(pool, "node-b", _capable())
    assert cluster_v2_writes_allowed(pool, "node-a") is True


def test_paused_old_node_keeps_gate_closed(pool: object) -> None:
    _node(pool, "node-a", _capable())
    _node(pool, "node-old", None, age_seconds=600)
    assert cluster_v2_writes_allowed(pool, "node-a") is False


def test_node_gone_for_more_than_a_day_is_ignored(pool: object) -> None:
    _node(pool, "node-a", _capable())
    _node(pool, "node-gone", None, age_seconds=25 * 3600)
    assert cluster_v2_writes_allowed(pool, "node-a") is True


def test_late_joining_old_node_closes_gate_on_next_check(pool: object) -> None:
    _node(pool, "node-a", _capable())
    assert cluster_v2_writes_allowed(pool, "node-a") is True
    _node(pool, "node-late", {"zone": "example"})
    assert cluster_v2_writes_allowed(pool, "node-a") is False


def test_downgraded_node_with_stale_capability_stamp_closes_gate(
    pool: object,
) -> None:
    _node(pool, "node-a", _capable())
    _node(pool, "node-down", _capable_at(120))  # heartbeat fresh, stamp old
    assert cluster_v2_writes_allowed(pool, "node-a") is False


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        "garbage",
        [TOKEN_KEY_CAPABILITY],
        {"capabilities": TOKEN_KEY_CAPABILITY},
        {"capabilities": [TOKEN_KEY_CAPABILITY]},  # no freshness stamp
        {"capabilities": [TOKEN_KEY_CAPABILITY], "capabilities_at": "soon"},
        {"capabilities": [TOKEN_KEY_CAPABILITY], "capabilities_at": 1.0},
    ],
)
def test_null_garbage_or_stale_metadata_counts_as_not_capable(
    pool: object, metadata: object
) -> None:
    _node(pool, "node-a", _capable())
    _node(pool, "node-b", metadata)
    assert cluster_v2_writes_allowed(pool, "node-a") is False


def test_heartbeat_publishes_capability_and_keeps_other_metadata(
    pool: object,
) -> None:
    from code_indexer.server.services.node_heartbeat_service import (
        NodeHeartbeatService,
    )

    _node(pool, "node-a", {"zone": "example", "capabilities": ["old"]})
    service = NodeHeartbeatService(pool, "node-a")
    service.update_heartbeat()
    with pool.connection() as conn:  # type: ignore[attr-defined]
        row = conn.execute(
            "SELECT metadata FROM cluster_nodes WHERE node_id = 'node-a'"
        ).fetchone()
    metadata = row[0]
    assert metadata["zone"] == "example"
    assert metadata["capabilities"] == [TOKEN_KEY_CAPABILITY]
    assert cluster_v2_writes_allowed(pool, "node-a") is True


def test_heartbeat_registration_publishes_capability_over_garbage(
    pool: object,
) -> None:
    from code_indexer.server.services.node_heartbeat_service import (
        NodeHeartbeatService,
    )

    _node(pool, "node-a", "garbage")
    NodeHeartbeatService(pool, "node-a")._upsert_node(status="online")
    NodeHeartbeatService(pool, "node-new")._upsert_node(status="online")
    assert cluster_v2_writes_allowed(pool, "node-new") is True


def test_heartbeat_start_registers_local_node(pool: object) -> None:
    from code_indexer.server.services.node_heartbeat_service import (
        NodeHeartbeatService,
    )
    from code_indexer.server.services.stored_secret_key import (
        clear_cluster_key_source,
        local_node_id,
    )

    service = NodeHeartbeatService(pool, "node-z", heartbeat_interval=1)
    service.start()
    try:
        assert local_node_id() == "node-z"
        assert cluster_v2_writes_allowed(pool) is True
    finally:
        service.stop()
        clear_cluster_key_source()


def test_registered_local_node_is_used_by_default(pool: object) -> None:
    from code_indexer.server.services.stored_secret_key import (
        clear_cluster_key_source,
        register_local_node,
    )

    _node(pool, "node-a", _capable())
    try:
        register_local_node("node-a")
        assert cluster_v2_writes_allowed(pool) is True
    finally:
        clear_cluster_key_source()
    assert cluster_v2_writes_allowed(pool) is False


@pytest.fixture(params=["ci", "git"])
def pg_rows(request: object, pool: object) -> Iterator[tuple]:
    from code_indexer.server.storage.postgres.ci_tokens_backend import (
        CITokensPostgresBackend,
    )
    from code_indexer.server.storage.postgres.git_credentials_backend import (
        GitCredentialsPostgresBackend,
    )

    with pool.connection() as conn:  # type: ignore[attr-defined]
        conn.execute("DELETE FROM ci_tokens")
        conn.execute("DELETE FROM user_git_credentials")
        conn.commit()
    if request.param == "ci":  # type: ignore[attr-defined]
        ci = CITokensPostgresBackend(pool)
        ci.save_token("github", "old-value", None)
        yield ci, "github", lambda: ci.save_token("github", "v2:new", None)
    else:
        git = GitCredentialsPostgresBackend(pool)

        def upsert(value: str) -> None:
            git.upsert_credential(
                credential_id="cred-1",
                username="alice",
                forge_type="github",
                forge_host="git.example.com",
                encrypted_token=value,
            )

        upsert("old-value")
        yield git, "cred-1", lambda: upsert("v2:new")


def test_pg_cas_replaces_only_an_unchanged_value(pg_rows: tuple) -> None:
    backend, row_id, _ = pg_rows
    assert (
        backend.replace_encrypted_token_if_unchanged(row_id, "stale", "v2:x") is False
    )
    assert backend.replace_encrypted_token_if_unchanged(row_id, "old-value", "v2:x")
    assert [r["encrypted_token"] for r in backend.list_secret_rows()] == ["v2:x"]


@pytest.fixture
def cluster_source(pool: object) -> Iterator[object]:
    from code_indexer.server.services.stored_secret_key import (
        clear_cluster_key_source,
        configure_cluster_key_source,
        register_local_node,
    )

    register_local_node("node-a")
    with pool.connection() as conn:  # type: ignore[attr-defined]
        conn.execute("DELETE FROM user_git_credentials")
        conn.execute(
            "INSERT INTO cluster_secrets (key_name, key_value)"
            " VALUES ('jwt_secret', 'shared-cluster-jwt-example')"
        )
        conn.commit()
    configure_cluster_key_source(pool)
    yield pool
    clear_cluster_key_source()


def _git_manager(pool: object, server_dir: object) -> object:
    from code_indexer.server.services.git_credential_manager import (
        GitCredentialManager,
    )
    from code_indexer.server.storage.postgres.git_credentials_backend import (
        GitCredentialsPostgresBackend,
    )

    return GitCredentialManager(
        "unused.db",
        server_dir=str(server_dir),
        backend=GitCredentialsPostgresBackend(pool),
    )


def _seed_legacy_git(pool: object) -> str:
    from code_indexer.server.services.token_encryption import (
        derive_key_from_salt,
        encrypt_token,
    )
    from code_indexer.server.storage.postgres.git_credentials_backend import (
        GitCredentialsPostgresBackend,
    )

    legacy = encrypt_token("legacy-pat", derive_key_from_salt(os.uname().nodename))
    GitCredentialsPostgresBackend(pool).upsert_credential(
        credential_id="cred-l",
        username="alice",
        forge_type="github",
        forge_host="git.example.com",
        encrypted_token=legacy,
    )
    return legacy


def _stored_git(pool: object) -> str:
    with pool.connection() as conn:  # type: ignore[attr-defined]
        row = conn.execute(
            "SELECT encrypted_token FROM user_git_credentials WHERE credential_id"
            " = 'cred-l'"
        ).fetchone()
    return str(row[0])


def test_gate_closed_writes_legacy_and_does_not_rewrite(
    cluster_source: object, tmp_path: object
) -> None:
    _node(cluster_source, "node-new", _capable())
    _node(cluster_source, "node-old", {"zone": "example"})
    mgr = _git_manager(cluster_source, tmp_path)
    assert not mgr._encrypt_token("ghp_" + "f" * 36).startswith("v2:")  # type: ignore[attr-defined]
    legacy = _seed_legacy_git(cluster_source)
    cred = mgr.get_credential_for_host("alice", "git.example.com")  # type: ignore[attr-defined]
    assert cred["token"] == "legacy-pat"
    assert _stored_git(cluster_source) == legacy


def test_gate_open_writes_v2_and_rewrites_legacy(
    cluster_source: object, tmp_path: object
) -> None:
    _node(cluster_source, "node-a", _capable())
    _node(cluster_source, "node-b", _capable())
    mgr = _git_manager(cluster_source, tmp_path)
    assert mgr._encrypt_token("ghp_" + "f" * 36).startswith("v2:")  # type: ignore[attr-defined]
    _seed_legacy_git(cluster_source)
    cred = mgr.get_credential_for_host("alice", "git.example.com")  # type: ignore[attr-defined]
    assert cred["token"] == "legacy-pat"
    assert _stored_git(cluster_source).startswith("v2:")


def test_nodes_with_different_server_dirs_share_the_cluster_key(
    cluster_source: object, tmp_path: object
) -> None:
    _node(cluster_source, "node-a", _capable())
    a = _git_manager(cluster_source, tmp_path / "a")  # type: ignore[operator]
    b = _git_manager(cluster_source, tmp_path / "b")  # type: ignore[operator]
    value = a._encrypt_token("ghp_" + "g" * 36)  # type: ignore[attr-defined]
    assert b._cipher.decrypt(value) == ("ghp_" + "g" * 36, False)  # type: ignore[attr-defined]


def test_solo_v2_value_is_read_and_rewritten_after_cluster_conversion(
    cluster_source: object, tmp_path: object
) -> None:
    from code_indexer.server.services.stored_secret_key import (
        load_or_create_cluster_key,
        load_or_create_key_file,
    )
    from code_indexer.server.services.token_encryption import decrypt_v2, encrypt_v2
    from code_indexer.server.storage.postgres.git_credentials_backend import (
        GitCredentialsPostgresBackend,
    )

    _node(cluster_source, "node-a", _capable())
    solo_key = load_or_create_key_file(tmp_path)  # type: ignore[arg-type]
    GitCredentialsPostgresBackend(cluster_source).upsert_credential(
        credential_id="cred-l",
        username="alice",
        forge_type="github",
        forge_host="git.example.com",
        encrypted_token=encrypt_v2("solo-pat", solo_key),
    )
    mgr = _git_manager(cluster_source, tmp_path)
    cred = mgr.get_credential_for_host("alice", "git.example.com")  # type: ignore[attr-defined]
    assert cred["token"] == "solo-pat"
    cluster_key = load_or_create_cluster_key(cluster_source)
    assert decrypt_v2(_stored_git(cluster_source), cluster_key) == "solo-pat"


@pytest.mark.parametrize("capable", [True, False])
def test_lease_state_rewritten_only_when_gate_open(
    cluster_source: object, tmp_path: object, capable: bool
) -> None:
    from code_indexer.server.config.llm_lease_state import (
        LlmLeaseState,
        LlmLeaseStateManager,
    )

    _node(cluster_source, "node-a", _capable() if capable else None)
    mgr = LlmLeaseStateManager(server_dir_path=str(tmp_path))
    mgr.set_connection_pool(cluster_source)
    legacy = mgr._encrypt(
        json.dumps(
            {"lease_id": "l-1", "credential_id": "c-1", "credential_type": "oauth"}
        )
    )
    with cluster_source.connection() as conn:  # type: ignore[attr-defined]
        conn.execute(
            "INSERT INTO cluster_secrets (key_name, key_value)"
            " VALUES ('llm_lease_state', %s)",
            (legacy,),
        )
        conn.commit()
    assert mgr.load_state() == LlmLeaseState("l-1", "c-1")
    with cluster_source.connection() as conn:  # type: ignore[attr-defined]
        row = conn.execute(
            "SELECT key_value FROM cluster_secrets WHERE key_name = 'llm_lease_state'"
        ).fetchone()
    assert str(row[0]).startswith("v2:") is capable


def test_pg_mark_listed_without_secret_and_cleared_by_save(pg_rows: tuple) -> None:
    backend, row_id, save_new = pg_rows
    assert backend.mark_needs_reentry(row_id, "changed") is False
    assert backend.mark_needs_reentry(row_id, "old-value") is True
    marked = backend.list_needs_reentry()
    assert [m["id"] for m in marked] == [row_id]
    assert "old-value" not in repr(marked)
    save_new()
    assert backend.list_needs_reentry() == []
