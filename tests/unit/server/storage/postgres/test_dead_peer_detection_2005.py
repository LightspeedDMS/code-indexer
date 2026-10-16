"""Bug #2005: bound how long a crashed node's PostgreSQL session survives.

A crashed leader's backend (and therefore the leader advisory lock) is only
dropped by the PostgreSQL SERVER once the server's own end of the socket
detects the dead peer. That is governed by the server-side session GUCs
``tcp_keepalives_idle`` / ``tcp_keepalives_interval`` /
``tcp_keepalives_count`` / ``tcp_user_timeout`` -- NOT by libpq's client
``keepalives*`` parameters, which only tune the client's own socket.

These tests pin that every long-lived server connection (leader lock
connection, pooled connections) carries those server GUCs via the libpq
``options`` startup parameter, plus client keepalives / tcp_user_timeout,
without clobbering anything the operator already set in ``postgres_dsn``.

``psycopg.connect`` is patched only to capture the conninfo handed to the
external driver; the effective libpq parameters are computed with psycopg's
own ``make_conninfo`` / ``conninfo_to_dict`` (the same merge psycopg does).
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402

from code_indexer.server.services.leader_election_service import (  # noqa: E402
    LeaderElectionService,
)

_OPERATOR_DSN = "postgresql://cidx@db.example.com:5432/cidx"

_SERVER_GUCS = {
    "tcp_keepalives_idle": "30",
    "tcp_keepalives_interval": "10",
    "tcp_keepalives_count": "3",
    "tcp_user_timeout": "60000",
}


def _effective_params(call_args: Any) -> Dict[str, Any]:
    """Merge positional conninfo + libpq kwargs exactly as psycopg does."""
    args, kwargs = call_args
    libpq_kwargs = {k: v for k, v in kwargs.items() if k != "autocommit"}
    return dict(conninfo_to_dict(make_conninfo(args[0], **libpq_kwargs)))


def _options_gucs(options: str) -> Dict[str, str]:
    """Parse ``-c name=value`` pairs out of a libpq ``options`` string."""
    tokens = options.split()
    gucs: Dict[str, str] = {}
    for i, tok in enumerate(tokens):
        if tok == "-c" and i + 1 < len(tokens):
            name, _, value = tokens[i + 1].partition("=")
            gucs[name] = value
    return gucs


def _leader_connect_params(dsn: str) -> Dict[str, Any]:
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = (False,)
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    service = LeaderElectionService(connection_string=dsn, node_id="node-test")
    with patch("psycopg.connect", return_value=conn) as mock_connect:
        service.try_acquire_leadership()
    assert mock_connect.call_count == 1
    return _effective_params(mock_connect.call_args)


# ---------------------------------------------------------------------------
# Leader advisory-lock connection
# ---------------------------------------------------------------------------


def test_leader_connection_sets_server_side_dead_client_gucs():
    """The lock connection must ask the SERVER to detect a dead client fast."""
    params = _leader_connect_params(_OPERATOR_DSN)

    gucs = _options_gucs(params.get("options", ""))
    assert gucs == _SERVER_GUCS
    # Client side: keepalives on and the tighter leader-only 30s
    # tcp_user_timeout (pooled/other connections get 60s).
    assert params["keepalives"] == "1"
    assert params["tcp_user_timeout"] == "30000"
    # Operator DSN identity preserved.
    assert params["host"] == "db.example.com"
    assert params["user"] == "cidx"
    assert params["dbname"] == "cidx"


def test_leader_connection_preserves_operator_dsn_values():
    """Explicit operator values in postgres_dsn are never overridden."""
    dsn = (
        _OPERATOR_DSN + "?keepalives_idle=120&tcp_user_timeout=15000"
        "&options=-c%20tcp_keepalives_idle%3D300%20-c%20statement_timeout%3D0"
    )
    params = _leader_connect_params(dsn)

    assert params["keepalives_idle"] == "120"
    assert params["tcp_user_timeout"] == "15000"
    gucs = _options_gucs(params["options"])
    assert gucs["tcp_keepalives_idle"] == "300"
    assert gucs["statement_timeout"] == "0"
    # Missing server GUCs are still added alongside the operator's own.
    assert gucs["tcp_keepalives_interval"] == "10"
    assert gucs["tcp_keepalives_count"] == "3"
    assert gucs["tcp_user_timeout"] == "60000"


# ---------------------------------------------------------------------------
# Leader: one WARNING when the operator DSN switches detection off
# ---------------------------------------------------------------------------


def _dead_peer_warnings_at_start(dsn: str, caplog: Any) -> list:
    service = LeaderElectionService(connection_string=dsn, node_id="node-warn")
    caplog.clear()
    with caplog.at_level("WARNING"):
        with patch("psycopg.connect", side_effect=OSError("no database here")):
            service.start_monitoring(check_interval=3600)
            service.stop_monitoring()
    return [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "dead-peer detection" in r.getMessage()
    ]


@pytest.mark.parametrize(
    "dsn, disabled_setting",
    [
        (_OPERATOR_DSN + "?keepalives=0", "keepalives"),
        (_OPERATOR_DSN + "?tcp_user_timeout=0", "tcp_user_timeout"),
        (
            _OPERATOR_DSN + "?options=-c%20tcp_keepalives_idle%3D0",
            "tcp_keepalives_idle",
        ),
        (
            _OPERATOR_DSN + "?options=-c%20tcp_keepalives_idle%3D0h",
            "tcp_keepalives_idle",
        ),
        (
            _OPERATOR_DSN + "?options=-c%20tcp_user_timeout%3D0us",
            "options -c tcp_user_timeout",
        ),
        (
            _OPERATOR_DSN + "?options=-c%20tcp_keepalives_count%3D0.0",
            "tcp_keepalives_count",
        ),
        (
            _OPERATOR_DSN + "?options=-c%20tcp_keepalives_interval%3D0d",
            "tcp_keepalives_interval",
        ),
    ],
)
def test_leader_warns_once_when_operator_dsn_disables_dead_peer_detection(
    dsn: str, disabled_setting: str, caplog: Any
):
    warnings = _dead_peer_warnings_at_start(dsn, caplog)

    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert disabled_setting in message
    assert "unbounded" in message


def test_leader_dead_peer_warning_once_per_service_instance(caplog: Any):
    service = LeaderElectionService(
        connection_string=_OPERATOR_DSN + "?keepalives=0", node_id="node-twice"
    )
    caplog.clear()
    with caplog.at_level("WARNING"):
        with patch("psycopg.connect", side_effect=OSError("no database here")):
            for _ in range(2):
                service.start_monitoring(check_interval=3600)
                service.stop_monitoring()

    warnings = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "dead-peer detection" in r.getMessage()
    ]
    assert len(warnings) == 1


@pytest.mark.parametrize(
    "dsn",
    [
        _OPERATOR_DSN,
        _OPERATOR_DSN + "?options=-c%20tcp_keepalives_idle%3D30s",
        _OPERATOR_DSN + "?options=-c%20tcp_keepalives_idle%3D0x",  # unknown unit
        _OPERATOR_DSN + "?options=-c%20tcp_keepalives_idle%3D0.0.0",  # not a number
    ],
)
def test_leader_no_dead_peer_warning_for_default_dsn(dsn: str, caplog: Any):
    assert _dead_peer_warnings_at_start(dsn, caplog) == []


# ---------------------------------------------------------------------------
# libpq `options` merge: escape-aware, case-insensitive, operator wins
# ---------------------------------------------------------------------------

_IDLE = "-c tcp_keepalives_idle=30"
_INTERVAL = "-c tcp_keepalives_interval=10"
_COUNT = "-c tcp_keepalives_count=3"
_USER_TIMEOUT = "-c tcp_user_timeout=60000"
_ALL_OURS = " ".join([_IDLE, _INTERVAL, _COUNT, _USER_TIMEOUT])
_KV_DSN = "host=db.example.com dbname=cidx user=cidx"
_URI_DSN = "postgresql://cidx@db.example.com:5432/cidx"


@pytest.mark.parametrize(
    "base_dsn, operator_options, expected_options",
    [
        pytest.param(_KV_DSN, None, _ALL_OURS, id="kv-dsn-no-options"),
        pytest.param(_URI_DSN, None, _ALL_OURS, id="uri-dsn-no-options"),
        pytest.param(_URI_DSN, "   ", _ALL_OURS, id="whitespace-only"),
        # The escaped spaces make this ONE search_path value; the lookalike
        # GUC text inside it is not an operator setting.
        pytest.param(
            _KV_DSN,
            r"-c search_path=x\ -c\ tcp_keepalives_idle=5",
            r"-c search_path=x\ -c\ tcp_keepalives_idle=5 " + _ALL_OURS,
            id="escaped-lookalike-inside-value",
        ),
        # Escaped trailing whitespace belongs to the value; never stripped.
        pytest.param(
            _URI_DSN,
            "-c application_name=a\\ ",
            "-c application_name=a\\  " + _ALL_OURS,
            id="escaped-trailing-space",
        ),
        # A lone (odd) trailing backslash escapes nothing; PostgreSQL drops
        # it. Kept, it would escape our separator and swallow our first -c.
        pytest.param(
            _URI_DSN,
            "-c application_name=a\\",
            "-c application_name=a " + _ALL_OURS,
            id="odd-trailing-backslash-1",
        ),
        pytest.param(
            _URI_DSN,
            "-c application_name=a\\\\\\",
            "-c application_name=a\\\\ " + _ALL_OURS,
            id="odd-trailing-backslash-3",
        ),
        pytest.param(
            _URI_DSN,
            "-c application_name=a\\\\",
            "-c application_name=a\\\\ " + _ALL_OURS,
            id="even-trailing-backslash-pair-kept",
        ),
        pytest.param(
            _URI_DSN,
            "-c TCP_KEEPALIVES_IDLE=300",
            "-c TCP_KEEPALIVES_IDLE=300 "
            + " ".join([_INTERVAL, _COUNT, _USER_TIMEOUT]),
            id="upper-case-dash-c",
        ),
        pytest.param(
            _KV_DSN,
            "--TCP-User-Timeout=5000",
            "--TCP-User-Timeout=5000 " + " ".join([_IDLE, _INTERVAL, _COUNT]),
            id="mixed-case-dashed-long-form",
        ),
        pytest.param(
            _URI_DSN,
            "-ctcp_keepalives_count=7",
            "-ctcp_keepalives_count=7 " + " ".join([_IDLE, _INTERVAL, _USER_TIMEOUT]),
            id="attached-dash-c",
        ),
        pytest.param(
            _KV_DSN,
            "-c tcp_keepalives_interval=20",
            "-c tcp_keepalives_interval=20 " + " ".join([_IDLE, _COUNT, _USER_TIMEOUT]),
            id="operator-sets-one-of-ours",
        ),
    ],
)
def test_options_merge_respects_libpq_escapes_and_operator_gucs(
    base_dsn: str, operator_options: Any, expected_options: str
):
    from code_indexer.server.storage.postgres.dead_peer_detection import (
        apply_dead_peer_detection,
    )

    dsn = (
        base_dsn
        if operator_options is None
        else make_conninfo(base_dsn, options=operator_options)
    )
    params = dict(conninfo_to_dict(apply_dead_peer_detection(dsn)))

    assert params["options"] == expected_options
    assert params["host"] == "db.example.com"
    assert params["dbname"] == "cidx"


# ---------------------------------------------------------------------------
# PGOPTIONS: libpq's default for `options` must not be silently discarded
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_ambient_pgoptions(monkeypatch: Any) -> None:
    """Tests never depend on the developer's own PGOPTIONS."""
    monkeypatch.delenv("PGOPTIONS", raising=False)


