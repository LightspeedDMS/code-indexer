"""A golden repo's temporal shards in the IndexStore, served on every pod.

Server-context temporal data lives at ONE fixed, mutable path per golden repo,
``{golden_repos_dir}/.temporal/{alias}/`` (``server_temporal_index_root``),
outside any snapshot. Under ``index_store_backend: delegated`` that directory
existed only on the pod that ran the temporal indexing, so temporal search
anywhere else had no data. Here:

* the pod that indexed temporal data PUBLISHES the shard root as its own
  versioned artifact (``temporal.<sha256(repo)>``, never a golden index alias)
  and the version is recorded with the golden alias pointer
  (``temporal_index_store_alias`` / ``temporal_index_version``);
* every reader -- temporal search, multi-repo temporal search, the live
  temporal worker, temporal status, repository health -- first makes this pod
  hold the RECORDED version (``served_temporal_index_root``): checked out when
  absent, swapped by rename (with rollback) when this pod holds another one;
* a pod about to index temporal data first serves the recorded version, so an
  incremental pass builds on the latest shards, not on whatever it held.

Markers live BESIDE the shard root (``.<alias>.temporal-checked-out`` holds
the version this pod has; ``.<alias>.temporal-in-progress`` marks an
interrupted swap), never inside the published tree.

Fail loud: an unreachable store or a failed checkout raises
``PublishedIndexUnavailableError``. A pointer recording NO temporal version
(never published -- temporal not enabled, or written before this existed) is
served from what this pod holds, as before. Local store: exactly
``server_temporal_index_root`` (no alias read, nothing fetched).
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple, Union

from code_indexer.server.services.index_materializer import (
    PublishedIndexUnavailableError,
    served_cache,
    still_served,
    exclusive_checkout,
)
from code_indexer.server.services.served_index_cache import KIND_TEMPORAL
from code_indexer.utils.index_store import (
    TEMPORAL_INDEX_STORE_ALIAS_FIELD,
    TEMPORAL_INDEX_VERSION_FIELD,
    IndexStore,
    is_temporal_store_alias,
    published_temporal_fields,
    temporal_index_store_alias,
)

logger = logging.getLogger(__name__)

_CHECKED_OUT_SUFFIX = ".temporal-checked-out"
_IN_PROGRESS_SUFFIX = ".temporal-in-progress"
_INCOMING_SUFFIX = ".temporal-incoming"
_RETIRED_SUFFIX = ".temporal-retired"
_VERSIONED_PREFIX = ".temporal-v-"

PathLike = Union[str, Path]


def _sibling(root: Path, suffix: str) -> Path:
    return root.parent / f".{root.name}{suffix}"


def _versioned_dir(root: Path, version: str) -> Path:
    """Alias-scoped versioned directory: ``.{alias}.temporal-v-{version}``."""
    return root.parent / f".{root.name}{_VERSIONED_PREFIX}{version}"


def _versioned_prefix(root: Path) -> str:
    """Only entries starting with this belong to ``root``'s alias."""
    return f".{root.name}{_VERSIONED_PREFIX}"


def checked_out_marker(root: Path) -> Path:
    """This pod's record of which published version it holds at ``root``."""
    return _sibling(root, _CHECKED_OUT_SUFFIX)


def _store_method(store: IndexStore, name: str) -> Any:
    method = getattr(store, name, None)
    if method is None:
        raise PublishedIndexUnavailableError(
            f"IndexStore {type(store).__name__} has no {name}"
        )
    return method


def _server_store() -> IndexStore:
    from code_indexer.server.startup.index_store_wiring import server_index_store

    return server_index_store()


def recorded_temporal(
    document: Optional[Mapping[str, Any]],
) -> Optional[Tuple[str, str]]:
    """``(store alias, version)`` a pointer records for the temporal shards,
    or None when it records none."""
    fields = document or {}
    alias = fields.get(TEMPORAL_INDEX_STORE_ALIAS_FIELD)
    version = fields.get(TEMPORAL_INDEX_VERSION_FIELD)
    if isinstance(alias, str) and alias and isinstance(version, str) and version:
        return alias, version
    return None


def _held_version(root: Path) -> Optional[str]:
    try:
        return checked_out_marker(root).read_text()
    except FileNotFoundError:
        return None


def _is_current(root: Path, version: str) -> bool:
    return (
        root.is_dir()
        and not _sibling(root, _IN_PROGRESS_SUFFIX).exists()
        and _held_version(root) == version
    )


