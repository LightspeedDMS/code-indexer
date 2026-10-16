"""Bound how long a dead PostgreSQL peer keeps its session alive (Bug #2005).

When a cluster node dies without closing its sockets (kernel crash, power
loss, network partition), the PostgreSQL SERVER keeps that node's backends
-- and every session-level lock they hold, e.g. the leader advisory lock --
until the server's own end of the TCP socket detects the dead peer. With
the server defaults (``tcp_keepalives_idle = 0`` -> OS default of 7200s)
that takes about two hours.

libpq's client parameters (``keepalives*``, ``tcp_user_timeout``) only tune
the CLIENT's socket: they let this node notice a dead server, not the other
way round. The server's socket is tuned by the per-session GUCs
``tcp_keepalives_idle`` / ``tcp_keepalives_interval`` /
``tcp_keepalives_count`` / ``tcp_user_timeout``, whose assign hooks apply
them to the backend's socket with ``setsockopt``. They are user-settable,
so a client can request them for its own session through the libpq
``options`` startup parameter (``-c name=value``). That is what
:func:`apply_dead_peer_detection` adds, alongside the client-side
parameters, so both ends of every long-lived connection detect a dead peer
in about a minute.

Operator-provided values in ``postgres_dsn`` always win: a libpq parameter
already present is kept as-is, and a server GUC already set in the
operator's ``options`` is not appended again.
"""

from __future__ import annotations

import os
from types import MappingProxyType
from typing import Dict, List, Mapping

# Server end: the backend drops a silent client after idle + interval * count
# = 30 + 10 * 3 = 60s; tcp_user_timeout bounds unacknowledged sends to 60s.
SERVER_DEAD_CLIENT_GUCS: Mapping[str, str] = MappingProxyType(
    {
        "tcp_keepalives_idle": "30",
        "tcp_keepalives_interval": "10",
        "tcp_keepalives_count": "3",
        "tcp_user_timeout": "60000",
    }
)

# Client end, general connections (pooled, lock stores, migrations).
CLIENT_KEEPALIVE_PARAMS: Mapping[str, str] = MappingProxyType(
    {
        "keepalives": "1",
        "keepalives_idle": "30",
        "keepalives_interval": "10",
        "keepalives_count": "3",
        "tcp_user_timeout": "60000",
    }
)

# Client end, dedicated leader advisory-lock connection only: a tighter 30s
# client tcp_user_timeout so a partitioned leader tends to fail its 10s
# ping and step down quickly. In a blackhole test the leader stepped down at
# ~40s and the server released the lock at ~60s -- an OBSERVED, approximate
# ordering, not a guarantee: tcp_user_timeout bounds only unacknowledged sent
# data (not a stalled response), and the ping has no response deadline.
LEADER_CLIENT_KEEPALIVE_PARAMS: Mapping[str, str] = MappingProxyType(
    {
        "keepalives": "1",
        "keepalives_idle": "10",
        "keepalives_interval": "5",
        "keepalives_count": "3",
        "tcp_user_timeout": "30000",
    }
)


def _split_libpq_options(options: str) -> List[str]:
    """Split ``options`` into arguments exactly like PostgreSQL's
    ``pg_split_opts``: unescaped whitespace separates arguments and a
    backslash makes the next character literal (the backslash is dropped)."""
    args: List[str] = []
    current: List[str] = []
    in_arg = escaped = False
    for ch in options:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = in_arg = True
        elif ch.isspace():
            if in_arg:
                args.append("".join(current))
                current, in_arg = [], False
        else:
            current.append(ch)
            in_arg = True
    if in_arg:
        args.append("".join(current))
    return args


def _options_gucs(options: str) -> Dict[str, str]:
    """GUCs set in ``options`` via ``-c name=value``, ``-cname=value`` or
    ``--name=value``, keyed by the name as PostgreSQL resolves it
    (case-insensitive, '-' == '_'). A later setting wins, as on the server."""
    args = _split_libpq_options(options)
    gucs: Dict[str, str] = {}
    i = 0
    while i < len(args):
        arg = args[i]
        setting = None
        if arg == "-c" and i + 1 < len(args):
            i += 1
            setting = args[i]
        elif arg.startswith("--") or (arg.startswith("-c") and len(arg) > 2):
            setting = arg[2:]
        i += 1
        if setting is not None and "=" in setting:
            name, _, value = setting.partition("=")
            gucs[name.replace("-", "_").lower()] = value
    return gucs