_ENV_OPTIONS = "-c application_name=from_env -c tcp_keepalives_idle=77"


def test_pgoptions_env_is_seed_when_dsn_has_no_options(monkeypatch: Any):
    from code_indexer.server.storage.postgres.dead_peer_detection import (
        apply_dead_peer_detection,
    )

    monkeypatch.setenv("PGOPTIONS", _ENV_OPTIONS)
    params = dict(conninfo_to_dict(apply_dead_peer_detection(_URI_DSN)))

    # Env value kept verbatim; its tcp_keepalives_idle wins over ours.
    assert params["options"] == " ".join(
        [_ENV_OPTIONS, _INTERVAL, _COUNT, _USER_TIMEOUT]
    )


def test_explicit_dsn_options_override_pgoptions(monkeypatch: Any):
    from code_indexer.server.storage.postgres.dead_peer_detection import (
        apply_dead_peer_detection,
    )

    monkeypatch.setenv("PGOPTIONS", _ENV_OPTIONS)
    dsn = make_conninfo(_URI_DSN, options="-c application_name=from_dsn")
    params = dict(conninfo_to_dict(apply_dead_peer_detection(dsn)))

    assert params["options"] == "-c application_name=from_dsn " + _ALL_OURS


# ---------------------------------------------------------------------------
# Shared connection pool (every PostgreSQL backend goes through it)
# ---------------------------------------------------------------------------


