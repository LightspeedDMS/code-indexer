"""Is the directory a request is served from one of the catalog's locations?

Catalog identity is otherwise decided from names and activation metadata,
while readers serve a resolved path. This module checks the resolved path
against the catalog's real locations so that a disagreement (an alias
pointer, or a clone, that is in fact the catalog) is treated as the catalog:

- its mutable base clone (``get_cidx_meta_path``) and the target of the
  ``cidx-meta-global`` alias pointer;
- its versioned snapshots (``is_versioned_snapshot`` with the catalog as
  namespace);
- a clone whose local git remote points at one of the above.

"Is the catalog" is the closed answer here: the caller is then refused the
surface. A clone whose git config exists but cannot be read is therefore
answered as the catalog.

Cost per request: alias-pointer reads (never creating a directory), one
``realpath`` and at most one small ``.git/config`` read per served
directory (``served_path_is_catalog``); no fleet scan. Synchronous: async
callers run it on a worker thread.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Callable, List, Mapping, Optional, Tuple

from code_indexer.global_repos.alias_manager import read_alias_from_disk

from ...storage.shared.snapshot_paths import is_versioned_snapshot
from ..cidx_meta_backup.paths import get_cidx_meta_path
from ..constants import CIDX_META_REPO_GLOBAL, is_internal_meta_repo
from .visibility import names_activation

logger = logging.getLogger(__name__)

_REMOTE_URL = re.compile(r"^\s*url\s*=\s*(.+?)\s*$")
_MAX_GIT_CONFIG_BYTES = 65536
_MAX_PROXY_CONFIG_BYTES = 1048576
_FILE_SCHEME = "file://"


def golden_repos_dir_of(activated_repo_manager: Optional[Any]) -> Optional[str]:
    """The golden repositories directory beside the manager's activations
    (``<data_dir>/golden-repos``), or None without a manager."""
    if activated_repo_manager is None:
        return None
    data_dir = getattr(activated_repo_manager, "data_dir", None)
    return str(Path(data_dir) / "golden-repos") if data_dir else None


def state_golden_repos_dir(state: Any) -> Optional[str]:
    """The server state's golden repositories directory; anything but a
    non-empty string counts as missing."""
    golden = getattr(state, "golden_repos_dir", None)
    return golden if isinstance(golden, str) and golden else None


def _alias_target(golden_repos_dir: str, alias: str) -> Optional[str]:
    return read_alias_from_disk(Path(golden_repos_dir) / "aliases", alias)


def catalog_locations(golden_repos_dir: str) -> Tuple[str, ...]:
    """Real paths of the catalog's base clone and current published copy."""
    golden = Path(golden_repos_dir)
    found = [os.path.realpath(get_cidx_meta_path(golden.parent.parent))]
    target = _alias_target(golden_repos_dir, CIDX_META_REPO_GLOBAL)
    if target:
        found.append(os.path.realpath(target))
    return tuple(found)


def _is_catalog_snapshot(real: str) -> bool:
    candidate = PurePosixPath(real)
    for path in (candidate, *candidate.parents):
        if is_versioned_snapshot(str(path)) and is_internal_meta_repo(path.parent.name):
            return True
    return False


def _is_location(real: str, locations: Tuple[str, ...]) -> bool:
    for location in locations:
        if real == location or real.startswith(location + os.sep):
            return True
    return _is_catalog_snapshot(real)


def _local_remote_paths(directory: str) -> Optional[List[str]]:
    """Local paths the clone's git remotes point at ([] when it is not a
    clone); None when its git config exists but cannot be read."""
    config = Path(directory) / ".git" / "config"
    try:
        with open(config, "rb") as handle:
            raw = handle.read(_MAX_GIT_CONFIG_BYTES)
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as exc:
        logger.warning(
            "Git config of a served repository unreadable (%s); answering it "
            "as the catalog",
            type(exc).__name__,
        )
        return None
    paths = []
    for line in raw.decode("utf-8", "replace").splitlines():
        match = _REMOTE_URL.match(line)
        if not match:
            continue
        url = match.group(1)
        if url.startswith(_FILE_SCHEME):
            url = url[len(_FILE_SCHEME) :]
        if url.startswith("/"):
            paths.append(url)
    return paths


def _named_like_catalog(real: str) -> bool:
    return is_internal_meta_repo(PurePosixPath(real).name) or _is_catalog_snapshot(real)


def path_looks_like_catalog(path: str) -> bool:
    """True when *path* alone shows it is the catalog: named like it, one of
    its snapshots, or a clone of such a directory (or a clone whose remotes
    cannot be read). No registry is read."""
    real = os.path.realpath(path)
    if _named_like_catalog(real):
        return True
    remotes = _local_remote_paths(real)
    if remotes is None:
        return True
    return any(_named_like_catalog(os.path.realpath(url)) for url in remotes)


CatalogLocations = Callable[[], Tuple[str, ...]]


def request_catalog_locations(golden_repos_dir: str) -> CatalogLocations:
    """``catalog_locations`` for ONE request: computed on first use, then
    reused for every alias and served directory the request names."""
    found: List[Tuple[str, ...]] = []

    def locations() -> Tuple[str, ...]:
        if not found:
            found.append(catalog_locations(golden_repos_dir))
        return found[0]

    return locations


