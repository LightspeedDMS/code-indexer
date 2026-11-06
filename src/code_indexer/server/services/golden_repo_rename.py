"""EVO-64616: rename a golden repository's alias without re-cloning or
re-embedding.

What moves (``old`` -> ``new``; ``-global`` is the query alias):

========================  =================================================
clone directory           ``golden-repos/<old>`` -> ``golden-repos/<new>``:
                          ``os.rename`` locally; under the content seam
                          workspace-mcp's ``/clone-path/move``, or where it
                          lacks that route a ``/clone-path`` copy (reflink),
                          the old copy deleted after the rows moved
published index           delegated IndexStore: the current version checked
                          out and published under the new store alias (an
                          upload, no embedding), temporal shards likewise;
                          the old store aliases deleted at the end
``-global`` pointer       re-created as ``<new>-global`` with the same
                          target (rewritten when it is the base clone), the
                          new index version and ``renamed_from``
registry rows             ``golden_repos_metadata`` (category, wiki flag,
                          temporal options, workspace id kept; URL/branch
                          corrected when given) and ``global_repos``
satellites                activated repos and composite members (their
                          golden reference), group grants, the cidx-meta
                          description (``<alias>.md`` and its ``name:``),
                          the wiki cache, description/dependency-map
                          tracking
========================  =================================================

Left in place on purpose: ``.versioned/<old>/v_*`` snapshots stay where the
pointer names them (a path is opaque once published; moving a live snapshot
under readers is the risk this avoids). The next refresh snapshots under
``.versioned/<new>/`` and the old namespace drains once no pointer names it
(the reclaim never deletes a referenced snapshot). Per-pod served checkouts
of the old names are sweep leftovers; activated repos' ``golden`` remotes
are re-pointed by their next sync; composite member directories keep their
names. History (jobs, audit, search/embedding stats, logs) keeps the old
alias. There is no old-alias redirect: callers use the new alias at once.

Crash safety. The old pointer is marked ``renaming_to`` first, so a
re-run of the same rename resumes, and the registry reconcile leaves both
directories alone meanwhile. Every step is idempotent and ordered so each
intermediate state is consistent: the new pointer exists before the new row
(the activation repair never activates it twice), the rows move before the
old pointer is deleted (its tombstone keeps the reconcile from re-adding the
old directory), and nothing is deleted before the new copy is registered.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Set, Tuple

from code_indexer.utils.git_remote_url import parse_git_remote_url
from code_indexer.utils.subprocess_diagnostics import redact_url_credentials

logger = logging.getLogger(__name__)

RENAME_OPERATION = "rename_golden_repo"
GLOBAL_SUFFIX = "-global"
#: On the old ``-global`` pointer while a rename of it is unfinished.
RENAMING_TO = "renaming_to"
#: On the new ``-global`` pointer: the alias it was renamed from.
RENAMED_FROM = "renamed_from"
#: Pointer fields that belong to one alias and are never copied.
_NOT_COPIED = ("target_path", "previous_path", "repo_name", RENAMING_TO)
_INDEX_FIELDS = ("index_store_alias", "index_version")
_TEMPORAL_FIELDS = ("temporal_index_store_alias", "temporal_index_version")
_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_OWNER = "rename_repo"
#: Jobs that own an alias while they run: a rename waits for every one.
CONFLICTING_OPERATIONS = (
    "add_golden_repo",
    "remove_golden_repo",
    "change_branch",
    "add_index",
    RENAME_OPERATION,
)


class RenameError(RuntimeError):
    """A rename refused or failed; ``status`` is its HTTP status."""

    status = 500


class RenameInvalidError(RenameError):
    status = 400


class RenameNotFoundError(RenameError):
    status = 404


class RenameConflictError(RenameError):
    status = 409


def validate_alias(alias: str) -> str:
    """*alias* stripped, or RenameInvalidError (no path characters, no
    ``-global`` suffix, a letter or digit first)."""
    value = (alias or "").strip()
    if not _ALIAS_RE.match(value) or value.endswith(GLOBAL_SUFFIX):
        raise RenameInvalidError(
            f"Invalid alias {alias!r}: letters, digits, '-' and '_' only, "
            f"starting with a letter or digit, not ending in {GLOBAL_SUFFIX}"
        )
    return value


class CloneDirs(Protocol):
    """Golden clone directories, wherever they live."""

    is_delegated: bool

    def exists(self, path: str) -> bool: ...

    def move(self, source: str, dest: str) -> None:
        """Make *dest* a full copy of *source*; *source* may go at once
        (local) or stay until :meth:`remove` (delegated)."""
        ...

    def remove(self, path: str) -> bool: ...


class LocalCloneDirs:
    """Clones on this pod's disk: an atomic ``os.rename``."""

    is_delegated = False

    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def move(self, source: str, dest: str) -> None:
        os.rename(source, dest)

    def remove(self, path: str) -> bool:
        if not os.path.exists(path):
            return False
        shutil.rmtree(path)
        return True