def test_connection_pool_conninfo_carries_dead_peer_detection():
    with patch(
        "code_indexer.server.storage.postgres.connection_pool._PsycopgPool"
    ) as mock_psycopg_pool:
        from code_indexer.server.storage.postgres.connection_pool import (
            ConnectionPool,
        )

        ConnectionPool(_OPERATOR_DSN + "?keepalives_count=9")

    mock_psycopg_pool.assert_called_once()
    params = dict(conninfo_to_dict(mock_psycopg_pool.call_args[0][0]))
    assert _options_gucs(str(params["options"])) == _SERVER_GUCS
    assert params["keepalives"] == "1"
    assert params["tcp_user_timeout"] == "60000"
    assert params["keepalives_count"] == "9"  # operator value kept
    assert params["host"] == "db.example.com"


# ---------------------------------------------------------------------------
# Other dedicated connections that hold session-level locks
# ---------------------------------------------------------------------------


def _assert_dead_peer_detection(params: Dict[str, Any]) -> None:
    assert _options_gucs(params["options"]) == _SERVER_GUCS
    assert params["keepalives"] == "1"
    assert params["tcp_user_timeout"] == "60000"
    assert params["host"] == "db.example.com"


def test_alias_lock_store_connection_carries_dead_peer_detection():
    """Alias locks are row locks held by an open session transaction."""
    from code_indexer.server.services.alias_lock_store import postgres_store

    with patch("psycopg.connect", return_value=MagicMock()) as mock_connect:
        postgres_store._connect(_OPERATOR_DSN, 5.0)

    params = _effective_params(mock_connect.call_args)
    _assert_dead_peer_detection(params)
    assert params["connect_timeout"] == "5"