def _merge_server_gucs(existing_options: str) -> str:
    """Append ``-c name=value`` for each server GUC the operator did not set.

    The operator's ``options`` string is kept verbatim (never stripped:
    an escaped trailing space belongs to the operator's last value).
    """
    already_set = _options_gucs(existing_options)
    flags = [
        f"-c {name}={value}"
        for name, value in SERVER_DEAD_CLIENT_GUCS.items()
        if name not in already_set
    ]
    if not _split_libpq_options(existing_options):
        return " ".join(flags)
    # An odd trailing backslash escapes nothing (PostgreSQL drops it); kept,
    # it would escape our separator and turn our first -c into a stray arg.
    trailing = len(existing_options) - len(existing_options.rstrip("\\"))
    if trailing % 2:
        existing_options = existing_options[:-1]
    return " ".join([existing_options, *flags])


def apply_dead_peer_detection(
    conninfo: str,
    client_params: Mapping[str, str] = CLIENT_KEEPALIVE_PARAMS,
) -> str:
    """Return ``conninfo`` with dead-peer detection for BOTH socket ends.

    Args:
        conninfo: Operator DSN (URI or key=value form).
        client_params: libpq client keepalive / tcp_user_timeout defaults.

    Returns:
        A key=value conninfo string. Parameters already present in
        ``conninfo`` are never overridden.

    Raises:
        psycopg.ProgrammingError: ``conninfo`` is not a valid DSN.
    """
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = dict(conninfo_to_dict(conninfo))
    for name, value in client_params.items():
        params.setdefault(name, value)
    # An explicit `options` replaces libpq's PGOPTIONS default, so when the
    # DSN has none, start from PGOPTIONS exactly as libpq would have.
    existing = (
        params["options"] if "options" in params else os.environ.get("PGOPTIONS", "")
    )
    params["options"] = _merge_server_gucs(str(existing or ""))
    return str(make_conninfo("", **params))


# Zero, with or without a time unit: for every managed setting this means
# "off" (keepalives) or "use the OS default" (up to ~2h of TCP keepalive).
_ZERO_UNITS = ("", "us", "ms", "s", "min", "h", "d")


def _is_zero(value: str) -> bool:
    """True for a zero setting, with or without a PostgreSQL time unit and
    in integer or float form ("0", "0s", "0.0", "0us", "0h"...)."""
    text = value.strip().lower()
    number = text.rstrip("abcdefghijklmnopqrstuvwxyz")
    if not number or text[len(number) :] not in _ZERO_UNITS:
        return False
    try:
        return float(number) == 0.0
    except ValueError:
        return False


def zeroed_dead_peer_settings(
    conninfo: str,
    client_params: Mapping[str, str] = CLIENT_KEEPALIVE_PARAMS,
) -> List[str]:
    """Managed settings the operator's ``conninfo`` sets to zero.

    Operator values win in :func:`apply_dead_peer_detection`, so a zero here
    switches dead-peer detection off (or back to the OS default) on that
    socket end. Client libpq parameters are reported by bare name (e.g.
    ``keepalives``); server GUCs as ``options -c <guc>``, since both ends
    have a ``tcp_user_timeout`` and are checked separately. Sorted; empty
    when detection is fully in force.
    """
    from psycopg.conninfo import conninfo_to_dict

    final = dict(conninfo_to_dict(apply_dead_peer_detection(conninfo, client_params)))
    server = _options_gucs(str(final.get("options") or ""))
    zeroed = [n for n in client_params if _is_zero(str(final.get(n) or ""))]
    zeroed += [
        f"options -c {n}"
        for n in SERVER_DEAD_CLIENT_GUCS
        if _is_zero(server.get(n, ""))
    ]
    return sorted(zeroed)