class DelegatedCloneDirs:
    """Clones on workspace-mcp (``/clone-path``). A move is an atomic rename
    (``/clone-path/move``) where workspace-mcp has it; otherwise a copy (a
    reflink where the volume can), the source deleted after the rows moved."""

    is_delegated = True

    def __init__(self, working_copies: Any, timeout: Optional[float] = None) -> None:
        self._copies = working_copies
        self._timeout = timeout
        self._move_route = True

    def exists(self, path: str) -> bool:
        return bool(self._copies.exists(path))

    def move(self, source: str, dest: str) -> None:
        if self._move_route:
            if self._copies.move(source, dest, timeout=self._timeout):
                return
            self._move_route = False
            logger.info(
                "workspace-mcp has no /clone-path/move; copying %s to %s instead",
                source,
                dest,
            )
        self._copies.clone(source, dest, timeout=self._timeout, cancel_check=None)

    def remove(self, path: str) -> bool:
        return bool(self._copies.remove(path))


@dataclass
class RenameDeps:
    """What a rename touches. ``from_manager`` builds it for a server;
    tests pass fakes."""

    golden_repos_dir: str
    metadata: Any  # GoldenRepoMetadataBackend
    registry: Any  # GlobalRegistry / its Postgres adapter
    aliases: Any  # AliasManager
    clone_dirs: CloneDirs
    git: Any  # GitBackend (the clone's git, through the seam)
    index_store: Any = None  # IndexStore; None or is_local: nothing published
    scheduler: Any = None  # RefreshScheduler: locks and refresh checks
    job_tracker: Any = None
    write_mode_active: Callable[[str], bool] = lambda alias: False
    strip_credentials: Callable[[str], str] = lambda url: url
    satellites: List[Tuple[str, Callable[[str, str, Optional[str]], Any]]] = field(
        default_factory=list
    )
    cache: Optional[Dict[str, Any]] = None  # the manager's per-worker cache


@dataclass
class RenamePlan:
    """A checked rename. ``resume``: an earlier run of it did not finish."""

    old: str
    new: str
    repo_url: str
    branch: str
    resume: bool = False


def _global(alias: str) -> str:
    return f"{alias}{GLOBAL_SUFFIX}"


def _rewrite(path: Optional[str], old_dir: str, new_dir: str) -> Optional[str]:
    """*path* with the old base clone replaced by the new one; any other
    path (a ``.versioned`` snapshot) is opaque and kept."""
    if path and os.path.normpath(path) == os.path.normpath(old_dir):
        return new_dir
    return path


