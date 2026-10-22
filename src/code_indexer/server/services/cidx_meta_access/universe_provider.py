"""The process's alias universe, shared by every door that projects
catalog documents.

One ``AliasUniverse`` per registry object (the cluster-aware
``backend_registry.global_repos``), so its TTL cache is shared by all
requests of the process. It only widens prose redaction, never an allow
decision (see ``alias_universe``).
"""

from __future__ import annotations

import threading
from typing import Any, Optional, Tuple

from .alias_universe import AliasUniverse

_lock = threading.Lock()
_current: Optional[Tuple[Any, AliasUniverse]] = None


def registry_of(app_state: Any) -> Any:
    """The global-repos registry on the server state, or None."""
    backend_registry = getattr(app_state, "backend_registry", None)
    return getattr(backend_registry, "global_repos", None)


def process_alias_universe(registry: Any) -> AliasUniverse:
    """The universe over *registry*; raises without one (a restricted
    projection never runs with an unknown universe)."""
    global _current
    if registry is None:
        raise RuntimeError("alias registry unavailable for catalog projection")
    with _lock:
        if _current is None or _current[0] is not registry:
            _current = (registry, AliasUniverse(registry))
        return _current[1]