def test_migration_runner_connection_carries_dead_peer_detection():
    """The runner holds a session pg_advisory_lock while migrating."""
    from code_indexer.server.storage.postgres.migrations.runner import (
        MigrationRunner,
    )

    with patch("psycopg.connect", return_value=MagicMock()) as mock_connect:
        MigrationRunner(_OPERATOR_DSN)

    _assert_dead_peer_detection(_effective_params(mock_connect.call_args))


# ---------------------------------------------------------------------------
# Live PostgreSQL: the GUCs are really in force on the server's backend.
# Needs a TCP DSN -- over a Unix socket PostgreSQL ignores tcp_keepalives_*
# and always reports 0.
# ---------------------------------------------------------------------------


def _live_tcp_dsn() -> str:
    import os

    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN not set")
    host = str(dict(conninfo_to_dict(dsn)).get("host") or "")
    if not host or host.startswith("/"):
        pytest.skip("TEST_POSTGRES_DSN must be a TCP DSN for tcp_* GUCs")
    return dsn


def _server_gucs_in_effect(conn: Any) -> Dict[str, str]:
    """Raw pg_settings values (base units: s for keepalives, ms for timeout)."""
    rows = conn.execute(
        "SELECT name, setting FROM pg_settings WHERE name = ANY(%s)",
        (list(_SERVER_GUCS),),
    ).fetchall()
    return {str(name): str(setting) for name, setting in rows}


_OPERATOR_KEEPALIVES_IDLE = "45"


def test_live_server_applies_dead_client_gucs_to_session():
    from code_indexer.server.storage.postgres.dead_peer_detection import (
        apply_dead_peer_detection,
    )

    dsn = _live_tcp_dsn()
    with psycopg.connect(dsn) as plain:
        control = _server_gucs_in_effect(plain)
    # Control: a plain session runs on the server default, not our values.
    assert control["tcp_keepalives_idle"] != _SERVER_GUCS["tcp_keepalives_idle"]

    with psycopg.connect(apply_dead_peer_detection(dsn)) as conn:
        assert _server_gucs_in_effect(conn) == _SERVER_GUCS


def test_live_pooled_connection_has_dead_client_gucs():
    from code_indexer.server.storage.postgres.connection_pool import (
        ConnectionPool,
    )

    pool = ConnectionPool(_live_tcp_dsn(), min_size=0, max_size=1, name="t2005")
    try:
        with pool.connection() as conn:
            assert _server_gucs_in_effect(conn) == _SERVER_GUCS
    finally:
        pool.close()


def test_live_operator_options_guc_wins():
    from code_indexer.server.storage.postgres.dead_peer_detection import (
        apply_dead_peer_detection,
    )

    params = dict(conninfo_to_dict(_live_tcp_dsn()))
    params["options"] = f"-c tcp_keepalives_idle={_OPERATOR_KEEPALIVES_IDLE}"
    with psycopg.connect(apply_dead_peer_detection(make_conninfo("", **params))) as c:
        shown = _server_gucs_in_effect(c)
    assert shown == {**_SERVER_GUCS, "tcp_keepalives_idle": _OPERATOR_KEEPALIVES_IDLE}