class GoldenRepoRenamer:
    """Plan (checks only) and execute (under both aliases' write locks) one
    rename; both raise :class:`RenameError` subclasses."""

    def __init__(self, deps: RenameDeps) -> None:
        self.d = deps

    def dir_of(self, alias: str) -> str:
        return os.path.join(self.d.golden_repos_dir, alias)

    # -- plan -------------------------------------------------------------

    def plan(
        self,
        old: str,
        new: str,
        repo_url: Optional[str] = None,
        branch: Optional[str] = None,
    ) -> RenamePlan:
        old, new = validate_alias(old), validate_alias(new)
        if old == new:
            raise RenameInvalidError("The new alias is the current alias")
        old_row = self.d.metadata.get_repo(old)
        new_row = self.d.metadata.get_repo(new)
        old_ptr = self.d.aliases.read_record(_global(old))
        new_ptr = self.d.aliases.read_record(_global(new))
        ours = bool(new_ptr) and new_ptr.get(RENAMED_FROM) == old
        marked = bool(old_ptr) and old_ptr.get(RENAMING_TO) == new
        if old_row is None:
            if new_row is not None and ours:
                return RenamePlan(
                    old,
                    new,
                    str(new_row["repo_url"]),
                    str(new_row["default_branch"]),
                    resume=True,
                )
            raise RenameNotFoundError(f"Golden repository '{old}' not found")
        if new_row is not None and not (ours and marked):
            raise RenameConflictError(f"Golden repository '{new}' already exists")
        if new_ptr is not None and not ours:
            raise RenameConflictError(f"Alias '{_global(new)}' already exists")
        if old_ptr is None:
            raise RenameConflictError(
                f"Golden repository '{old}' is not globally active (no "
                f"'{_global(old)}' pointer): wait for the activation repair"
            )
        if self.d.clone_dirs.exists(self.dir_of(new)) and not marked:
            raise RenameConflictError(
                f"A directory named '{new}' already exists under the golden "
                "repositories and is not this rename's"
            )
        current_url = str(old_row["repo_url"])
        if current_url.startswith("local://"):
            raise RenameInvalidError(
                f"'{old}' is a local:// repository; only cloned repositories "
                "can be renamed"
            )
        clone_path = str(old_row.get("clone_path") or self.dir_of(old))
        if os.path.normpath(clone_path) != os.path.normpath(self.dir_of(old)):
            raise RenameInvalidError(
                f"'{old}' is not cloned at its own golden-repos entry "
                f"({clone_path}); it cannot be moved"
            )
        target_url = repo_url.strip() if repo_url else current_url
        target_branch = branch.strip() if branch else str(old_row["default_branch"])
        if repo_url or branch:
            self._check_content(old, new, target_url, target_branch)
        return RenamePlan(
            old,
            new,
            self.d.strip_credentials(target_url),
            target_branch,
            resume=marked,
        )

    def _clone_to_probe(self, old: str, new: str) -> str:
        """The directory that holds the clone now: the old one while it
        exists (a delegated copy at the new name may be partial), else the
        new one (a resumed local rename already moved it)."""
        if self.d.clone_dirs.exists(self.dir_of(old)):
            return self.dir_of(old)
        return self.dir_of(new)

    def _check_content(self, old: str, new: str, repo_url: str, branch: str) -> None:
        """A corrected URL/branch must describe what the clone holds: its
        origin is that repository and HEAD is that branch, on origin's
        history. A ``file://`` URL is an in-place registration of the
        directory itself: it must name the new directory. A clone that
        cannot be read refuses the rename (409), never a raw git error."""
        clone = self._clone_to_probe(old, new)
        try:
            self._check_clone(clone, old, new, repo_url, branch)
        except RenameError:
            raise
        except Exception as exc:
            # The git error's own text is the only clue to why (git-mcp's
            # stderr rides on the exception); without it the refusal said
            # just "GitMcpBackendError" (EVO-64616).
            detail = redact_url_credentials(
                str(getattr(exc, "stderr", None) or exc).strip()
            )[:300]
            logger.warning(
                "Rename '%s' -> '%s': reading the clone at %s failed: %s: %s",
                old,
                new,
                clone,
                type(exc).__name__,
                detail,
            )
            raise RenameConflictError(
                f"The clone of '{old}' at {clone} could not be read to verify "
                f"the URL/branch ({type(exc).__name__}: {detail}); refused"
            ) from exc

    def _check_clone(
        self, clone: str, old: str, new: str, repo_url: str, branch: str
    ) -> None:
        if repo_url.startswith("file://"):
            if os.path.normpath(repo_url[len("file://") :]) != os.path.normpath(
                self.dir_of(new)
            ):
                raise RenameInvalidError(
                    f"A file:// URL must name the renamed directory "
                    f"file://{self.dir_of(new)}"
                )
        else:
            wanted = parse_git_remote_url(repo_url)
            if wanted is None:
                raise RenameInvalidError(
                    f"{redact_url_credentials(repo_url)} is not a git remote URL"
                )
            origin_url = str(self.d.git.remote_get_url(clone, "origin", timeout=10))
            origin = parse_git_remote_url(origin_url)
            if origin is None or origin.identity.casefold() != (
                wanted.identity.casefold()
            ):
                raise RenameInvalidError(
                    f"The clone of '{old}' fetches from "
                    f"{redact_url_credentials(origin_url)}, not "
                    f"{redact_url_credentials(repo_url)}: refused"
                )
        from code_indexer.server.storage.shared.git_backend import head_branch

        head = head_branch(self.d.git, clone, timeout=10)
        if head != branch:
            raise RenameInvalidError(
                f"The clone of '{old}' is on '{head or 'a detached HEAD'}', not "
                f"'{branch}': refused"
            )
        from code_indexer.services.git_head import head_is_unborn

        if head_is_unborn(self.d.git, clone):
            # An empty clone (nothing pushed upstream yet) has no commit at all,
            # so none ahead of origin -- and no refs/remotes/origin/<branch> to
            # count against: rev-list fails "unknown revision" on it, which
            # refused every rename of an empty clone (EVO-64616).
            return
        ahead = int(
            self.d.git.rev_list_count(
                clone, f"refs/remotes/origin/{branch}..HEAD", timeout=30
            )
        )
        if ahead:
            raise RenameInvalidError(
                f"The clone of '{old}' has {ahead} commit(s) that origin's "
                f"'{branch}' does not: its content is not that branch; refused"
            )

    # -- guards -----------------------------------------------------------

    def check_not_busy(self, plan: RenamePlan, *, own_job: Optional[str]) -> None:
        """Refuse while another job owns either alias, a refresh of either is
        running, or the old one is in write mode."""
        for alias in (plan.old, plan.new):
            if self.d.job_tracker is not None:
                for operation in CONFLICTING_OPERATIONS:
                    job = self.d.job_tracker.active_job_id(operation, alias)
                    if job and job != own_job:
                        raise RenameConflictError(
                            f"A {operation} job ({job}) of '{alias}' is in flight"
                        )
            if self.d.scheduler is not None:
                try:
                    self.d.scheduler.check_refresh_not_in_progress(alias)
                except Exception as exc:
                    raise RenameConflictError(
                        f"A refresh of '{alias}' is in progress ({exc})"
                    ) from exc
        if self.d.write_mode_active(plan.old):
            raise RenameConflictError(f"'{plan.old}' has an open write-mode session")

    def locked(self, plan: RenamePlan) -> "_Locks":
        return _Locks(self.d.scheduler, sorted((plan.old, plan.new)))

    # -- execute ----------------------------------------------------------

    def execute(self, plan: RenamePlan) -> Dict[str, Any]:
        """Run every step of *plan* (each idempotent); the caller holds the
        locks. Returns what was done."""
        old, new = plan.old, plan.new
        old_dir, new_dir = self.dir_of(old), self.dir_of(new)
        result: Dict[str, Any] = {"old_alias": old, "new_alias": new}
        old_ptr = self.d.aliases.read_record(_global(old))
        if old_ptr is not None and old_ptr.get(RENAMING_TO) != new:
            self.d.aliases.update_fields_if(_global(old), {}, {RENAMING_TO: new})
            old_ptr[RENAMING_TO] = new
        new_ptr = self.d.aliases.read_record(_global(new))
        rows_moved = self.d.metadata.get_repo(new) is not None
        if new_ptr is None:
            if old_ptr is None:
                raise RenameError(
                    f"Neither '{_global(old)}' nor '{_global(new)}' exists: "
                    "nothing to rename from"
                )
            fields = self._republish(old, new, old_ptr)
            result["index_versions_published"] = len(fields) // 2
            self._move_clone(old_dir, new_dir)
            self._create_pointer(old, new, old_ptr, fields, old_dir, new_dir)
        elif not rows_moved:
            self._move_clone(old_dir, new_dir)
        self._move_rows(plan, old_dir, new_dir)
        if self.d.aliases.read_record(_global(old)) is not None:
            # AliasManager.delete_alias logs a failure instead of raising:
            # read it back, or the old alias would stay queryable.
            self.d.aliases.delete_alias(_global(old))
            if self.d.aliases.read_record(_global(old)) is not None:
                raise RenameError(
                    f"The pointer '{_global(old)}' could not be deleted; the "
                    "rename is otherwise done -- run it again to finish"
                )
        result["satellites"] = self._satellites(plan)
        result["leftovers"] = self._cleanup(old, old_dir, new_dir, old_ptr)
        logger.info("Renamed golden repository '%s' to '%s'", old, new)
        return result

    def _republish(self, old: str, new: str, old_ptr: Dict[str, Any]) -> Dict[str, str]:
        """The new alias's published versions (a copy of the old ones), as
        pointer fields; {} under a local store or with nothing published."""
        store = self.d.index_store
        if store is None or getattr(store, "is_local", True):
            return {}
        from code_indexer.utils.index_store import (
            index_store_alias,
            temporal_index_store_alias,
        )

        fields: Dict[str, str] = {}
        pairs = (
            (_INDEX_FIELDS, index_store_alias(new), False),
            (_TEMPORAL_FIELDS, temporal_index_store_alias(new), True),
        )
        for (alias_key, version_key), new_store_alias, temporal in pairs:
            old_store_alias = old_ptr.get(alias_key)
            version = old_ptr.get(version_key)
            if not old_store_alias or not version:
                continue
            current = store.current_version_as(new_store_alias)
            if current is None:
                current = self._copy_version(
                    store, str(old_store_alias), str(version), new_store_alias, temporal
                )
            fields[alias_key] = new_store_alias
            fields[version_key] = str(current)
        return fields

    @staticmethod
    def _copy_version(
        store: Any, source: str, version: str, dest: str, temporal: bool
    ) -> str:
        """Check *source*@*version* out and publish it as *dest*'s first
        version (an upload of the built index, never a rebuild)."""
        with tempfile.TemporaryDirectory(prefix="cidx-rename-") as scratch:
            if temporal:
                built = store.checkout_dir(source, Path(scratch) / "temporal", version)
            else:
                built = store.checkout_as(source, Path(scratch), version)
            return str(store.publish_on(dest, Path(built), None))

    def _move_clone(self, old_dir: str, new_dir: str) -> None:
        dirs = self.d.clone_dirs
        old_there, new_there = dirs.exists(old_dir), dirs.exists(new_dir)
        if new_there and not old_there:
            return  # moved by an earlier run
        if not old_there:
            raise RenameError(f"The clone {old_dir} is gone; nothing to move")
        if new_there:
            if not dirs.is_delegated:
                raise RenameConflictError(
                    f"Both {old_dir} and {new_dir} exist; refusing to choose"
                )
            # an interrupted copy (nothing names it yet): start over
            dirs.remove(new_dir)
        dirs.move(old_dir, new_dir)

    def _create_pointer(
        self,
        old: str,
        new: str,
        old_ptr: Dict[str, Any],
        fields: Dict[str, str],
        old_dir: str,
        new_dir: str,
    ) -> None:
        extra = {k: v for k, v in old_ptr.items() if k not in _NOT_COPIED}
        for key in _INDEX_FIELDS + _TEMPORAL_FIELDS:
            extra.pop(key, None)
        extra.update(fields)
        previous = _rewrite(old_ptr.get("previous_path"), old_dir, new_dir)
        if previous:
            extra["previous_path"] = previous
        extra[RENAMED_FROM] = old
        target = _rewrite(old_ptr.get("target_path"), old_dir, new_dir) or new_dir
        self.d.aliases.create_alias(
            _global(new), str(target), repo_name=new, extra=extra
        )

    def _move_rows(self, plan: RenamePlan, old_dir: str, new_dir: str) -> None:
        old, new = plan.old, plan.new
        old_row = self.d.metadata.get_repo(old)
        if self.d.metadata.get_repo(new) is None:
            if old_row is None:
                raise RenameError(f"The registry row of '{old}' is gone")
            self.d.metadata.add_repo(
                alias=new,
                repo_url=plan.repo_url,
                default_branch=plan.branch,
                clone_path=new_dir,
                created_at=old_row["created_at"],
                enable_temporal=bool(old_row.get("enable_temporal")),
                temporal_options=old_row.get("temporal_options"),
                workspace_id=old_row.get("workspace_id"),
            )
            if old_row.get("category_id") is not None:
                self.d.metadata.update_category(
                    new,
                    old_row["category_id"],
                    bool(old_row.get("category_auto_assigned")),
                )
            if old_row.get("wiki_enabled"):
                self.d.metadata.update_wiki_enabled(new, True)
        old_global = self.d.registry.get_global_repo(_global(old))
        if self.d.registry.get_global_repo(_global(new)) is None:
            source = old_global or {}
            self.d.registry.register_global_repo(
                repo_name=new,
                alias_name=_global(new),
                repo_url=plan.repo_url,
                index_path=_rewrite(source.get("index_path"), old_dir, new_dir)
                or new_dir,
                enable_temporal=bool(source.get("enable_temporal")),
                temporal_options=source.get("temporal_options"),
            )
            if source.get("enable_scip"):
                self.d.registry.update_enable_scip(_global(new), True)
        if old_row is not None:
            self.d.metadata.remove_repo(old)
        if old_global is not None:
            self.d.registry.unregister_global_repo(_global(old))
        if self.d.cache is not None:
            self.d.cache.pop(old, None)
            self.d.cache.pop(new, None)

    def _satellites(self, plan: RenamePlan) -> Dict[str, str]:
        """Every reference keyed by the alias; each idempotent, each failure
        reported (the rename itself is done: a re-run retries them)."""
        outcome: Dict[str, str] = {}
        for name, move in self.d.satellites:
            try:
                done = move(plan.old, plan.new, plan.repo_url)
                outcome[name] = "ok" if done is None else str(done)
            except Exception as exc:
                logger.error(
                    "Rename '%s' -> '%s': moving %s failed: %s",
                    plan.old,
                    plan.new,
                    name,
                    exc,
                )
                outcome[name] = f"failed: {type(exc).__name__}: {exc}"
        return outcome

    def _cleanup(
        self,
        old: str,
        old_dir: str,
        new_dir: str,
        old_ptr: Optional[Dict[str, Any]],
    ) -> List[str]:
        """Delete what the old alias left; a failure is a reported leftover,
        never a failed rename (everything now names the new alias)."""
        leftovers: List[str] = []
        try:
            if self.d.clone_dirs.exists(old_dir):
                self.d.clone_dirs.remove(old_dir)
        except Exception as exc:
            leftovers.append(f"clone {old_dir}: {type(exc).__name__}: {exc}")
        store = self.d.index_store
        if store is not None and not getattr(store, "is_local", True):
            for store_alias in self._old_store_aliases(old, new_dir, old_ptr):
                try:
                    store.delete_as(store_alias)  # idempotent: 0 when gone
                except Exception as exc:
                    leftovers.append(
                        f"index store alias {store_alias}: {type(exc).__name__}: {exc}"
                    )
        for leftover in leftovers:
            logger.warning("Rename of '%s' left %s", old, leftover)
        return leftovers

    @staticmethod
    def _old_store_aliases(
        old: str, new_dir: str, old_ptr: Optional[Dict[str, Any]]
    ) -> List[str]:
        return _old_store_aliases_for(old, os.path.basename(new_dir), old_ptr)