def _global_alias(repo_alias: str) -> str:
    bare = repo_alias.removesuffix("-global")
    return f"{bare}-global"


def _alias_document(golden_repos_dir: PathLike, repo_alias: str) -> Any:
    from code_indexer.global_repos.alias_manager import AliasManager

    aliases = AliasManager(str(Path(golden_repos_dir) / "aliases"))
    return aliases.read_record(_global_alias(repo_alias))


def ensure_temporal_root(
    root: Path,
    document: Optional[Mapping[str, Any]],
    repo_alias: str,
    *,
    store: IndexStore,
) -> None:
    """Make this pod hold the temporal version ``document`` records at
    ``root`` (no-op when it already does, or when nothing is recorded)."""
    if store.is_local:
        return
    recorded = recorded_temporal(document)
    if recorded is None:
        return
    store_alias, version = recorded
    expected = temporal_index_store_alias(repo_alias.removesuffix("-global"))
    if store_alias != expected or not is_temporal_store_alias(store_alias):
        raise PublishedIndexUnavailableError(
            f"The pointer of {repo_alias} names temporal alias {store_alias!r}, "
            "not this repository's own; refusing to use it"
        )
    cache = served_cache()
    if _is_current(root, version) and still_served(cache, root):
        return
    with exclusive_checkout(root):
        if _is_current(root, version):
            still_served(cache, root)
            return
        if cache is not None:
            cache.admit(root, version)
        _swap_in(root, store_alias, version, store)
        if cache is not None:
            cache.admitted(root, version, kind=KIND_TEMPORAL)


