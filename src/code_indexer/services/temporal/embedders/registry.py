"""Registry/factory for pluggable TemporalEmbedder adapters (Story #1290).

Single wiring point: a new embedder is added by implementing
``TemporalEmbedder`` and calling ``register_embedder(name, factory)`` --
zero core indexer/recall change (Epic #1289 primary objective).
"""

import threading
from typing import Any, Callable, Dict, List

from .base import TemporalEmbedder

# Module-level registry is process-local factory metadata only (constructor
# callables), NOT cross-request runtime state -- safe under the cluster-aware
# state rule (CLAUDE.md), which governs per-node RAM used to answer requests,
# not process-startup adapter registration.
_REGISTRY: Dict[str, Callable[[Any], TemporalEmbedder]] = {}
_REGISTRY_LOCK = threading.Lock()


def register_embedder(name: str, factory: Callable[[Any], TemporalEmbedder]) -> None:
    """Register a TemporalEmbedder factory under `name`.

    Args:
        name: Config-facing embedder identifier (e.g. "voyage-context-4").
        factory: Callable taking a config object and returning a
            TemporalEmbedder instance.
    """
    with _REGISTRY_LOCK:
        _REGISTRY[name] = factory


def unregister_embedder_for_tests(name: str) -> None:
    """Remove a registered embedder (test-only cleanup hook)."""
    with _REGISTRY_LOCK:
        _REGISTRY.pop(name, None)


def registered_embedder_names() -> List[str]:
    """Return the names of all currently-registered embedders."""
    with _REGISTRY_LOCK:
        return list(_REGISTRY.keys())


def create_embedder(name: str, config: Any) -> TemporalEmbedder:
    """Instantiate the TemporalEmbedder registered under `name`.

    Args:
        name: Embedder identifier to look up.
        config: Config object passed through to the registered factory.

    Raises:
        KeyError: If no embedder is registered under `name`.
    """
    with _REGISTRY_LOCK:
        factory = _REGISTRY.get(name)
    if factory is None:
        raise KeyError(
            f"Unknown temporal embedder {name!r}. "
            f"Registered embedders: {sorted(_REGISTRY.keys())}"
        )
    return factory(config)