def _old_store_aliases_for(
    old: str, new: str, old_ptr: Optional[Dict[str, Any]]
) -> List[str]:
    """The old alias's published store aliases: the ones its pointer
    recorded, and the ones derived from its name (all a resumed rename has
    once the pointer is gone) -- never the new alias's own."""
    from code_indexer.utils.index_store import (
        index_store_alias,
        temporal_index_store_alias,
    )

    recorded = [
        str(old_ptr[key])
        for key in (_INDEX_FIELDS[0], _TEMPORAL_FIELDS[0])
        if old_ptr and old_ptr.get(key)
    ]
    derived = [index_store_alias(old), temporal_index_store_alias(old)]
    keep = {index_store_alias(new), temporal_index_store_alias(new)}
    ordered: List[str] = []
    for alias in recorded + derived:
        if alias not in keep and alias not in ordered:
            ordered.append(alias)
    return ordered


class _Locks:
    """Both aliases' write locks, taken in name order, or RenameConflictError."""

    def __init__(self, scheduler: Any, aliases: List[str]) -> None:
        self._scheduler = scheduler
        self._aliases = aliases
        self._held: List[str] = []

    def __enter__(self) -> "_Locks":
        if self._scheduler is None:
            return self
        for alias in self._aliases:
            if not self._scheduler.acquire_write_lock(alias, owner_name=_OWNER):
                self.__exit__(None, None, None)
                raise RenameConflictError(
                    f"The write lock of '{alias}' is held by another operation"
                )
            self._held.append(alias)
        return self

    def __exit__(self, *exc: Any) -> None:
        while self._held:
            alias = self._held.pop()
            try:
                self._scheduler.release_write_lock(alias, owner_name=_OWNER)
            except Exception as error:
                logger.error("Could not release the write lock of %s: %s", alias, error)


