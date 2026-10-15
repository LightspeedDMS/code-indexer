"""Shared freshness convention for built indexes.

SCIP already had one (task #6): a ``.build_commit`` sidecar under the index
directory holding the commit the index was built at, compared against the clone's
HEAD by :func:`scip_lifecycle.scip_db_is_fresh`. FTS had nothing -- its policy
decided presence from Tantivy's ``meta.json`` alone -- so once an FTS index
existed it was never rebuilt and drifted arbitrarily far behind the clone.

This module lifts that convention into one place so both types share it:

* the SAME marker filename, so there is one thing to look for on disk rather than
  a second parallel mechanism;
* one way to read the clone's commit;
* one way to record it after a build.

SCIP keeps its own ``scip_db_is_fresh``, which layers rules this cannot know about
(the ``no-index`` token for repos where every project is an unsupported language,
and the root-vs-subproject DB shape check). It reads the same marker this writes.

Reading the commit avoids a subprocess on purpose. The backfill sweep walks every
golden repo on every tick -- ~900 of them, every 5 minutes for FTS -- and a
``git rev-parse`` per repo per tick is not affordable at that scale, so the common
case is two small file reads. ``get_current_commit`` remains the fallback for
layouts this does not recognise.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: Shared with scip_lifecycle._BUILD_COMMIT_MARKER -- one convention, one filename.
BUILD_COMMIT_MARKER = ".build_commit"

#: How long a HEAD asked of git-mcp is reused while the alias is unchanged. The
#: sweeps tick every 5 minutes over ~900 golden repos; HEAD only moves when a
#: refresh pulls, and a refresh that publishes changes the alias pointer, which
#: misses the cache at once. The TTL bounds what an out-of-band move (a pull
#: whose re-index failed, so the alias did not move) can leave stale.
HEAD_CACHE_TTL_SECONDS = 1800.0

#: Above this many entries, expired ones are dropped on insert.
_HEAD_CACHE_PRUNE_SIZE = 4096

#: clone path -> (alias version token, monotonic expiry, HEAD or None).
_head_cache: Dict[str, Tuple[str, float, Optional[str]]] = {}
_head_cache_lock = threading.Lock()


def _reset_head_cache() -> None:
    """Forget every cached HEAD (tests)."""
    with _head_cache_lock:
        _head_cache.clear()


def current_commit(repo_path: Path, *, fresh: bool = False) -> Optional[str]:
    """The clone's HEAD commit, or ``None`` when it cannot be determined.

    Reads ``.git`` directly (loose ref, packed-refs, or a detached HEAD) and falls
    back to ``get_current_commit`` for anything unrecognised -- a worktree file,
    say -- so correctness never depends on the fast path covering every layout.

    On a pod that does not hold the repository (``content_backend: delegated``)
    there is no ``.git`` to read: HEAD is asked of git-mcp instead
    (:func:`_current_commit_through_backend`), never of this disk -- and reused
    while the golden repo's alias pointer is unchanged
    (:func:`_cached_backend_commit`). ``fresh=True`` asks git-mcp regardless
    and refreshes the cache.
    """
    from code_indexer.server.startup.content_backend_wiring import (
        is_content_seam_delegated,
    )

    if is_content_seam_delegated():
        return _cached_backend_commit(repo_path, fresh=fresh)

    git_dir = repo_path / ".git"
    try:
        head = (git_dir / "HEAD").read_text(errors="ignore").strip()
    except OSError:
        head = ""

    if head and not head.startswith("ref:"):
        return head.split()[0] or None

    if head:
        ref = head[len("ref:") :].strip()
        if ref:
            try:
                value = (git_dir / ref).read_text(errors="ignore").strip()
                if value:
                    return value.split()[0]
            except OSError:
                pass
            try:
                packed = (git_dir / "packed-refs").read_text(errors="ignore")
                for line in packed.splitlines():
                    if line.startswith("#") or not line.strip():
                        continue
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] == ref:
                        return parts[0]
            except OSError:
                pass

    # Unrecognised layout -- pay for the subprocess rather than guess.
    from code_indexer.utils.git_runner import get_current_commit

    try:
        return get_current_commit(repo_path)
    except Exception:  # noqa: BLE001 - freshness must never abort a sweep
        return None


def _alias_version(repo_path: Path) -> Optional[str]:
    """A token that changes whenever the golden repo's alias pointer changes.

    The ``<alias>-global`` alias document (target, swap and refresh times), read
    from the control-plane registry -- the pod's own state, not the repository.
    *repo_path* is a base clone (``golden-repos/<alias>``) or a versioned
    snapshot (``golden-repos/.versioned/<alias>/v_*``). ``None`` when there is
    no such alias or it cannot be read: the caller then does not cache.
    """
    from code_indexer.global_repos.alias_manager import AliasManager
    from code_indexer.server.storage.shared.snapshot_paths import (
        is_versioned_snapshot,
    )

    path = Path(repo_path)
    if is_versioned_snapshot(str(path)):
        alias, golden_repos_dir = path.parent.name, path.parent.parent.parent
    else:
        alias, golden_repos_dir = path.name, path.parent
    try:
        manager = AliasManager.existing(golden_repos_dir / "aliases")
        record = manager.read_record(f"{alias}-global") if manager else None
    except Exception as exc:  # the registry's state: uncached, never wrong
        logger.debug("index_freshness: alias of %s unreadable: %s", repo_path, exc)
        return None
    if not record:
        return None
    return json.dumps(record, sort_keys=True, default=str)


def _cached_backend_commit(repo_path: Path, *, fresh: bool) -> Optional[str]:
    """:func:`_current_commit_through_backend`, reused per alias version.

    A hit needs the SAME alias version token and an unexpired entry, so a
    refresh that moves the alias is seen on the next call. Only git's own
    answers are cached (a commit, or "not a git repository"); a failure is
    returned as unknown and evicts the entry, so the next call asks again.
    """
    key = str(repo_path)
    version = _alias_version(repo_path)
    now = time.monotonic()
    if version is not None and not fresh:
        with _head_cache_lock:
            hit = _head_cache.get(key)
        if hit is not None and hit[0] == version and now < hit[1]:
            return hit[2]
    commit, definitive = _current_commit_through_backend(repo_path)
    with _head_cache_lock:
        if version is not None and definitive:
            if len(_head_cache) >= _HEAD_CACHE_PRUNE_SIZE:
                for stale in [k for k, v in _head_cache.items() if v[1] <= now]:
                    del _head_cache[stale]
            _head_cache[key] = (version, now + HEAD_CACHE_TTL_SECONDS, commit)
        else:
            _head_cache.pop(key, None)
    return commit


def _current_commit_through_backend(repo_path: Path) -> Tuple[Optional[str], bool]:
    """``(HEAD, definitive)`` of the repository behind *repo_path*, from git-mcp.

    ``None`` -- "cannot be determined", which callers already treat as unknown
    -- when the path is not a git repository (a ``local://`` folder, as its
    missing ``.git`` reads locally); that is git's own answer, so definitive. A
    server with no git seam, or a seam that fails, is also unknown but NOT
    definitive, and LOGGED: the sweep must not abort, but nor may an index's
    freshness go silently unchecked.
    """
    from code_indexer.server.startup.content_backend_wiring import (
        golden_repo_content_root,
    )
    from code_indexer.server.startup.git_backend_wiring import (
        server_delegated_git_backend,
    )
    from code_indexer.services.git_head import NOT_A_REPOSITORY_MARKER

    backend = server_delegated_git_backend()
    if backend is None:
        logger.warning(
            "index_freshness: %s is not on this pod and git_backend is local; "
            "its commit is unknown",
            repo_path,
        )
        return None, False
    clone = str(golden_repo_content_root(repo_path, consumer="index_freshness"))
    try:
        commit = str(backend.rev_parse(clone, "HEAD")).strip()
    except subprocess.CalledProcessError as exc:
        if NOT_A_REPOSITORY_MARKER in str(exc.stderr or ""):
            return None, True
        logger.warning("index_freshness: HEAD of %s unavailable: %s", clone, exc)
        return None, False
    except Exception as exc:  # the seam's transport: a sweep must never abort
        logger.warning("index_freshness: HEAD of %s unavailable: %s", clone, exc)
        return None, False
    return (commit.split()[0] if commit else None), bool(commit)


def recorded_build_commit(index_dir: Path) -> Optional[str]:
    """The commit an index was built at, or ``None`` if never recorded.

    Returns the bare commit, dropping any trailing token (SCIP appends
    ``no-index``), so callers that only care about identity need not parse it.
    """
    try:
        raw = (index_dir / BUILD_COMMIT_MARKER).read_text(errors="ignore").strip()
    except OSError:
        return None
    return raw.split()[0] if raw else None


def record_build_commit(index_dir: Path, repo_path: Path) -> None:
    """Record the clone's current commit beside a freshly built index.

    Best-effort: failing here must never fail the build that just succeeded. Not
    recording costs one extra rebuild later, never a wrong answer.

    Does nothing when the index directory does not exist, so a build that produced
    no index cannot leave a marker claiming otherwise.
    """
    # Fresh: the marker must name the commit the build just saw, and the cache
    # picks it up, so the next freshness check agrees with it.
    commit = current_commit(repo_path, fresh=True)
    if not commit:
        return
    if not index_dir.is_dir():
        # Record BESIDE an index, never in place of one. Creating the directory
        # here would leave a phantom index dir holding only a marker when the build
        # produced nothing -- a repo that looks indexed to anything checking for the
        # directory, and a marker claiming a build that never happened.
        logger.debug(
            "index_freshness: no index at %s; not recording a build commit",
            index_dir,
        )
        return
    try:
        (index_dir / BUILD_COMMIT_MARKER).write_text(commit)
    except OSError as exc:
        logger.debug(
            "index_freshness: could not record build commit for %s: %s", repo_path, exc
        )


def build_commit_is_current(repo_path: Path, index_dir: Path) -> bool:
    """True when the index was built at the clone's current commit.

    Unknown CURRENT commit -> treated as current, so a repo whose revision cannot
    be read is left alone instead of rebuilt on every tick. Unrecorded BUILT commit
    -> NOT current: every index predating this convention is in that state, and
    that is exactly the backlog one rebuild each clears. The engine's per-cycle
    bound and attempt cooldown pace the drain.
    """
    commit = current_commit(repo_path)
    if not commit:
        return True
    recorded = recorded_build_commit(index_dir)
    if recorded is None:
        return False
    return recorded == commit