def _swap_in(root: Path, store_alias: str, version: str, store: IndexStore) -> None:
    checkout_dir = _store_method(store, "checkout_dir")
    incoming = _sibling(root, _INCOMING_SUFFIX)
    marker = _sibling(root, _IN_PROGRESS_SUFFIX)
    versioned = _versioned_dir(root, version)
    tmp_link = _sibling(root, f".temporal-swap-{version}")
    root.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(incoming, ignore_errors=True)
    try:
        checkout_dir(store_alias, incoming, version)
        marker.write_text(version)
        shutil.rmtree(versioned, ignore_errors=True)
        os.rename(incoming, versioned)
        tmp_link.unlink(missing_ok=True)
        os.symlink(versioned, tmp_link)
        # Atomic: rename of a symlink over a symlink (or onto a missing
        # path) is a single kernel inode operation.  When root is a real
        # directory (first swap after upgrade), rename over it fails, so
        # we fall back to the two-step retire-then-link path.
        try:
            os.rename(tmp_link, root)
        except (IsADirectoryError, OSError):
            retired = _sibling(root, _RETIRED_SUFFIX)
            shutil.rmtree(retired, ignore_errors=True)
            os.rename(root, retired)
            os.rename(tmp_link, root)
        checked_out_marker(root).write_text(version)
        marker.unlink()
    except Exception as exc:
        # Rollback: if root is gone or broken, restore from the retired
        # dir (legacy) or just leave what was there.
        tmp_link.unlink(missing_ok=True)
        retired = _sibling(root, _RETIRED_SUFFIX)
        if retired.exists() and not root.exists():
            os.rename(retired, root)
        marker.unlink(missing_ok=True)
        raise PublishedIndexUnavailableError(
            f"Could not check out temporal shards {store_alias} version "
            f"{version} to {root}: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        shutil.rmtree(incoming, ignore_errors=True)
    _cleanup_old_versioned_dirs(root, keep_version=version)
    logger.info("Temporal shards %s version %s now at %s", store_alias, version, root)
    _evict_caches(root)


def _cleanup_old_versioned_dirs(root: Path, keep_version: Optional[str]) -> None:
    """Remove versioned temporal directories from previous swaps, plus any
    legacy ``.temporal-retired`` directory left by the pre-symlink code."""
    retired = _sibling(root, _RETIRED_SUFFIX)
    shutil.rmtree(retired, ignore_errors=True)
    try:
        current_target = root.resolve() if root.is_symlink() else None
    except OSError:
        current_target = None
    prefix = _versioned_prefix(root)
    try:
        for entry in root.parent.iterdir():
            if not entry.name.startswith(prefix):
                continue
            if keep_version and entry.name == f"{prefix}{keep_version}":
                continue
            if current_target:
                try:
                    if entry.resolve() == current_target:
                        continue
                except OSError:
                    pass
            shutil.rmtree(entry, ignore_errors=True)
    except OSError:
        pass


def _evict_caches(root: Path) -> None:
    """Drop this process's cached handles under ``root``: the files behind
    them were just swapped for another version."""
    from code_indexer.server.cache.snapshot_cache_invalidation import (
        invalidate_snapshot_caches,
    )

    invalidate_snapshot_caches(
        str(root),
        log_context="[temporal-replace]",
        is_versioned_snapshot_check=lambda _path: True,
    )


def served_temporal_index_root(
    golden_repos_dir: PathLike,
    repo_alias: str,
    *,
    store: Optional[IndexStore] = None,
) -> Path:
    """``server_temporal_index_root``, with this pod holding the recorded
    temporal version under a delegated IndexStore. Raises on a store failure.
    Under the local store this is exactly ``server_temporal_index_root``."""
    from code_indexer.services.temporal.temporal_server_paths import (
        server_temporal_index_root,
    )

    root = server_temporal_index_root(golden_repos_dir, repo_alias)
    active = store if store is not None else _server_store()
    if active.is_local:
        return root
    document = _alias_document(golden_repos_dir, repo_alias)
    ensure_temporal_root(root, document, repo_alias, store=active)
    return root


class TemporalNotServedHereError(PublishedIndexUnavailableError):
    """Temporal data may exist but cannot be served on this pod: "not served
    here", which a status surface must never report as "absent"."""


def require_temporal_answerable(
    golden_repos_dir: PathLike,
    repo_alias: str,
    *,
    golden_repo_manager: Any,
    store: Optional[IndexStore] = None,
) -> None:
    """Raise :class:`TemporalNotServedHereError` when this pod cannot tell
    whether *repo_alias* has temporal data, before a status read says "absent".

    Under a delegated IndexStore a recorded temporal version is checked out by
    ``served_temporal_index_root``. A pointer recording NONE is served from
    what this pod holds -- right when temporal is not enabled (it is absent),
    but for a repository with temporal enabled whose pointer predates temporal
    publishing, an empty root here says nothing about the repository. No-op
    under the local store (nothing else is read), when temporal is not enabled
    on the golden repository (*golden_repo_manager*), when a version is
    recorded, or when this pod holds a root.
    """
    from code_indexer.services.temporal.temporal_server_paths import (
        server_temporal_index_root,
    )

    active = store if store is not None else _server_store()
    if active.is_local or not golden_temporal_enabled(golden_repo_manager, repo_alias):
        return
    if recorded_temporal(_alias_document(golden_repos_dir, repo_alias)) is not None:
        return
    if server_temporal_index_root(golden_repos_dir, repo_alias).is_dir():
        return
    raise TemporalNotServedHereError(
        f"Temporal index of {repo_alias} is not served on this pod: its alias "
        "records no published temporal version (refresh the repository to "
        "publish one); whether it has temporal data cannot be told here"
    )


def golden_temporal_enabled(golden_repo_manager: Any, golden_alias: str) -> bool:
    """Whether the golden repository *golden_alias* has temporal indexing on.

    False when the manager does not know the repository. No manager at all
    cannot tell, so it answers True: the caller then checks what is recorded
    and held here rather than calling temporal absent."""
    if golden_repo_manager is None:
        return True
    repo = golden_repo_manager.get_golden_repo(golden_alias.removesuffix("-global"))
    return bool(getattr(repo, "enable_temporal", False))


def publish_temporal_root(
    golden_repos_dir: PathLike,
    repo_alias: str,
    *,
    store: IndexStore,
    base_version: Optional[str],
    use_current_base: bool = False,
) -> Dict[str, str]:
    """Publish this pod's temporal shard root; the alias-document fields.

    ``use_current_base`` (a refresh, which holds the repo's write lock):
    publish over the store's current version. Otherwise exactly on top of
    ``base_version`` (CAS: a publish by anyone in between is refused).
    """
    from code_indexer.services.temporal.temporal_server_paths import (
        server_temporal_index_root,
    )

    root = server_temporal_index_root(golden_repos_dir, repo_alias)
    store_alias = temporal_index_store_alias(repo_alias.removesuffix("-global"))
    if use_current_base:
        version = _store_method(store, "publish_as")(store_alias, root)
    else:
        version = _store_method(store, "publish_on")(store_alias, root, base_version)
    if not version:
        raise PublishedIndexUnavailableError(
            f"Publishing the temporal shards of {repo_alias} returned no version"
        )
    checked_out_marker(root).write_text(str(version))
    return published_temporal_fields(store_alias, str(version))