def names_in_rename(aliases: Any) -> Set[str]:
    """Both ends of every unfinished rename (old pointers still marked
    ``renaming_to``). The registry reconcile leaves these directories
    alone: one is being copied, the other is about to be deleted."""
    names: Set[str] = set()
    for name, record in aliases.list_records().items():
        target = record.get(RENAMING_TO) if isinstance(record, dict) else None
        if target:
            names.add(str(name).removesuffix(GLOBAL_SUFFIX))
            names.add(str(target))
    return names


# -- satellites (each: (old, new, repo_url) -> a short outcome) ------------


def relink_activated_repos(activated_repo_manager: Any) -> Callable[..., str]:
    """Activated repos and composites derived from *old* now name *new*.
    Their working copies and ``golden`` remotes are left to their next sync."""

    def move(old: str, new: str, _repo_url: Optional[str]) -> str:
        moved = 0
        for found in activated_repo_manager.find_repos_by_golden_alias(old):
            username, user_alias = found["username"], found["user_alias"]
            metadata = activated_repo_manager._load_metadata(username, user_alias)
            if not metadata:
                continue
            if metadata.get("golden_repo_alias") == old:
                metadata["golden_repo_alias"] = new
            members = metadata.get("golden_repo_aliases")
            if isinstance(members, list):
                metadata["golden_repo_aliases"] = [
                    new if member == old else member for member in members
                ]
            activated_repo_manager._save_metadata(username, user_alias, metadata)
            moved += 1
        return f"{moved} relinked"

    return move


