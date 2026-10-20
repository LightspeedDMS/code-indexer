"""One request's view of the caller's activations.

A door that both checks whether a request may serve the catalog and then
reads the repository resolves each activated alias once: the directory of
``(username, alias)`` is looked up on first use and reused by every later
check and reader of the same request. Never shared across requests, so a
change to an activation is seen by the next request.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


class RequestActivations:
    """The activation lookups a door makes for ONE request."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager
        self._paths: Dict[Tuple[str, str], Optional[str]] = {}

    @property
    def data_dir(self) -> Any:
        return getattr(self._manager, "data_dir", None)

    def get_repository(self, username: str, alias: str, touch: bool = False) -> Any:
        return self._manager.get_repository(username, alias, touch=touch)

    def get_activated_repo_path(self, username: str, alias: str) -> Optional[str]:
        key = (username, alias)
        if key not in self._paths:
            self._paths[key] = self._manager.get_activated_repo_path(username, alias)
        return self._paths[key]


def request_activations(manager: Optional[Any]) -> Optional[RequestActivations]:
    """*manager*'s lookups for one request; None without a manager."""
    return None if manager is None else RequestActivations(manager)