def path_is_catalog_location(
    path: str, golden_repos_dir: str, locations: Optional[CatalogLocations] = None
) -> bool:
    """True when *path* is the catalog, one of its snapshots, or a clone of
    either (or a clone whose remotes cannot be read). *locations* is the
    request's ``request_catalog_locations``."""
    known = (locations or request_catalog_locations(golden_repos_dir))()
    return _in_locations(path, known)


def served_path_is_catalog(
    path: str,
    golden_repos_dir: Optional[str],
    locations: Optional[CatalogLocations] = None,
) -> bool:
    """``path_looks_like_catalog`` or ``path_is_catalog_location`` for one
    served directory, from ONE ``realpath`` and at most one ``.git/config``
    read. True without *golden_repos_dir* (it cannot be judged)."""
    real = os.path.realpath(path)
    if _named_like_catalog(real) or not golden_repos_dir:
        return True
    known = (locations or request_catalog_locations(golden_repos_dir))()
    if _is_location(real, known):
        return True
    remotes = _local_remote_paths(real)
    if remotes is None:
        return True
    for url in remotes:
        remote = os.path.realpath(url)
        if _named_like_catalog(remote) or _is_location(remote, known):
            return True
    return False


def composite_component_paths(path: str) -> Optional[List[str]]:
    """The component directories when *path* is a composite activation (its
    proxy configuration on disk), ``[]`` when it is not one, None when that
    configuration cannot be read or names a component that is not a plain
    directory name (then answered as the catalog)."""
    config = Path(path) / ".code-indexer" / "config.json"
    try:
        with open(config, "rb") as handle:
            raw = handle.read(_MAX_PROXY_CONFIG_BYTES + 1)
        data = json.loads(raw) if len(raw) <= _MAX_PROXY_CONFIG_BYTES else None
    except (FileNotFoundError, NotADirectoryError):
        return []
    except (OSError, ValueError) as exc:
        logger.warning(
            "Configuration of a served repository unreadable (%s); answering "
            "it as the catalog",
            type(exc).__name__,
        )
        return None
    if not isinstance(data, dict):
        return None
    if data.get("proxy_mode") is not True:
        return []
    names = data.get("discovered_repos")
    if not isinstance(names, list) or not all(_plain_name(n) for n in names):
        return None
    return [os.path.join(path, name) for name in names]


def activation_is_composite(
    alias: Any, username: str, activated_repo_manager: Optional[Any]
) -> bool:
    """True when *alias* names the caller's activation and its record says it
    is a composite; a record that cannot be read counts as one."""
    if activated_repo_manager is None or not isinstance(alias, str):
        return False
    if not names_activation(alias):
        return False
    try:
        record = activated_repo_manager.get_repository(username, alias, touch=False)
    except Exception as exc:
        logger.warning(
            "Activation lookup for composite detection failed (%s); treating "
            "it as a composite",
            type(exc).__name__,
        )
        return True
    return isinstance(record, Mapping) and bool(record.get("is_composite"))


def _plain_name(name: Any) -> bool:
    return (
        isinstance(name, str)
        and name not in ("", ".", "..")
        and not any(ch in name for ch in "/\\\x00")
    )


def _in_locations(path: str, locations: Tuple[str, ...]) -> bool:
    real = os.path.realpath(path)
    if _is_location(real, locations):
        return True
    remotes = _local_remote_paths(real)
    if remotes is None:
        return True
    return any(_is_location(os.path.realpath(url), locations) for url in remotes)


def request_served_paths(
    alias: str,
    username: str,
    activated_repo_manager: Optional[Any],
    golden_repos_dir: str,
) -> List[str]:
    """Every directory a door may serve for *alias*: the caller's activation
    with that name (when it exists) and the alias pointer targets of the
    alias and its ``-global`` form. A path-shaped alias names none."""
    if not names_activation(alias) and not alias.endswith("-global"):
        return []
    paths: List[str] = []
    if activated_repo_manager is not None and names_activation(alias):
        activated = activated_repo_manager.get_activated_repo_path(username, alias)
        if activated and os.path.isdir(activated):
            paths.append(str(activated))
    names = [alias] if alias.endswith("-global") else [alias, f"{alias}-global"]
    for name in names:
        target = _alias_target(golden_repos_dir, name)
        if target:
            paths.append(target)
    return paths


def alias_serves_catalog_path(
    alias: str,
    username: str,
    activated_repo_manager: Optional[Any],
    golden_repos_dir: Optional[str],
    locations: Optional[CatalogLocations] = None,
) -> bool:
    """True when any directory a door may serve for *alias* is a catalog
    location. Without a golden repositories directory nothing is checked.
    *locations* is the request's ``request_catalog_locations``."""
    if not golden_repos_dir:
        return False
    paths = request_served_paths(
        alias, username, activated_repo_manager, golden_repos_dir
    )
    if not paths:
        return False
    known = (locations or request_catalog_locations(golden_repos_dir))()
    return any(_in_locations(path, known) for path in paths)