def move_group_grants(group_access_manager: Any) -> Callable[..., str]:
    """Every group granted *old* is granted *new*, then *old* is revoked."""

    def move(old: str, new: str, _repo_url: Optional[str]) -> str:
        groups = list(group_access_manager.get_repo_groups(old))
        for group in groups:
            group_access_manager.grant_repo_access(new, group.id, "golden-repo-rename")
            group_access_manager.revoke_repo_access(old, group.id)
        return f"{len(groups)} group(s)"

    return move


def move_description(golden_repos_dir: str) -> Callable[..., str]:
    def move(old: str, new: str, repo_url: Optional[str]) -> str:
        from code_indexer.global_repos.meta_description_hook import on_repo_renamed

        return on_repo_renamed(old, new, golden_repos_dir, repo_url)

    return move


def announce_rename() -> Callable[..., str]:
    """Features keeping state per alias are told (``repository_events``):
    the wiki drops the old alias's rendered pages (re-rendered under the new
    one) and keeps its view counts as history."""

    def move(old: str, new: str, _repo_url: Optional[str]) -> str:
        from code_indexer.server.features.repository_events import (
            publish_repository_renamed,
        )

        return f"{publish_repository_renamed(old, new)} listener(s)"

    return move


def reset_dependency_tracking(metadata: Any) -> Callable[..., str]:
    """The dependency map re-analyses the repository under its new alias."""

    def move(old: str, new: str, _repo_url: Optional[str]) -> str:
        metadata.invalidate_dependency_map_tracking(old)
        metadata.invalidate_dependency_map_tracking(new)
        return "invalidated"

    return move


