"""Origin marker carried by payload-cache handles.

While a request is bound (``bind_handles``), every handle the payload cache
mints carries the binding as a suffix: ``<base>.cmv1.<binding>``. The
binding is opaque here (the catalog visibility layer writes the caller's
visibility hash and re-checks it on read). The marker lives in the key, so
every node of a cluster can read it with no per-process state and no
schema change; the suffix keeps prefix discriminators (e.g. ``xray-pv1-``)
intact. An unbound request mints plain handles.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Optional

HANDLE_BINDING_MARKER = ".cmv1."

_BINDING: ContextVar[Optional[str]] = ContextVar("payload_handle_binding", default=None)


def bind_handles(binding: Optional[str]) -> "Token[Optional[str]]":
    """Bind handles minted in this context to *binding* (None = unbound)."""
    if binding is not None and (not binding or HANDLE_BINDING_MARKER in binding):
        raise ValueError("a handle binding is a non-empty string without the marker")
    return _BINDING.set(binding)


def reset_handle_binding(token: "Optional[Token[Optional[str]]]") -> None:
    """Restore the binding in force before ``bind_handles`` (None: no-op)."""
    if token is not None:
        _BINDING.reset(token)


def mint_handle(base: str) -> str:
    """*base* plus the current binding, if any."""
    binding = _BINDING.get()
    return base if binding is None else f"{base}{HANDLE_BINDING_MARKER}{binding}"


def handle_binding(handle: str) -> Optional[str]:
    """The binding a handle was minted under, or None for a plain handle."""
    _, marker, binding = handle.rpartition(HANDLE_BINDING_MARKER)
    return binding if marker and binding else None
