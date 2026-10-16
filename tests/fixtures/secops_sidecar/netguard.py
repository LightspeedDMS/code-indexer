"""Outbound-connection guard for the sidecar process (sys.addaudithook).

The in-memory sidecar never needs an outbound network connection.  Any
``socket.connect`` to a non-loopback address is counted (exposed by
/_control/counts so tests can assert zero) and refused.  Unix-domain sockets
(local IPC) and loopback addresses are not outbound.  Audit hooks cannot be
removed once installed, which is the point.
"""

from __future__ import annotations

import ipaddress
import socket
import sys
import threading
from typing import Any, Tuple

_lock = threading.Lock()
_outbound_attempts = 0
_installed = False


def _is_loopback(host: Any) -> bool:
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False  # a hostname: never resolved here, treated as outbound
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(address.is_loopback or (mapped is not None and mapped.is_loopback))


def _hook(event: str, args: Tuple[Any, ...]) -> None:
    global _outbound_attempts
    if event != "socket.connect" or len(args) < 2:
        return
    sock, address = args[0], args[1]
    if getattr(sock, "family", None) == getattr(socket, "AF_UNIX", object()):
        return  # local IPC, not a network connection
    host = address[0] if isinstance(address, tuple) and address else address
    if _is_loopback(host):
        return
    with _lock:
        _outbound_attempts += 1
    raise PermissionError("secops sidecar: outbound network connections are refused")


def install() -> None:
    """Install the guard once per process."""
    global _installed
    with _lock:
        if _installed:
            return
        _installed = True
    sys.addaudithook(_hook)


def outbound_connects() -> int:
    """Number of refused non-loopback connection attempts since start."""
    with _lock:
        return _outbound_attempts
