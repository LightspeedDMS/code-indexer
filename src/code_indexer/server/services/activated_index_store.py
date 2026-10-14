"""An activated copy's index in the IndexStore, so any pod can serve it.

A user's activated copy lives on workspace-mcp; its INDEX is pod state, built
by whichever pod activated or reindexed it. Under ``index_store_backend:
delegated`` that index is published to the IndexStore and the version recorded
in the copy's registry entry (``index_store_alias`` / ``index_version``), the
same mechanism as golden repositories (``index_materializer.py``):

* activation and every reindex PUBLISH the copy's index;
* a query, a status read or a reindex on ANY pod first makes this pod hold the
  recorded version (``ensure_current_index``: checked out, or swapped for a
  newer one);
* deactivation DELETES the alias from the IndexStore.

Isolation: the store alias is derived HERE from the registry's
``(username, user_alias)`` (``activated_index_store_alias``) -- one namespace
per user and copy, never a golden alias; a registry entry naming any other
alias is refused. Callers have already checked the registry for THIS user.

Local store: every method is a no-op (``publish`` returns no fields), so the
default deployment is unchanged.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Union

from code_indexer.server.services.index_materializer import (
    PublishedIndexUnavailableError,
    ensure_current_index,
    publish_index,
)
from code_indexer.utils.index_store import (
    INDEX_STORE_ALIAS_FIELD,
    INDEX_VERSION_FIELD,
    IndexStore,
    activated_index_store_alias,
    is_activated_store_alias,
)

logger = logging.getLogger(__name__)

StoreProvider = Callable[[], IndexStore]


def _server_store() -> IndexStore:
    from code_indexer.server.startup.index_store_wiring import server_index_store

    return server_index_store()


class ActivatedIndexStore:
    """Publish / serve / delete one activated copy's index (facade)."""

    def __init__(self, store_provider: StoreProvider = _server_store) -> None:
        self._store_provider = store_provider

    def is_delegated(self) -> bool:
        return not self._store_provider().is_local

    def _checked_alias(
        self, username: str, user_alias: str, metadata: Optional[Mapping[str, Any]]
    ) -> str:
        alias = activated_index_store_alias(username, user_alias)
        recorded = (metadata or {}).get(INDEX_STORE_ALIAS_FIELD)
        if recorded is not None and recorded != alias:
            raise PublishedIndexUnavailableError(
                f"The registry entry of '{user_alias}' names IndexStore alias "
                f"{recorded!r}, not this copy's own; refusing to use it"
            )
        if not is_activated_store_alias(
            alias
        ):  # defensive: the derivation's own contract
            raise PublishedIndexUnavailableError(f"{alias!r} is not an activated alias")
        return alias

    def serve(
        self,
        username: str,
        user_alias: str,
        repo_path: Union[str, Path],
        metadata: Optional[Mapping[str, Any]],
    ) -> None:
        """Make this pod hold the version the registry records for the copy."""
        store = self._store_provider()
        if store.is_local:
            return
        self._checked_alias(username, user_alias, metadata)
        ensure_current_index(repo_path, metadata, store=store)

    def publish(
        self,
        username: str,
        user_alias: str,
        repo_path: Union[str, Path],
        metadata: Optional[Mapping[str, Any]],
        *,
        fresh: bool = False,
    ) -> Dict[str, str]:
        """Publish the index this pod holds for the copy; the registry fields.

        ``fresh`` (activation): the index was just seeded, so it is published
        over whatever the store holds. Otherwise it was built from the version
        ``metadata`` records and is published on top of exactly that one: a
        publish by another pod in between is refused (compare-and-swap).
        Empty under the local store.
        """
        store = self._store_provider()
        if store.is_local:
            return {}
        alias = self._checked_alias(username, user_alias, metadata)
        if fresh:
            current_version_as = getattr(store, "current_version_as", None)
            if current_version_as is None:
                raise PublishedIndexUnavailableError(
                    f"IndexStore {type(store).__name__} cannot read a version"
                )
            base: Optional[str] = current_version_as(alias)
        else:
            recorded = (metadata or {}).get(INDEX_VERSION_FIELD)
            base = recorded if isinstance(recorded, str) and recorded else None
        fields = publish_index(repo_path, alias, store=store, base_version=base)
        logger.info(
            "Published the index of %s/%s as %s",
            username,
            user_alias,
            fields[INDEX_VERSION_FIELD],
        )
        return fields

    def delete(self, username: str, user_alias: str) -> None:
        """Remove every published version of the copy's index. Raises on failure."""
        store = self._store_provider()
        if store.is_local:
            return
        alias = self._checked_alias(username, user_alias, None)
        delete_as = getattr(store, "delete_as", None)
        if delete_as is None:
            raise PublishedIndexUnavailableError(
                f"IndexStore {type(store).__name__} cannot delete an alias"
            )
        deleted = delete_as(alias)
        logger.info(
            "Deleted %s published index version(s) of %s/%s",
            deleted,
            username,
            user_alias,
        )


def serve_activated_index(
    activated_repo_manager: Any,
    username: str,
    user_alias: str,
    *,
    store_provider: StoreProvider = _server_store,
) -> None:
    """For a reader about to open a copy's index: under a delegated IndexStore,
    ``activated_repo_manager.serve_index_here`` (registry gate for THIS user,
    then the recorded version on this pod; raises on failure). Under the local
    store nothing is read or called -- today's path."""
    if store_provider().is_local:
        return
    activated_repo_manager.serve_index_here(username, user_alias)