def deps_from_manager(manager: Any) -> RenameDeps:
    """The rename's collaborators on a running server (GoldenRepoManager)."""
    from code_indexer.global_repos.alias_manager import AliasManager
    from code_indexer.global_repos.global_activation import GlobalActivator
    from code_indexer.global_repos.write_mode_markers import WriteModeMarkers
    from code_indexer.server.startup.index_store_wiring import server_index_store

    golden = str(manager.golden_repos_dir)
    clone_dirs: CloneDirs
    if manager.repositories_are_delegated():
        clone_dirs = DelegatedCloneDirs(
            manager._delegated_working_copies("rename of a golden repository"),
            timeout=manager.resource_config.git_clone_timeout,
        )
    else:
        clone_dirs = LocalCloneDirs()
    markers = WriteModeMarkers(golden)
    satellites: List[Tuple[str, Callable[[str, str, Optional[str]], Any]]] = [
        ("cidx_meta_description", move_description(golden)),
        ("feature_listeners", announce_rename()),
        ("dependency_map_tracking", reset_dependency_tracking(manager._sqlite_backend)),
    ]
    if getattr(manager, "activated_repo_manager", None) is not None:
        satellites.insert(
            0,
            ("activated_repos", relink_activated_repos(manager.activated_repo_manager)),
        )
    if getattr(manager, "group_access_manager", None) is not None:
        satellites.insert(
            1, ("group_access", move_group_grants(manager.group_access_manager))
        )
    return RenameDeps(
        golden_repos_dir=golden,
        metadata=manager._sqlite_backend,
        registry=GlobalActivator(golden).registry,
        aliases=AliasManager(os.path.join(golden, "aliases")),
        clone_dirs=clone_dirs,
        git=manager._git_backend,
        index_store=server_index_store(),
        scheduler=getattr(manager, "_refresh_scheduler", None),
        job_tracker=getattr(manager, "job_tracker", None),
        write_mode_active=markers.exists,
        strip_credentials=manager._strip_credentials_if_delegated,
        satellites=satellites,
        cache=manager.golden_repos,
    )


