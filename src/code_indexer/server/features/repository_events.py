"""``repository_events``: repository lifecycle events delivered to features.

The core announces what it did to a repository -- files written or deleted,
a git operation that may have changed any file, a golden repository removed
or renamed -- and a feature that keeps state derived from repository content
(the wiki's render cache and view counts) stays in step without the core
knowing it exists.

Listeners are process wiring, subscribed by a feature's ``on_startup`` on
every node and dropped by ``on_shutdown``: never request state (the
Cluster-Aware State rule). An event is announced on the node that made the
change, after it was made; what a listener does with it must therefore go to
a shared store, and the change itself is already visible to every node.

Delivery never fails the operation that announced it: a listener that raises
is logged at WARNING with the exception type and the next one runs.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, List, Optional

from cidx_sdk.capability_types import RepositoryEventListener

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_listeners: List[RepositoryEventListener] = []


def subscribe(listener: RepositoryEventListener) -> Callable[[], None]:
    """Deliver events to *listener* until the returned callable is called."""
    with _lock:
        _listeners.append(listener)

    def _unsubscribe() -> None:
        with _lock:
            if listener in _listeners:
                _listeners.remove(listener)

    return _unsubscribe


def subscriber_count() -> int:
    """How many listeners are subscribed in this process."""
    with _lock:
        return len(_listeners)


def _deliver(event: str, call: Callable[[RepositoryEventListener], None]) -> int:
    with _lock:
        listeners = list(_listeners)
    for listener in listeners:
        try:
            call(listener)
        except Exception as exc:  # a listener never fails the operation
            logger.warning(
                "repository event %s: listener %s failed (%s)",
                event,
                type(listener).__name__,
                type(exc).__name__,
            )
    return len(listeners)


def publish_content_changed(alias: str, path: Optional[str] = None) -> int:
    """Files of *alias* changed (*path*, or None for any file). Returns the
    number of listeners told."""
    return _deliver("content_changed", lambda li: li.content_changed(alias, path))


def publish_repository_removed(alias: str) -> int:
    """The golden repository *alias* was removed."""
    return _deliver("repository_removed", lambda li: li.repository_removed(alias))


def publish_repository_renamed(old_alias: str, new_alias: str) -> int:
    """The golden repository *old_alias* is now *new_alias*."""
    return _deliver(
        "repository_renamed", lambda li: li.repository_renamed(old_alias, new_alias)
    )


class CoreRepositoryEventsCapability:
    """``repository_events`` for one feature."""

    def subscribe(self, listener: RepositoryEventListener) -> Callable[[], None]:
        return subscribe(listener)
