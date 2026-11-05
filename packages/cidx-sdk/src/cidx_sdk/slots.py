"""A process-wide slot holding the one implementation of an interface.

Used by libraries that receive a platform service without depending on the
code that provides it (the core's X-Ray and SCIP packages today): the host
installs its implementation once at wiring time, and a library reads the slot
when it needs the service. An empty slot means "no host in this process" and
each consumer decides its own no-host answer.

A slot is process wiring, set once and identical on every node; it is never a
store for request state.
"""

from __future__ import annotations

import threading
from typing import Generic, Optional, TypeVar

_T = TypeVar("_T")


class CapabilitySlot(Generic[_T]):
    """The one implementation of a capability installed in this process."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._implementation: Optional[_T] = None
        self._lock = threading.Lock()

    def install(self, implementation: _T) -> None:
        """Install *implementation*, replacing any earlier one."""
        with self._lock:
            self._implementation = implementation

    def uninstall(self) -> None:
        """Empty the slot (tests)."""
        with self._lock:
            self._implementation = None

    def get(self) -> Optional[_T]:
        """The installed implementation, or None when no host installed one."""
        return self._implementation