def submit_rename(
    manager: Any,
    old: str,
    new: str,
    *,
    repo_url: Optional[str],
    branch: Optional[str],
    submitter_username: str,
) -> str:
    """Check the rename here (so a refusal is the caller's error, not a
    failed job), then run it as a background job; the job id. The job takes
    both aliases' write locks and checks everything again before moving."""
    from code_indexer.server.jobs.exceptions import MaintenanceModeError
    from code_indexer.server.services.maintenance_service import (
        get_maintenance_state,
    )

    if get_maintenance_state().is_maintenance_mode():
        raise MaintenanceModeError()
    renamer = GoldenRepoRenamer(deps_from_manager(manager))
    plan = renamer.plan(old, new, repo_url, branch)
    renamer.check_not_busy(plan, own_job=None)

    def worker() -> Dict[str, Any]:
        with renamer.locked(plan):
            checked = renamer.plan(old, new, repo_url, branch)
            renamer.check_not_busy(checked, own_job=_current_job_id(manager, checked))
            done = renamer.execute(checked)
        return {
            "success": True,
            "alias": checked.new,
            "message": f"Golden repository '{checked.old}' renamed to '{checked.new}'",
            "rename": done,
        }

    return str(
        manager.background_job_manager.submit_job(
            operation_type=RENAME_OPERATION,
            func=worker,
            submitter_username=submitter_username,
            is_admin=True,
            repo_alias=plan.old,
        )
    )


def _current_job_id(manager: Any, plan: RenamePlan) -> Optional[str]:
    """This rename's own job (registered under the old alias)."""
    tracker = getattr(manager, "job_tracker", None)
    if tracker is None:
        return None
    job = tracker.active_job_id(RENAME_OPERATION, plan.old)
    return str(job) if job else None
