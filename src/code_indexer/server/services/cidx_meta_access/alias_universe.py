"""Every golden repository alias known to the registry, for prose redaction.

The universe only widens redaction (names that must not appear in prose
for callers who cannot see them); it is never consulted for an allow
decision. It comes from the cluster-aware registry (PostgreSQL in cluster
mode), cached per process for a short TTL.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, FrozenSet, Mapping, Optional, Protocol

from ..repo_access_guard import normalize_repo_alias

ALIAS_UNIVERSE_TTL_SECONDS = 60.0


class GlobalReposRegistry(Protocol):
    def list_repos(self) -> Mapping[str, Mapping[str, Any]]: ...


@dataclass(frozen=True)
class UniverseSnapshot:
    """``generation`` changes whenever ``names`` changes; ``digest`` names the
    content (projection cache keys use it)."""

    generation: int
    names: FrozenSet[str]
    digest: str


def _digest(names: FrozenSet[str]) -> str:
    return hashlib.sha256("\n".join(sorted(names)).encode("utf-8")).hexdigest()


def _names_from(repos: Mapping[str, Mapping[str, Any]]) -> FrozenSet[str]:
    names = set()
    for alias, details in repos.items():
        if isinstance(alias, str) and alias:
            names.add(normalize_repo_alias(alias))
        repo_name = details.get("repo_name") if isinstance(details, Mapping) else None
        if isinstance(repo_name, str) and repo_name:
            names.add(normalize_repo_alias(repo_name))
    return frozenset(names)


class AliasUniverse:
    """Per-process, TTL-cached view of the registry's alias names."""

    def __init__(
        self,
        registry: GlobalReposRegistry,
        *,
        ttl_seconds: float = ALIAS_UNIVERSE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._registry = registry
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._snapshot: Optional[UniverseSnapshot] = None
        self._loaded_at = 0.0

    def snapshot(self) -> UniverseSnapshot:
        """The current names; re-read from the registry once the TTL expired."""
        with self._lock:
            now = self._clock()
            if self._snapshot is not None and now - self._loaded_at < self._ttl:
                return self._snapshot
            names = _names_from(self._registry.list_repos())
            previous = self._snapshot
            if previous is not None and previous.names == names:
                generation = previous.generation
            else:
                generation = (previous.generation if previous else 0) + 1
            self._snapshot = UniverseSnapshot(
                generation=generation, names=names, digest=_digest(names)
            )
            self._loaded_at = now
            return self._snapshot
