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

import hashlib
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Union

from code_indexer.server.services.index_materializer import (
    CHECKED_OUT_MARKER,
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
    published_index_fields,
)

#: Registry field of a COMPOSITE copy: member name -> its published index
#: version (the proxy config's own version is ``index_version``).
MEMBER_INDEX_VERSIONS_FIELD = "member_index_versions"

#: Present beside ``CHECKED_OUT_MARKER``: a SHA-256 digest of the index
#: directory's content at the time it was published.  On a partial-publish
#: retry the digest is compared to the current on-disk content; a mismatch
#: (index rebuilt in between) triggers a re-publish instead of adopting the
#: stale version the marker still names.
CONTENT_DIGEST_MARKER = ".cidx-index-content-digest"

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

    def _member_parts(
        self,
        username: str,
        user_alias: str,
        repo_path: Union[str, Path],
        metadata: Optional[Mapping[str, Any]],
    ) -> List[Tuple[str, str, Path, Optional[str]]]:
        """A composite's members: ``(member, store alias, pod path, recorded
        version)``. [] for a single copy. Members come from the REGISTRY
        (``discovered_repos``), never from this pod's disk, and only plain
        names (one path component) are used."""
        fields = metadata or {}
        if not fields.get("is_composite"):
            return []
        from code_indexer.server.services.composite_content import (
            plain_member_names,
        )

        recorded = fields.get(MEMBER_INDEX_VERSIONS_FIELD) or {}
        if not isinstance(recorded, Mapping):
            raise PublishedIndexUnavailableError(
                f"The registry entry of '{user_alias}' records unreadable member "
                "index versions"
            )
        parts = []
        for member in plain_member_names(
            fields.get("discovered_repos") or [], repo_path
        ):
            version = recorded.get(member)
            parts.append(
                (
                    member,
                    activated_index_store_alias(username, f"{user_alias}/{member}"),
                    Path(repo_path) / member,
                    version if isinstance(version, str) and version else None,
                )
            )
        return parts

    def serve(
        self,
        username: str,
        user_alias: str,
        repo_path: Union[str, Path],
        metadata: Optional[Mapping[str, Any]],
    ) -> None:
        """Make this pod hold the version the registry records for the copy --
        for a composite, its proxy config AND every member's index."""
        store = self._store_provider()
        if store.is_local:
            return
        self._checked_alias(username, user_alias, metadata)
        members = self._member_parts(username, user_alias, repo_path, metadata)
        # A composite's own state is its proxy config: no index directory.
        ensure_current_index(
            repo_path, metadata, store=store, require_index=not members
        )
        for _member, alias, path, version in members:
            document = published_index_fields(alias, version) if version else None
            ensure_current_index(path, document, store=store)

    def publish(
        self,
        username: str,
        user_alias: str,
        repo_path: Union[str, Path],
        metadata: Optional[Mapping[str, Any]],
        *,
        fresh: bool = False,
    ) -> Dict[str, Any]:
        """Publish the index this pod holds for the copy; the registry fields.

        ``fresh`` (activation): the index was just seeded, so it is published
        over whatever the store holds. Otherwise it was built from the version
        ``metadata`` records and is published on top of exactly that one: a
        publish by another pod in between is refused (compare-and-swap).
        A composite publishes its proxy config and each member's index
        (``member_index_versions``). Empty under the local store.
        """
        store = self._store_provider()
        if store.is_local:
            return {}
        alias = self._checked_alias(username, user_alias, metadata)
        recorded = (metadata or {}).get(INDEX_VERSION_FIELD)
        base = self._base(store, alias, recorded, fresh)
        fields: Dict[str, Any] = self._publish_or_adopt(
            store, repo_path, alias, base, fresh
        )
        members = self._member_parts(username, user_alias, repo_path, metadata)
        if members:
            versions: Dict[str, str] = {}
            for member, member_alias, path, member_recorded in members:
                member_base = self._base(store, member_alias, member_recorded, fresh)
                published = self._publish_or_adopt(
                    store, path, member_alias, member_base, fresh
                )
                versions[member] = published[INDEX_VERSION_FIELD]
            fields[MEMBER_INDEX_VERSIONS_FIELD] = versions
        logger.info(
            "Published the index of %s/%s as %s",
            username,
            user_alias,
            fields[INDEX_VERSION_FIELD],
        )
        return fields

    @staticmethod
    def _content_digest(store: IndexStore, target_path: Union[str, Path]) -> str:
        """SHA-256 digest of the index directory's content (file paths + data).

        Deterministic: files are sorted by relative path.  Used to detect
        whether the on-disk index changed since the last publish.
        """
        index_root = store.index_root(Path(target_path))
        h = hashlib.sha256()
        for p in sorted(index_root.rglob("*")):
            if p.is_file():
                rel = str(p.relative_to(index_root))
                h.update(rel.encode("utf-8"))
                h.update(b"\0")
                with open(p, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        h.update(chunk)
                h.update(b"\0")
        return h.hexdigest()

    @staticmethod
    def _write_content_digest(
        store: IndexStore, target_path: Union[str, Path]
    ) -> None:
        """Record the current index content's digest beside the marker."""
        digest = ActivatedIndexStore._content_digest(store, target_path)
        (Path(target_path) / CONTENT_DIGEST_MARKER).write_text(digest)

    @staticmethod
    def _publish_or_adopt(
        store: IndexStore,
        target_path: Union[str, Path],
        store_alias: str,
        base_version: Optional[str],
        fresh: bool,
    ) -> Dict[str, str]:
        """Publish the index at *target_path*, or re-publish on top of the
        store's current version if this pod already pushed it (partial-publish
        retry).

        A composite publishes its proxy config and each member sequentially.
        If member *k* fails after members 1..k-1 succeeded, the store is at
        N+1 but the registry still records N. Every retry of the already-
        published members hits a CAS conflict (``RuntimeError`` /
        ``PublishedIndexUnavailableError``). When the conflict is on a non-
        fresh publish, and this pod's ``CHECKED_OUT_MARKER`` matches the
        store's current version for the alias, a re-publish on top of that
        version is attempted. This re-uploads the on-disk content (which may
        have been rebuilt by a reindex since the original publish) rather
        than silently adopting the store's version.

        Only genuine CAS conflicts (store advanced past ``base_version``)
        are eligible. A transient error where the store is still at
        ``base_version`` is re-raised so the caller fails loud.

        A content digest (SHA-256 of the index directory) is recorded next
        to the marker after every successful publish. When a digest is
        present and matches the current on-disk content, the prior version
        is adopted (no re-upload needed). When the digest is absent or
        differs, a re-publish uploads whatever is on disk now.
        """
        try:
            result = dict(
                publish_index(
                    target_path, store_alias, store=store, base_version=base_version
                )
            )
            ActivatedIndexStore._write_content_digest(store, target_path)
            return result
        except (RuntimeError, PublishedIndexUnavailableError):
            if fresh:
                raise
            current_version_as = getattr(store, "current_version_as", None)
            if current_version_as is None:
                raise
            current = current_version_as(store_alias)
            if current is not None and current == base_version:
                raise
            marker_path = Path(target_path) / CHECKED_OUT_MARKER
            if not marker_path.is_file():
                raise
            marker = marker_path.read_text().strip()
            if current and marker == current:
                # This pod published ``current`` in a prior call that
                # failed mid-composite.  Check whether the on-disk index
                # has changed since that publish (a reindex in between).
                digest_path = Path(target_path) / CONTENT_DIGEST_MARKER
                if digest_path.is_file():
                    stored = digest_path.read_text().strip()
                    on_disk = ActivatedIndexStore._content_digest(
                        store, target_path
                    )
                    if stored == on_disk:
                        # Content unchanged -- adopt the prior version
                        # without a redundant re-upload.
                        logger.info(
                            "Adopting already-published version %s of %s "
                            "(partial-publish retry; content unchanged)",
                            current,
                            store_alias,
                        )
                        return dict(
                            published_index_fields(store_alias, current)
                        )
                # Content changed, or no digest recorded (pre-digest
                # code path) -- re-publish so the store gets the current
                # on-disk state.
                logger.info(
                    "Re-publishing %s on top of %s (partial-publish retry; "
                    "content changed or no digest recorded)",
                    store_alias,
                    current,
                )
                result = dict(
                    publish_index(
                        target_path,
                        store_alias,
                        store=store,
                        base_version=current,
                    )
                )
                ActivatedIndexStore._write_content_digest(store, target_path)
                return result
            raise

    @staticmethod
    def _base(
        store: IndexStore, alias: str, recorded: Any, fresh: bool
    ) -> Optional[str]:
        """The version a publish builds on: the store's current one for a fresh
        seed, else exactly the recorded one (None: the first)."""
        if fresh:
            current_version_as = getattr(store, "current_version_as", None)
            if current_version_as is None:
                raise PublishedIndexUnavailableError(
                    f"IndexStore {type(store).__name__} cannot read a version"
                )
            current: Optional[str] = current_version_as(alias)
            return current
        return recorded if isinstance(recorded, str) and recorded else None

    @staticmethod
    def _delete_member_aliases(
        username: str,
        user_alias: str,
        metadata: Optional[Mapping[str, Any]],
    ) -> List[str]:
        """Store aliases of a composite's members for *delete* -- tolerant of
        corrupt ``member_index_versions``.

        Uses ``discovered_repos`` (the canonical member list) via
        ``plain_member_names`` without reading ``member_index_versions``,
        so a composite whose registry metadata is partially corrupt can still
        be deactivated. When *metadata* is None (orphan cleanup), no member
        aliases are returned and only the composite's own alias is deleted.
        """
        fields = metadata or {}
        if not fields.get("is_composite"):
            return []
        from code_indexer.server.services.composite_content import (
            plain_member_names,
        )

        return [
            activated_index_store_alias(username, f"{user_alias}/{member}")
            for member in plain_member_names(fields.get("discovered_repos") or [], ".")
        ]

    def delete(
        self,
        username: str,
        user_alias: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Remove every published version of the copy's index (a composite's
        members' too, as its registry entry names them). Raises on failure.

        Unlike *_member_parts*, member aliases are derived from
        ``discovered_repos`` without validating ``member_index_versions``, so
        a composite with corrupt metadata can still be deactivated. When
        *metadata* is None (orphan cleanup), only the composite's own alias
        is deleted.
        """
        store = self._store_provider()
        if store.is_local:
            return
        alias = self._checked_alias(username, user_alias, None)
        delete_as = getattr(store, "delete_as", None)
        if delete_as is None:
            raise PublishedIndexUnavailableError(
                f"IndexStore {type(store).__name__} cannot delete an alias"
            )
        aliases = [alias] + self._delete_member_aliases(username, user_alias, metadata)
        deleted = sum(int(delete_as(each)) for each in aliases)
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
