"""Crash-safe per-shard relocation for legacy temporal indexes.

Issue #1548 review blockers 1/2/3/7/8/9 (plus a third-round critical exploit
fix and blockers 2/3/4/5 from the follow-up review). Locked collision
policy: "new shard wins". If the fixed-root shard ALREADY holds real,
verified data -- whether that data matches or diverges from the legacy
source -- the fixed-root copy is authoritative and is NEVER overwritten,
and its legacy counterpart is ALSO left untouched (never deleted) in this
same pass. The divergence-or-coincidental-match is counted as a collision
and resolution is deferred to a later, separate manual/cleanup pass.

The one exception: if the fixed-root data was itself placed there by THIS
migration mechanism in a prior pass, it is legitimately "already
complete" -- a genuine multi-pass migration (relocate now, clean up
later) must still be able to converge. Proof of that provenance is a
CONTENT-BOUND digest marker (see ``verification.PROVENANCE_MARKER_NAME``),
never a bare sentinel file: a sentinel alone could be coincidentally or
accidentally present at the target path without this migration having put
it there, which would falsely authorize treating foreign data as "our own
prior work". A legacy shard is deleted ONLY after its own migrated copy
has been read back and field-for-field verified identical to the legacy
source -- "something exists at the target path" is never sufficient.

Third-round critical exploit fix: a digest match alone was proven
exploitable when the digest covered only logical point records (see
``verification.manifest_digest``'s docstring) -- an incomplete target
(missing ``hnsw_index.bin``/``collection_meta.json``) could still satisfy
a forged/stale marker whose digest was computed the old, records-only
way. ``manifest_digest`` now also covers the full file tree, closing that
gap structurally; ``_target_is_structurally_complete`` below is kept as an
INDEPENDENT, direct completeness check on top of the digest comparison --
"already_complete" must never be granted to a target that is not, on its
own terms, a real, queryable shard.

Round-5 residual TOCTOU risk (documented, not claimed fully closed): the
delete sequence below closes every window a single-process test can
reproduce, but a genuinely concurrent external actor could still
interleave a mutation between the FINAL recheck and ``os.rename`` itself.
Production closes this via the caller-held write lock
(``locking.guarded_by_refresh_lock``), which excludes every LEGITIMATE
writer; it does not exclude an illegitimate actor with independent
filesystem access, outside this module's threat model.

Round-6 fixes (sixth adversarial review round), both critical:

1. HNSW completeness (``verification.hnsw_index_covers_all_logical_points``)
   previously trusted ``collection_meta.json``'s ``id_mapping`` field --
   attacker-writable metadata -- as proof of which point ids an HNSW index
   covers. It now requires the caller to pass the REAL label set loaded
   from the actual ``hnsw_index.bin`` binary (``index.get_ids_list()``);
   ``id_mapping`` is used only to translate those real labels into names,
   never to invent which ones exist.
2. The delete sequence (``_verify_and_delete_source`` /
   ``_delete_source_atomically``) now re-verifies the TARGET's own
   structural completeness immediately before AND immediately after the
   atomic rename-to-trash -- not just the source's symlink state. On a
   post-rename failure, the source is RESTORED from trash (a genuine
   recovery path) rather than left permanently deleted against a
   corrupted target.

Round-6 residual TOCTOU risk (still not claimed fully closed, narrowed
further): the window that matters is now strictly between the post-rename
target re-verification and the final ``shutil.rmtree(trash_path)`` -- an
external actor would need to corrupt the target in that specific gap to
still cause data loss, since anything before it is now caught and
recovered. As with round-5, production relies on the caller-held write
lock to exclude every legitimate writer; an illegitimate actor with
independent, concurrent filesystem access remains outside this module's
threat model and has not been eliminated, only narrowed.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol

from code_indexer.services.temporal.temporal_collection_naming import (
    LEGACY_TEMPORAL_COLLECTION,
)
from code_indexer.services.temporal.temporal_row_existence import (
    temporal_shard_has_committed_rows,
)

from .verification import (
    PROVENANCE_MARKER_NAME,
    VerificationError,
    hnsw_index_covers_all_logical_points,
    manifest_digest,
    peek_one_vector_dimension,
    verify_shard_copy,
    verify_source_subset_of_target,
)

logger = logging.getLogger(__name__)

_SHARD_PREFIX = "code-indexer-temporal-"
_STAGING_INFIX = ".staging-"
_PENDING_DELETE_INFIX = ".pending-delete-"

# Blocker 1: written into a shard's staging directory (and therefore into
# the atomically-published target) ONLY by this module's own _publish(),
# holding the SOURCE'S manifest digest at publish time (see
# verification.manifest_digest). A shard is only ever treated as
# "already_complete" on a later pass when: the marker is present, AND its
# recorded digest equals the CURRENT legacy source's digest, AND it equals
# the target's own current digest -- i.e. content-bound proof that neither
# side has changed since a verified publish, not merely "a file with this
# name happens to exist". This is what makes the marker unforgeable by an
# unrelated process that merely creates a same-named file.
#
# Safe by construction against verify_shard_copy()/manifest_digest(): the
# logical-record manifest builders (verification.py's
# _json_manifest/_chunks_manifest) enumerate ONLY "vector_*.json" files and
# a "chunks.db" SQLite store, and the structural manifest builder
# explicitly excludes this exact filename -- so this marker file can never
# be mistaken for a data record or corrupt a digest, including its own.
_PROVENANCE_MARKER_NAME = PROVENANCE_MARKER_NAME

# Third-round review, blocker 4: a repo-level (not per-shard) durable proof
# that this migration mechanism previously observed and fully relocated
# every legacy shard it discovered for this repo, written under
# fixed_root (server-owned, outlives legacy shard deletion). This is what
# lets metadata-scope cleanup converge in a LATER pass that sees zero
# legacy shard directories -- either because none ever existed (never
# touched, permanently keep) or because they were all genuinely migrated
# and deleted already (safe to finish cleaning up). See
# ``_repo_relocation_previously_completed`` for how the ambiguity between
# those two cases is resolved.
_REPO_RELOCATION_COMPLETE_MARKER_NAME = ".legacy-migration-repo-complete"

# Bar this codebase already uses elsewhere (temporal_status.py's
# ``is_queryable``) for "this shard has a working HNSW index" -- reused
# here as one of the direct completeness checks, rather than inventing a
# second definition of "queryable".
_HNSW_INDEX_FILENAME = "hnsw_index.bin"
_COLLECTION_META_FILENAME = "collection_meta.json"


class LockLossCheck(Protocol):
    """Minimal structural surface this module needs from a caller's
    write-lock-loss signal (``locking.LockLossSignal`` in production).

    Typed here (rather than importing ``locking.py``, a server-startup-
    heavy module) so callers get real structural type checking instead of
    a bare ``Any`` -- mirrors ``TemporalMetadataScopeBackend`` immediately
    below, this module's existing pattern for decoupled backend typing.
    """

    def is_lost(self) -> bool: ...

    def raise_if_lost(self) -> None: ...


def _abort_if_lock_lost(lock_lost_check: Optional[LockLossCheck]) -> None:
    """Raise if the caller's write-lock heartbeat signalled the lock may
    no longer be held (Issue #1548 round-8, Issue 1).

    Checked immediately before every destructive filesystem/metadata
    operation in this module -- a renewal failure must abort BEFORE
    acting, never merely be logged while destructive work proceeds
    regardless. A ``None`` check (the default for every caller outside
    the guarded production path, e.g. tests exercising this module
    directly) is a no-op.
    """
    if lock_lost_check is not None:
        lock_lost_check.raise_if_lost()


class TemporalMetadataScopeBackend(Protocol):
    """Minimal surface this module needs from a temporal metadata backend.

    Both `TemporalMetadataSqliteBackend` and `TemporalMetadataPostgresBackend`
    satisfy this structurally -- typed here (rather than importing either
    concrete class, which would pull psycopg/fastapi into this module) so
    ``metadata_backend_factory``'s return value gets real type checking
    instead of a bare ``object`` + ``# type: ignore``.
    """

    def copy_collection_scope(
        self,
        target_collection_path: Path,
        *,
        pre_commit_check: Optional[Callable[[], None]] = None,
    ) -> None: ...

    def delete_collection_scope(self) -> None: ...

    def count_entries(self) -> int: ...

    def content_digest(self) -> str: ...


@dataclass(frozen=True)
class MigrationResult:
    published: int = 0
    already_complete: int = 0
    deleted: int = 0
    collisions: int = 0
    failed: int = 0


def _fsync_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file():
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        elif path.is_dir():
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _has_verified_data(path: Path) -> bool:
    """Return True iff *path* holds real, committed temporal rows.

    Blocker 1 fix: replaces a directory-emptiness check (``any(path.
    iterdir())``, which was True for a directory containing nothing but a
    stray ``collection_meta.json``) with this codebase's designated
    layout-aware data-existence primitive. ``on_error="raise"`` because this
    decision gates destructive cleanup -- "cannot verify" must never be
    silently treated as "no data, safe to proceed" (Bug #1529 finding #5).
    """
    if not path.is_dir():
        return False
    return bool(temporal_shard_has_committed_rows(path, on_error="raise"))


def _contains_symlink(root: Path) -> bool:
    """True if *root* itself, or anything beneath it, is a symlink.

    Issue #1548 round-4 exploit fix: a reproduced exploit planted
    ``hnsw_index.bin``/``collection_meta.json`` at a fixed-root target as
    SYMLINKS pointing back into the legacy source directory itself, then
    let ``.is_file()``-based checks follow them and hash the source's own
    bytes -- trivially "matching" without the target ever holding
    independent data (its files went dangling the moment the legacy source
    was later deleted). ``os.path.islink()`` never follows, so it cannot be
    fooled the same way. Any symlink anywhere in the tree makes the whole
    tree untrustworthy for provenance purposes.

    Deliberately walks via ``os.walk(followlinks=False)`` rather than
    ``Path.rglob("*")``: on Python versions predating 3.13, ``rglob``'s
    ``**`` recursion follows symlinked directories, so a symlink cycle
    (e.g. a directory symlinked to one of its own ancestors) would recurse
    forever. ``os.walk`` with ``followlinks=False`` (its default) reports a
    symlinked directory's NAME in its parent's ``dirnames`` -- caught by
    the check below -- without ever descending into it, so a cycle is
    structurally impossible to reach.

    Fails CLOSED on any traversal error (permission denied, race with a
    concurrent delete, etc.): an ``onerror`` handler re-raises so a subtree
    that couldn't be listed is never silently treated as "no symlinks
    found here" -- this decision gates destructive cleanup, so "cannot
    verify" must never collapse into "safe to proceed" (same principle as
    ``_has_verified_data``'s ``on_error="raise"``).
    """
    if root.is_symlink():
        return True
    if not root.is_dir():
        return False

    def _raise_on_walk_error(exc: OSError) -> None:
        raise exc

    try:
        for dirpath, dirnames, filenames in os.walk(
            root, followlinks=False, onerror=_raise_on_walk_error
        ):
            for name in (*dirnames, *filenames):
                if os.path.islink(os.path.join(dirpath, name)):
                    return True
    except OSError:
        logger.exception(
            "failed to fully traverse %s while checking for symlinks -- "
            "treating as untrustworthy (fail closed)",
            root,
        )
        return True
    return False


def _collection_meta_is_valid(target: Path) -> bool:
    """Minimal content-semantic validation of ``collection_meta.json``.

    Issue #1548 round-4 exploit fix: a target whose ``collection_meta.json``
    merely EXISTS (previously the whole check) is not proof of anything --
    a reproduced exploit planted a zero-byte/invalid-JSON file there and
    the old existence-only check still passed. Requires the file to parse
    as valid JSON and be a non-empty object -- a genuine shard's metadata
    is never an empty dict.
    """
    meta_path = target / _COLLECTION_META_FILENAME
    if not meta_path.is_file() or meta_path.is_symlink():
        return False
    try:
        with meta_path.open(encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    return isinstance(data, dict) and bool(data)


def _hnsw_index_structurally_valid(target: Path) -> bool:
    """Non-empty, hnswlib-loadable, and its ACTUAL contents (never
    ``collection_meta.json``'s ``id_mapping`` alone) cover EVERY logical
    point record present. Never raises.

    Issue #1548 round-6 exploit fix (CRITICAL): the round-5 fix asked
    "does ``id_mapping`` claim to cover every logical point" -- but
    ``id_mapping`` is JSON metadata, not the actual content of
    ``hnsw_index.bin``. Codex reproduced forging ``id_mapping`` to claim 2
    point ids while the REAL, loaded binary held exactly 1 element
    (``get_current_count() == 1``, ``get_ids_list() == [0]``); the old
    check still passed, since it read the ids straight out of the JSON
    metadata rather than the binary. This now loads the real index and
    passes its OWN ``get_ids_list()`` -- the ground truth of what is
    actually stored -- into ``hnsw_index_covers_all_logical_points``,
    which uses ``id_mapping`` only to translate those REAL labels into
    point-id names, never to invent which labels exist.
    """
    hnsw_path = target / _HNSW_INDEX_FILENAME
    try:
        if not hnsw_path.is_file() or hnsw_path.is_symlink():
            return False
        if hnsw_path.stat().st_size == 0:
            return False
    except OSError:
        return False
    try:
        dim = peek_one_vector_dimension(target)
    except Exception:
        logger.exception("failed to peek vector dimension at %s", target)
        return False
    if not dim:
        return False
    try:
        # Lazy import (Bug #1468 pattern): hnswlib is heavy/optional.
        from code_indexer.storage.hnsw_index_manager import HNSWIndexManager

        manager = HNSWIndexManager(vector_dim=dim, space="cosine")
        index = manager.load_index(target)
        if index is None or index.get_current_count() <= 0:
            return False
        actual_label_ids = set(index.get_ids_list())
    except Exception:
        logger.exception("hnsw_index.bin at %s failed to load", target)
        return False
    return hnsw_index_covers_all_logical_points(target, actual_label_ids)


def _target_is_structurally_complete(target: Path) -> bool:
    """Direct, independent proof that *target* is a real, complete,
    queryable shard -- not merely "a digest happened to match".

    Third-round critical exploit fix: kept deliberately independent of
    ``manifest_digest``'s now-expanded structural coverage.

    Round-4 exploit fix: strengthened twice over. (1) refuses a target
    containing ANY symlink (``_contains_symlink``) -- a digest match alone
    can be trivially satisfied by a symlink pointing back at the source,
    so this is checked FIRST and short-circuits everything else. (2) both
    ``collection_meta.json`` and ``hnsw_index.bin`` now require genuine
    content-semantic validation (``_collection_meta_is_valid``,
    ``_hnsw_index_structurally_valid``) rather than bare file existence --
    a zero-byte/invalid-content file digest-matching on both sides
    previously passed. Real committed rows (``_has_verified_data``) is
    still required. A target failing any of these is never eligible for
    "already_complete", regardless of what any marker file claims.
    """
    if _contains_symlink(target):
        return False
    return (
        _collection_meta_is_valid(target)
        and _hnsw_index_structurally_valid(target)
        and _has_verified_data(target)
    )


def _content_matches_expected_source(expected_source: Path, target: Path) -> bool:
    """True iff *target*'s content is field-for-field identical to
    *expected_source* -- a trusted source of truth (the just-trashed
    legacy source, or a surviving orphan trash directory).

    Issue #1548 round-7 exploit fix: ``_target_is_structurally_complete``
    proves the target LOOKS like a real, complete shard -- right files
    present, HNSW genuinely loadable, id set matching its OWN records --
    but says nothing about whether that data is the SAME data
    *expected_source* holds. Codex reproduced replacing the target with a
    DIFFERENT, fully valid, structurally-complete shard (same shape,
    different vector content) at both the post-rename recovery check and
    the orphan-trash-cleanup revalidation, and both accepted it. This
    reuses ``verify_shard_copy`` -- the SAME fresh, independent,
    field-for-field record comparison the pre-delete verification already
    relies on -- rather than reimplementing content comparison a second
    time. Never raises: any mismatch or unexpected failure is treated as
    "does not match" (fail closed), since this gates a destructive
    decision.
    """
    try:
        verify_shard_copy(expected_source, target)
        return True
    except VerificationError:
        return False
    except Exception:
        logger.exception(
            "failed to compare content of %s against %s -- treating as "
            "not matching (fail closed)",
            expected_source,
            target,
        )
        return False


def _read_marker_digest(marker_path: Path) -> Optional[str]:
    if not marker_path.is_file():
        return None
    try:
        return marker_path.read_text().strip()
    except OSError:
        logger.exception("failed to read provenance marker %s", marker_path)
        return None


def _cleanup_orphaned_staging_dirs(
    target_parent: Path, lock_lost_check: Optional[LockLossCheck] = None
) -> int:
    """Remove staging directories orphaned by a crash before publish.

    Blocker 8/review-round-3 blocker 3: a crash between ``shutil.
    copytree`` and the atomic rename into place leaves a
    ``.{name}.staging-{uuid}`` directory permanently orphaned -- nothing
    previously looked for it on a later run. Safe to sweep unconditionally
    here because the caller (scheduler/CLI) holds the repo's write lock
    for the whole pass, so no other process can be concurrently writing a
    staging directory for this same target_parent.

    Returns the number of entries that failed to list/remove -- these were
    previously logged (ERROR) and silently dropped, never surfaced in
    ``MigrationResult.failed``. The caller folds the return value into the
    pass's failure count so an operator can actually see that disk space
    is being leaked, rather than the pass looking clean.

    Issue #1548 round-9: checked (non-raising, via ``is_lost()``) before
    EACH individual ``shutil.rmtree`` -- this sweep can iterate many
    orphaned directories, so a lock lost partway through must stop
    deleting the remainder rather than run to completion regardless. An
    entry left in place this way is NOT counted as a failure: leaving an
    orphan for a later pass to retry is a safe deferral, not an error.
    """
    if not target_parent.is_dir():
        return 0
    try:
        entries = list(target_parent.iterdir())
    except OSError:
        logger.exception(
            "failed to list %s while sweeping orphaned staging directories",
            target_parent,
        )
        return 1
    failures = 0
    for entry in entries:
        if not entry.is_dir():
            continue
        if not entry.name.startswith(".") or _STAGING_INFIX not in entry.name:
            continue
        if lock_lost_check is not None and lock_lost_check.is_lost():
            logger.error(
                "temporal legacy migration: write lock may have been "
                "lost -- leaving orphaned staging directory %s (and any "
                "remaining ones) in place rather than deleting it",
                entry,
            )
            break
        logger.warning("removing orphaned migration staging directory: %s", entry)
        try:
            shutil.rmtree(entry)
        except OSError:
            logger.exception(
                "failed to remove orphaned migration staging directory: %s", entry
            )
            failures += 1
    return failures


def _publish(
    source: Path,
    target: Path,
    pre_publish_hook: Optional[Callable[[], None]],
    lock_lost_check: Optional[LockLossCheck] = None,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}{_STAGING_INFIX}{uuid.uuid4().hex}"
    try:
        shutil.copytree(source, staging)
        verify_shard_copy(source, staging)
        # Blocker 1: the provenance marker records the digest of what was
        # just verified, written only AFTER verification succeeds, and is
        # itself fsync'd as part of the staged tree below -- a crash
        # before this point leaves no marker, so a later pass correctly
        # refuses to trust the (incomplete or unverified) target.
        (staging / _PROVENANCE_MARKER_NAME).write_text(manifest_digest(staging))
        _fsync_tree(staging)
        if pre_publish_hook is not None:
            pre_publish_hook()
        # Issue #1548 round-8, Issue 1: the write lock may have been lost
        # since this pass began -- check immediately before the first
        # destructive mutation below (clearing a stray target / the
        # publish rename itself).
        _abort_if_lock_lost(lock_lost_check)
        if target.exists():
            # Caller only reaches _publish() when target has already been
            # confirmed to hold no verified data -- clear any stray/partial
            # artifact (e.g. a bare collection_meta.json) before the rename.
            shutil.rmtree(target)
        staging.rename(target)
        fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        logger.info("published legacy temporal shard %s -> %s", source, target)
    finally:
        # Issue #1548 round-9: this scratch directory belongs solely to
        # THIS call's own (possibly incomplete) publish attempt -- if the
        # lock may have been lost, leave it for a later pass's
        # ``_cleanup_orphaned_staging_dirs`` sweep rather than deleting it
        # here. Never raises from a `finally` block (which could mask an
        # in-flight exception); it simply defers the cleanup.
        if staging.exists():
            if lock_lost_check is not None and lock_lost_check.is_lost():
                logger.warning(
                    "temporal legacy migration: write lock may have been "
                    "lost -- leaving staging directory %s in place for a "
                    "later orphan sweep instead of deleting it now",
                    staging,
                )
            else:
                shutil.rmtree(staging)


def _classify_existing_target(source: Path, target: Path) -> str:
    """Return "already_complete" or "collision" for a target that already
    holds verified data (Blocker 1's "new shard wins" policy).

    "already_complete" requires: a provenance marker at *target* whose
    digest matches the CURRENT legacy source (proves THIS mechanism
    published FOR this exact source); ``_target_is_structurally_complete``
    (a real, queryable shard on its own terms); and EITHER the marker also
    still matches the target's current digest (steady state) OR -- Issue
    #1580 -- the source's originally-verified data is still provably
    preserved as a SUBSET of the target's current data (see
    ``_target_converges_via_additive_evolution``). Bug #1529 made the
    fixed root the live, continuously-refreshed write destination, so a
    target's digest legitimately moves on after publish; requiring it to
    stay frozen forever made every migrated shard an unresolvable
    collision on its very next refresh. Anything short of this -- no
    marker, a mismatched marker, lost/altered data, or an incomplete
    target -- is a collision, even on a byte-for-byte coincidental match.
    """
    # Issue #1548 round-4 exploit fix: symlinks resolve THROUGH to their
    # target's bytes, so a digest match alone proves nothing. Checked
    # first, unconditionally.
    if _contains_symlink(source) or _contains_symlink(target):
        logger.warning(
            "temporal shard collision: symlink detected in %s or %s -- "
            "refusing to trust either tree for provenance, never "
            "classifying as already_complete",
            source,
            target,
        )
        return "collision"
    marker_digest = _read_marker_digest(target / _PROVENANCE_MARKER_NAME)
    target_complete = _target_is_structurally_complete(target)
    provably_converged = (
        marker_digest is not None
        and marker_digest == manifest_digest(source)
        and target_complete
        and (
            marker_digest == manifest_digest(target)
            or _target_converges_via_additive_evolution(source, target)
        )
    )
    if provably_converged:
        return "already_complete"
    logger.warning(
        "temporal shard collision: fixed root %s already holds data that "
        "is not verifiably a prior publish of this migration mechanism "
        "(marker_digest=%s, structurally_complete=%s) -- destination "
        "wins, both sides left untouched pending a later, separate "
        "cleanup pass",
        target,
        marker_digest,
        target_complete,
    )
    return "collision"


def _target_converges_via_additive_evolution(source: Path, target: Path) -> bool:
    """Issue #1580: True iff *target*'s digest no longer matches the
    publish-time marker but *source*'s originally-verified data is still
    fully preserved (additively) within *target*'s current data -- new
    records only, nothing lost or altered. See
    ``_classify_existing_target`` for why this is safe: the legacy source
    is dead-end data superseded by the fixed root's own ongoing refreshes.
    """
    try:
        verify_source_subset_of_target(source, target)
    except VerificationError as exc:
        logger.debug(
            "temporal legacy migration: target %s does not (yet) preserve "
            "all of source %s's originally-verified data -- %s",
            target,
            source,
            exc,
        )
        return False
    return True


def _verify_and_delete_source(
    source: Path,
    target: Path,
    pre_delete_hook: Optional[Callable[[], None]],
    post_rename_hook: Optional[Callable[[], None]] = None,
    lock_lost_check: Optional[LockLossCheck] = None,
) -> None:
    """Check-verify-recheck-delete sequence for one shard's legacy source.

    Round-5 exploit 2 fix (TOCTOU): symlink check, ``verify_shard_copy``,
    ``pre_delete_hook`` (test seam), a SECOND symlink check immediately
    before delete -- closes a symlink swapped in DURING
    ``verify_shard_copy``.

    Round-6 exploit 2 fix (CRITICAL): round-5 protected only the SOURCE.
    It never re-verified the TARGET itself, so a concurrent actor
    mutating the target (e.g. zeroing ``hnsw_index.bin``) after this point
    went uncaught. Target completeness is now re-checked here immediately
    before the point of no return, and ``_delete_source_atomically``
    re-checks it ONE MORE time right after the source is renamed to trash
    -- the narrowest achievable window -- restoring the source on failure
    instead of leaving it permanently gone against a corrupted target.
    ``post_rename_hook`` is a test-only seam firing in that exact window.
    """
    if _contains_symlink(source) or _contains_symlink(target):
        raise VerificationError(
            f"refusing to delete legacy temporal shard {source}: a "
            f"symlink was detected in {source} or {target} immediately "
            f"before deletion"
        )
    # Blocker 1 (relaxed by Issue #1580): re-verify immediately before
    # destroying the legacy copy that source's data is still fully
    # preserved in target -- never trust the branch above alone. Subset
    # (not exact-equality) verification: a legitimately-evolved target
    # (Bug #1529's in-place refreshes) may legally hold MORE than source
    # ever did; it may never hold LESS or DIFFERENT data for a record
    # source already has.
    verify_source_subset_of_target(source, target)
    if pre_delete_hook is not None:
        pre_delete_hook()
    # Round-5 exploit 2 fix: recheck AGAIN, AFTER the verification above --
    # this is what actually closes Codex's repro, since the exploit
    # planted its symlink DURING that verification's own execution, after
    # the check above already ran clean.
    if _contains_symlink(source) or _contains_symlink(target):
        raise VerificationError(
            f"refusing to delete legacy temporal shard {source}: a "
            f"symlink appeared in {source} or {target} during "
            f"verification -- aborting before any destructive action"
        )
    # Round-6 exploit 2 fix: the target -- not just the source -- must be
    # re-proven structurally complete immediately before the point of no
    # return. A digest match only compares logical point records; it says
    # nothing about whether the target's HNSW/metadata are still intact.
    if not _target_is_structurally_complete(target):
        raise VerificationError(
            f"refusing to delete legacy temporal shard {source}: target "
            f"{target} is not structurally complete immediately before "
            f"deletion"
        )
    # Round-7 exploit fix: snapshot the target's digest RIGHT NOW, while it
    # is still trusted -- this is what _delete_source_atomically compares
    # against after the rename, so a target SWAPPED during that window is
    # caught even though a swapped-in shard can be independently valid.
    expected_target_digest = manifest_digest(target)
    _delete_source_atomically(
        source,
        target,
        expected_target_digest,
        post_rename_hook,
        lock_lost_check=lock_lost_check,
    )
    logger.info("deleted verified legacy temporal shard %s", source)


def _process_one_shard(
    source: Path,
    target: Path,
    *,
    relocation_enabled: bool,
    cleanup_authorized: bool,
    pre_publish_hook: Optional[Callable[[], None]],
    pre_delete_hook: Optional[Callable[[], None]] = None,
    post_rename_hook: Optional[Callable[[], None]] = None,
    lock_lost_check: Optional[LockLossCheck] = None,
) -> str:
    """Migrate/verify/cleanup one shard. Returns one of: "published",
    "already_complete", "collision", "skipped", "deleted" (deleted implies
    one of the first two also happened, tracked by the caller). See
    ``_verify_and_delete_source`` for the destructive-delete sequence.
    """
    if _has_verified_data(target):
        outcome = _classify_existing_target(source, target)
        if outcome == "collision":
            return "collision"
    elif relocation_enabled:
        _publish(source, target, pre_publish_hook, lock_lost_check=lock_lost_check)
        outcome = "published"
    else:
        return "skipped"

    if cleanup_authorized:
        # Issue #1548 round-5 exploit 1 fix (fresh-publish gap): the
        # "already exists at target" reclassification path above always
        # ran _target_is_structurally_complete() before authorizing
        # deletion, but a FRESH publish in the same call as
        # cleanup_authorized never did -- verify_shard_copy only compares
        # logical point records, so an incomplete/corrupt LEGACY SOURCE
        # (e.g. an HNSW index missing points) was copied verbatim and the
        # source then deleted regardless. Gated here (not unconditionally
        # after publish) so a relocation-only pass (cleanup_authorized
        # False) is unaffected -- only the destructive decision requires
        # completeness.
        if outcome == "published" and not _target_is_structurally_complete(target):
            raise VerificationError(
                f"published shard at {target} is not structurally complete "
                f"immediately after publish (source data itself may be "
                f"incomplete or corrupt) -- refusing to authorize legacy "
                f"source deletion"
            )
        _verify_and_delete_source(
            source,
            target,
            pre_delete_hook,
            post_rename_hook,
            lock_lost_check=lock_lost_check,
        )
    return outcome


def _run_shard_pass(
    shards: List[Path],
    fixed_root: Path,
    *,
    relocation_enabled: bool,
    cleanup_authorized: bool,
    pre_publish_hook: Optional[Callable[[], None]],
    pre_delete_hook: Optional[Callable[[], None]] = None,
    post_rename_hook: Optional[Callable[[], None]] = None,
    lock_lost_check: Optional[LockLossCheck] = None,
) -> Dict[str, int]:
    """Process every shard, isolating per-shard failures (Blocker 8)."""
    counts = {
        "published": 0,
        "already_complete": 0,
        "deleted": 0,
        "collision": 0,
        "failed": 0,
    }
    for index, source in enumerate(shards):
        # Issue #1548 round-8, Issue 1: once the write lock may have been
        # lost, abort the CURRENT shard AND every subsequent one in this
        # run without attempting any further destructive work -- a
        # renewal failure must stop the pass, not merely fail one shard
        # while the loop keeps going.
        if lock_lost_check is not None and lock_lost_check.is_lost():
            remaining = len(shards) - index
            counts["failed"] += remaining
            logger.error(
                "temporal legacy migration: write lock may have been "
                "lost -- aborting %d remaining shard(s) for this repo "
                "without attempting any further destructive work",
                remaining,
            )
            break
        target = fixed_root / source.name
        try:
            outcome = _process_one_shard(
                source,
                target,
                relocation_enabled=relocation_enabled,
                cleanup_authorized=cleanup_authorized,
                pre_publish_hook=pre_publish_hook,
                pre_delete_hook=pre_delete_hook,
                post_rename_hook=post_rename_hook,
                lock_lost_check=lock_lost_check,
            )
        except Exception:
            counts["failed"] += 1
            logger.exception("temporal legacy migration failed for shard %s", source)
            continue

        if outcome == "collision":
            counts["collision"] += 1
            continue
        if outcome == "skipped":
            continue
        counts[outcome] += 1
        if not source.exists():
            counts["deleted"] += 1
    return counts


def _metadata_scope_relocation_verified(
    legacy_meta_path: Path,
    fixed_meta_path: Path,
    metadata_backend_factory: Callable[[Path], TemporalMetadataScopeBackend],
) -> bool:
    """Genuine, content-bound proof that the fixed-root scope holds the
    SAME rows as the legacy scope -- never merely "some rows exist at the
    destination".

    Issue #1548 round-4 exploit 2 fix: replaces
    ``_fixed_metadata_scope_has_data``'s ``count_entries() > 0`` check,
    which Codex reproduced as exploitable -- a forged repo-level
    relocation marker plus UNRELATED (but coincidentally non-empty) rows
    already sitting at the fixed root satisfied that check even though
    zero shards, and zero metadata rows, were ever actually verified or
    migrated by this mechanism. This function instead compares
    ``content_digest()`` (a genuine hash of every row's content) between
    the LEGACY scope -- real, unforgeable production data this pass has
    not yet touched -- and the FIXED scope. Only an EXACT content match
    proves "the same rows were relocated"; a coincidental non-empty
    destination with different content fails this check regardless of any
    marker file's claims.

    ``_sync_metadata_scope`` only reaches this function when
    ``legacy_meta_path.is_dir()`` is already True (it returns early
    otherwise), so the legacy scope is always available here to compare
    against -- there is no code path where this function is asked to
    authorize deletion of a legacy scope it cannot also read.
    """
    if not fixed_meta_path.is_dir() or not legacy_meta_path.is_dir():
        return False
    try:
        fixed_backend = metadata_backend_factory(fixed_meta_path)
        fixed_count = fixed_backend.count_entries()
        fixed_digest = fixed_backend.content_digest()
        legacy_digest = metadata_backend_factory(legacy_meta_path).content_digest()
    except Exception:
        logger.exception(
            "failed to compare legacy/fixed temporal metadata scopes (%s / "
            "%s) before authorizing legacy cleanup -- refusing to delete",
            legacy_meta_path,
            fixed_meta_path,
        )
        return False
    digests_match = legacy_digest == fixed_digest
    if fixed_count <= 0 or not digests_match:
        logger.warning(
            "temporal legacy migration: refusing to delete metadata scope "
            "%s -- %s does not verifiably hold the SAME rows "
            "(fixed_count=%d, digest_match=%s)",
            legacy_meta_path,
            fixed_meta_path,
            fixed_count,
            digests_match,
        )
        return False
    return True


def _make_pre_commit_lock_check(
    lock_lost_check: Optional[LockLossCheck],
) -> Optional[Callable[[], None]]:
    """Build a narrow, commit-point lock-loss check callback for a
    backend's ``pre_commit_check`` hook (Issue #1548 round-10, Finding 1).

    ``None`` when there is no lock-loss signal to check -- mirrors every
    other ``None``-is-a-no-op convention in this module. The returned
    callable is passed straight through to
    ``TemporalMetadataScopeBackend.copy_collection_scope``'s
    ``pre_commit_check`` parameter, which the backend invokes immediately
    before its own commit, inside the same transaction -- so a lock lost
    DURING the copy (not just before it started) still aborts before the
    write becomes durable.
    """
    if lock_lost_check is None:
        return None

    def _check() -> None:
        lock_lost_check.raise_if_lost()

    return _check


def _copy_metadata_scope_if_safe(
    legacy_meta_path: Path,
    fixed_meta_path: Path,
    metadata_backend_factory: Callable[[Path], TemporalMetadataScopeBackend],
    *,
    relocation_enabled: bool,
    withhold: bool,
    lock_lost_check: Optional[LockLossCheck] = None,
) -> bool:
    """Attempt the metadata-scope copy. Returns True iff it was attempted
    and failed (Blocker 6). Skips entirely (returns False, no attempt) when
    relocation is disabled, ``withhold`` is True, or the write lock may
    have been lost.

    Review-round-3 blocker 2: ``withhold`` must be True whenever this pass
    detected EITHER a shard collision OR a shard FAILURE (not collision
    alone) -- a failed shard copy is just as untrustworthy a signal as a
    collision: proceeding to copy the shared metadata scope in either case
    would let ``INSERT OR REPLACE`` silently overwrite rows belonging to
    whatever independently produced the collision, or rows this pass never
    actually got to verify because the shard copy itself blew up.

    Issue #1548 round-9: a lock-loss skip is treated exactly like
    ``withhold`` -- a refused attempt, never counted as a failure. This
    closes the "metadata copying must check lock-loss before proceeding"
    completeness gap Codex's ninth-round review identified as missing.

    Issue #1548 round-10, Finding 1: checking ``is_lost()`` ONCE before
    this call starts is not enough -- the backend's own transaction
    (SQLite ``executemany`` / PostgreSQL ``INSERT ... ON CONFLICT``) can
    take real time for a large scope. A ``pre_commit_check`` built from
    the SAME ``lock_lost_check`` is threaded into ``copy_collection_
    scope`` so the backend itself rechecks immediately before its commit
    and rolls back instead of committing if the lock was lost DURING the
    write. If that recheck fires, this function classifies it exactly
    like the entry-level check above -- a refused, non-failure deferral,
    determined by re-consulting ``lock_lost_check.is_lost()`` after the
    exception rather than by matching an exception type (any exception
    the backend raises after a genuine lock loss is treated the same
    way, regardless of its concrete type).
    """
    if not relocation_enabled:
        return False
    if withhold:
        logger.warning(
            "temporal legacy migration: withholding metadata scope copy "
            "%s -> %s because at least one shard collision or failure was "
            "detected this pass",
            legacy_meta_path,
            fixed_meta_path,
        )
        return False
    if lock_lost_check is not None and lock_lost_check.is_lost():
        logger.error(
            "temporal legacy migration: write lock may have been lost -- "
            "refusing to copy metadata scope %s -> %s",
            legacy_meta_path,
            fixed_meta_path,
        )
        return False
    try:
        metadata_backend_factory(legacy_meta_path).copy_collection_scope(
            fixed_meta_path,
            pre_commit_check=_make_pre_commit_lock_check(lock_lost_check),
        )
        logger.info(
            "copied temporal metadata scope %s -> %s",
            legacy_meta_path,
            fixed_meta_path,
        )
        return False
    except Exception:
        if lock_lost_check is not None and lock_lost_check.is_lost():
            logger.error(
                "temporal legacy migration: write lock was lost DURING "
                "the metadata scope copy %s -> %s -- transaction rolled "
                "back, not counted as a failure",
                legacy_meta_path,
                fixed_meta_path,
            )
            return False
        logger.exception(
            "failed to copy temporal metadata scope %s -> %s",
            legacy_meta_path,
            fixed_meta_path,
        )
        return True


def _delete_metadata_scope_if_safe(
    legacy_meta_path: Path,
    fixed_meta_path: Path,
    metadata_backend_factory: Callable[[Path], TemporalMetadataScopeBackend],
    *,
    cleanup_authorized: bool,
    all_legacy_shards_gone: bool,
    lock_lost_check: Optional[LockLossCheck] = None,
) -> bool:
    """Attempt the metadata-scope deletion. Returns True iff it was
    attempted and failed (Blocker 6). Withheld (returns False, no attempt)
    unless cleanup is authorized, every legacy shard is verified gone, AND
    (Blocker 5, hardened by the round-4 review's Exploit 2 fix) a fresh,
    content-bound comparison proves the fixed-root scope holds the SAME
    rows as the legacy scope -- see ``_metadata_scope_relocation_verified``
    for why neither a same-run copy attempt nor a non-empty destination
    alone is ever treated as sufficient proof.
    """
    if not cleanup_authorized or not all_legacy_shards_gone:
        return False
    if not _metadata_scope_relocation_verified(
        legacy_meta_path, fixed_meta_path, metadata_backend_factory
    ):
        return False
    try:
        # Issue #1548 round-8, Issue 1: check immediately before this
        # destructive metadata deletion -- a lock-loss here is treated
        # exactly like any other failure of this call (caught below,
        # counted as failed, never silently proceeding).
        _abort_if_lock_lost(lock_lost_check)
        metadata_backend_factory(legacy_meta_path).delete_collection_scope()
        logger.info("deleted legacy temporal metadata scope %s", legacy_meta_path)
        return False
    except Exception:
        logger.exception(
            "failed to delete temporal metadata scope %s", legacy_meta_path
        )
        return True


_SHA256_HEX_LENGTH = 64
_SHA256_HEX_ALPHABET = frozenset("0123456789abcdef")


def _looks_like_sha256_hex(value: object) -> bool:
    """Format-level plausibility check for a value claiming to be a
    sha256 hex digest -- exactly 64 lowercase hex characters. Cheap,
    additional defense-in-depth against a forged record whose ``shards``
    dict carries syntactically-invalid "digests" that could never have
    been produced by ``manifest_digest()``.
    """
    return (
        isinstance(value, str)
        and len(value) == _SHA256_HEX_LENGTH
        and all(ch in _SHA256_HEX_ALPHABET for ch in value)
    )


def _is_safe_shard_name(fixed_root: Path, name: object) -> bool:
    """True iff *name* is a plain path component that resolves to a
    direct child of *fixed_root* -- never a path-traversal payload
    (``../..``, an absolute path, embedded separators, or a symlink
    resolving elsewhere) read out of an untrusted JSON record.

    Issue #1548 round-4 exploit fix: ``_repo_relocation_previously_
    completed`` constructs ``fixed_root / name`` from a repo-level record
    an attacker with filesystem access to ``fixed_root`` could otherwise
    forge with an arbitrary string -- without this guard, a crafted name
    (or a symlink placed at that name) could make later path construction
    and filesystem inspection escape ``fixed_root`` entirely. Resolves
    BOTH sides symlink-free (``Path.resolve()``, which follows any
    existing symlink components) and requires the resolved candidate's
    PARENT to equal the resolved ``fixed_root`` -- so even a symlink named
    ``name`` that points elsewhere is rejected, not just a literal ``../``
    string.
    """
    if not isinstance(name, str) or not name:
        return False
    if os.path.basename(name) != name:
        return False
    resolved_root = fixed_root.resolve()
    resolved_candidate = (fixed_root / name).resolve()
    return resolved_candidate.parent == resolved_root


def _read_relocation_record(fixed_root: Path) -> Dict[str, Any]:
    """Read and parse the repo-level relocation record, fail closed.

    Issue #1548 round-4 exploit 2 fix: any absent, unparseable, or
    non-object record is treated as "no record" (an empty dict) --
    never raises, so callers never mistake "cannot read" for "verified
    complete".
    """
    path = fixed_root / _REPO_RELOCATION_COMPLETE_MARKER_NAME
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError, UnicodeDecodeError):
        logger.exception(
            "failed to read/parse repo-level relocation record %s -- "
            "treating as absent (fail closed)",
            path,
        )
        return {}
    return data if isinstance(data, dict) else {}


def _write_relocation_record_atomic(
    fixed_root: Path,
    record: Dict[str, Any],
    lock_lost_check: Optional[LockLossCheck] = None,
) -> bool:
    """Durably persist *record* as JSON: temp-file write, fsync, atomic
    ``os.replace``, then fsync the parent directory.

    Issue #1548 round-4 exploit 2 fix: a bare ``write_text()`` (the
    previous implementation) can leave a partial/corrupt file on a
    mid-write crash. Atomicity here is about crash-safety, not
    forgery-resistance -- forgery-resistance comes from the CONTENT this
    record carries and how ``_repo_relocation_previously_completed``
    cross-validates it against real, currently-present shard data, never
    from the write mechanism alone.

    Issue #1548 round-10, Finding 2: this write is several syscalls
    (mkdir, write, fsync, replace, fsync), not one atomic operation. The
    caller's entry-level lock-loss check (in ``_mark_repo_relocation_
    complete``) only proves the lock was healthy BEFORE this sequence
    started -- a lock lost DURING it could otherwise still complete the
    ``os.replace()`` and overwrite a newer owner's relocation record.
    This function therefore rechecks immediately before that call --
    the true point of no return -- and aborts BEFORE performing the
    replace (cleaning up the temp file exactly like every other failure
    path) if the lock may have been lost. Classified exactly like every
    other lock-loss abort in this module: not a failure, since nothing
    was mutated.

    Issue #1548 round-10, Finding 3: returns ``True`` iff a write was
    attempted and genuinely FAILED via ``OSError`` -- this used to be
    silently swallowed (logged, then returned as if nothing happened),
    letting a caller report an apparently-successful migration despite
    the record never being durably written. ``False`` covers both a
    genuine success and a lock-loss-triggered withholding (a deliberate,
    safe deferral, not a failure).
    """
    tmp: Optional[Path] = None
    try:
        fixed_root.mkdir(parents=True, exist_ok=True)
        target = fixed_root / _REPO_RELOCATION_COMPLETE_MARKER_NAME
        tmp = (
            fixed_root
            / f".{_REPO_RELOCATION_COMPLETE_MARKER_NAME}.tmp-{uuid.uuid4().hex}"
        )
        tmp.write_text(json.dumps(record, sort_keys=True))
        with tmp.open("rb") as stream:
            os.fsync(stream.fileno())
        if lock_lost_check is not None and lock_lost_check.is_lost():
            logger.error(
                "temporal legacy migration: write lock may have been "
                "lost DURING the relocation-record write sequence for "
                "%s -- aborting before the atomic replace, refusing to "
                "write the record",
                fixed_root,
            )
            return False
        os.replace(tmp, target)
        tmp = None
        fd = os.open(fixed_root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return False
    except OSError:
        logger.exception(
            "failed to durably persist repo-level relocation record under %s",
            fixed_root,
        )
        return True
    finally:
        if tmp is not None and tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                # Best-effort cleanup of a temp file left behind by an
                # already-logged primary failure above -- a leaked temp
                # file here is a harmless disk-space nit, not a
                # correctness issue (it is never read by any consumer),
                # so this is intentionally NOT re-raised, only logged.
                logger.warning(
                    "failed to remove leftover temp file %s after a "
                    "relocation-record write failure",
                    tmp,
                )


def _collect_shard_provenance_digests(
    fixed_root: Path, shards: List[Path]
) -> Dict[str, str]:
    """Read each shard's OWN per-shard provenance marker (written only by
    ``_publish()`` after real, independent verification) from its
    fixed-root target -- never recomputed here, since the legacy source
    may already be gone by the time this runs. Shards without a readable,
    plausibly-formatted marker are simply omitted.
    """
    digests: Dict[str, str] = {}
    for shard in shards:
        digest = _read_marker_digest(fixed_root / shard.name / _PROVENANCE_MARKER_NAME)
        if digest is not None and _looks_like_sha256_hex(digest):
            digests[shard.name] = digest
    return digests


def _mark_repo_relocation_complete(
    fixed_root: Path,
    legacy_root: Path,
    shard_digests: Dict[str, str],
    lock_lost_check: Optional[LockLossCheck] = None,
) -> bool:
    """Durably record, as a content-bound JSON record, that THIS pass
    observed a non-empty legacy shard list for this repo and verified
    every one of them gone.

    Issue #1548 round-10, Finding 3: returns ``True`` iff the write was
    attempted and genuinely FAILED (a real ``OSError`` inside
    ``_write_relocation_record_atomic``) -- folded by the caller
    (``migrate_temporal_shards``) into ``MigrationResult.failed``.
    ``False`` covers the empty-manifest refusal, the lock-loss refusals
    (both the entry-level one below and the narrower mid-write one
    inside ``_write_relocation_record_atomic``), and genuine success --
    none of those are failures.

    Issue #1548 round-4 exploit 2 fix: the previous implementation wrote a
    bare sentinel (a timestamp string, no digest, no repo identity, no
    shard manifest) -- Codex reproduced forging this exact file to
    authorize metadata deletion despite zero shards ever actually being
    verified/migrated. The record now carries the repository identity
    (``legacy_root``, checked for an exact match by
    ``_repo_relocation_previously_completed``) and the actual set of shard
    names this pass genuinely relocated, each bound to its own per-shard
    provenance digest (``_collect_shard_provenance_digests`` -- itself
    only populated by ``_publish()``'s independent verification, never by
    this function). Written under ``fixed_root`` (server-owned, outlives
    legacy shard deletion) so a LATER pass that sees zero legacy shard
    directories can distinguish "genuinely fully migrated already" from
    "never had any shards, permanently keep the metadata scope".

    Fails closed: refuses to write (logs an error and returns) if
    *shard_digests* is empty -- this function must only ever be called
    with a genuinely non-empty, provenance-derived manifest.

    Issue #1548 round-9: ALSO refuses to write (logs an error and
    returns, checked via non-raising ``is_lost()`` immediately before the
    durable write) if the write lock may have been lost -- this record's
    existence can later authorize deleting the shared temporal-metadata
    scope (see ``_repo_relocation_previously_completed``), so writing it
    under a lost lock must itself be refused, not merely the deletions it
    later enables.
    """
    if not shard_digests:
        logger.error(
            "refusing to write a repo-level relocation record under %s "
            "with an empty shard-digest manifest -- this indicates a "
            "caller bug, not a legitimate migration state",
            fixed_root,
        )
        return False
    record = _read_relocation_record(fixed_root)
    record["legacy_root"] = str(legacy_root)
    record["shards"] = shard_digests
    record["recorded_at"] = time.time()
    if lock_lost_check is not None and lock_lost_check.is_lost():
        logger.error(
            "temporal legacy migration: write lock may have been lost -- "
            "refusing to write repo-level relocation record under %s",
            fixed_root,
        )
        return False
    return _write_relocation_record_atomic(
        fixed_root, record, lock_lost_check=lock_lost_check
    )


def _repo_relocation_previously_completed(fixed_root: Path, legacy_root: Path) -> bool:
    """True only if the repo-level record: (1) was written for THIS exact
    repository identity (``legacy_root`` matches exactly), (2) carries a
    genuinely non-empty per-shard digest manifest of SAFE shard names
    (``_is_safe_shard_name``, closing a path-traversal escape from
    ``fixed_root``) each bound to a syntactically plausible sha256 hex
    digest, AND (3) every recorded shard still has a REAL, currently-
    complete published copy at the fixed root whose OWN independently
    written per-shard provenance marker (never recomputed here, only
    re-read) matches the recorded digest exactly.

    Issue #1548 round-4 exploit 2 fix: (1)+(2) alone still let a forged
    record claim an arbitrary self-consistent name/digest pair with no
    real shard behind it. (3) closes that: a genuinely migrated shard's
    published copy is NEVER deleted by this mechanism (only its legacy
    source is), so its own provenance marker -- written independently by
    ``_publish()`` at the time it was actually verified -- must still be
    physically present and must still agree with what the repo-level
    record claims, AND the target must still pass the FULL structural-
    completeness gate. Forging the repo-level record alone can no longer
    satisfy this function without ALSO fabricating a complete shard
    directory for every claimed name.

    Issue #1548 round-5 secondary finding 3 fix: (1)-(3) above still only
    ever compared a marker digest against ANOTHER marker digest (the
    per-shard file at ``target``) -- a forged repo-level record whose
    digest happened to match a SEPARATELY forged per-shard marker (neither
    ever derived from the target's real content) still satisfied every
    check. This function now ALSO recomputes ``manifest_digest(target)``
    -- the target's ACTUAL current content -- and requires it to equal
    the recorded digest too, so a marker-to-marker coincidence alone can
    no longer substitute for genuine content-bound proof.

    This function is deliberately NOT the sole authorization gate for
    metadata deletion: ``_metadata_scope_relocation_verified``'s live
    legacy-vs-fixed content-digest comparison is the actual, unforgeable
    proof used for that decision; this function only gates whether a repo
    with zero CURRENTLY-discoverable legacy shards may be treated as
    "previously fully migrated" rather than "never had any shards".
    """
    record = _read_relocation_record(fixed_root)
    if record.get("legacy_root") != str(legacy_root):
        return False
    shards = record.get("shards")
    if not isinstance(shards, dict) or not shards:
        return False
    for name, digest in shards.items():
        if not _is_safe_shard_name(fixed_root, name) or not _looks_like_sha256_hex(
            digest
        ):
            return False
        target = fixed_root / name
        current_marker_digest = _read_marker_digest(target / _PROVENANCE_MARKER_NAME)
        if current_marker_digest != digest:
            return False
        if not _target_is_structurally_complete(target):
            return False
        try:
            recomputed_digest = manifest_digest(target)
        except Exception:
            logger.exception(
                "failed to recompute manifest digest for %s while validating "
                "repo-level relocation record -- refusing to trust it",
                target,
            )
            return False
        if recomputed_digest != digest:
            logger.warning(
                "repo-level relocation record for %s claims digest %s but "
                "the target's ACTUAL current content digest is %s -- "
                "refusing to trust the record (marker-to-marker match alone "
                "is not proof of provenance)",
                target,
                digest,
                recomputed_digest,
            )
            return False
    return True


def _sync_metadata_scope(
    legacy_root: Path,
    fixed_root: Path,
    metadata_backend_factory: Optional[Callable[[Path], TemporalMetadataScopeBackend]],
    *,
    relocation_enabled: bool,
    cleanup_authorized: bool,
    all_legacy_shards_gone: bool,
    withhold_copy: bool,
    lock_lost_check: Optional[LockLossCheck] = None,
) -> bool:
    """Copy/delete the repo-level shared temporal-metadata bookkeeping scope.

    Blocker 3: the temporal metadata store lives at the SHARED
    bookkeeping-collection path (``LEGACY_TEMPORAL_COLLECTION``, one store
    per repo index root shared across every quarter/embedder shard) -- NOT
    at each shard's own path. This must run ONCE per repo, never once per
    shard.

    Returns True iff a copy or delete operation was attempted and failed
    (Blocker 6) -- the caller folds this into ``MigrationResult.failed``.
    """
    if metadata_backend_factory is None:
        return False
    legacy_meta_path = legacy_root / LEGACY_TEMPORAL_COLLECTION
    if not legacy_meta_path.is_dir():
        return False
    fixed_meta_path = fixed_root / LEGACY_TEMPORAL_COLLECTION

    copy_failed = _copy_metadata_scope_if_safe(
        legacy_meta_path,
        fixed_meta_path,
        metadata_backend_factory,
        relocation_enabled=relocation_enabled,
        withhold=withhold_copy,
        lock_lost_check=lock_lost_check,
    )
    if copy_failed:
        return True

    return _delete_metadata_scope_if_safe(
        legacy_meta_path,
        fixed_meta_path,
        metadata_backend_factory,
        cleanup_authorized=cleanup_authorized,
        all_legacy_shards_gone=all_legacy_shards_gone,
        lock_lost_check=lock_lost_check,
    )


def _discover_shards(legacy_root: Path) -> List[Path]:
    return sorted(
        path
        for path in legacy_root.iterdir()
        if path.name.startswith(_SHARD_PREFIX) and path.is_dir()
    )


def _target_digest_still_matches(target: Path, expected_digest: str) -> bool:
    """True iff *target*'s CURRENT ``manifest_digest`` equals
    *expected_digest* -- a snapshot the caller captured earlier, while
    *target* was still trusted (immediately after its own verification
    succeeded). Never raises: any recomputation failure (missing files,
    a symlink, a corrupt tree) is treated as "does not match" (fail
    closed), since this gates a destructive decision.
    """
    try:
        return manifest_digest(target) == expected_digest
    except Exception:
        logger.exception(
            "failed to recompute manifest digest for %s -- treating as "
            "not matching the expected pre-rename snapshot (fail closed)",
            target,
        )
        return False


def _delete_source_atomically(
    source: Path,
    target: Path,
    expected_target_digest: str,
    post_rename_hook: Optional[Callable[[], None]] = None,
    lock_lost_check: Optional[LockLossCheck] = None,
) -> None:
    """Atomically rename *source* to a private trash path, re-verify
    *target* once more, then delete the trash -- or, on re-verification
    failure, RESTORE *source* from trash and raise (never left ambiguous).

    Round-5 exploit 2 fix (TOCTOU): a single atomic ``os.rename`` to an
    unpredictable uuid4-suffixed path (same filesystem, one syscall)
    replaces a direct ``shutil.rmtree(source)`` over a well-known path.

    Round-6 exploit 2 fix (CRITICAL): round-5 protected only the SOURCE --
    a concurrent TARGET mutation (e.g. ``hnsw_index.bin`` zeroed) in the
    same window went uncaught. Immediately after the rename, with no other
    I/O in between, *target* is re-verified complete and *trash_path*
    re-checked for symlinks. Either failing renames *source* BACK from
    trash and raises -- a genuine recovery path, not merely a loud
    failure -- so the caller's outer handling counts this as a failed
    migration attempt, never a partial state. ``post_rename_hook`` is a
    test-only seam firing in exactly this window.

    Round-7 exploit fix (CRITICAL): structural completeness alone proves
    the target LOOKS like a real shard, not that it still holds the SAME
    data it held at verification time. Codex reproduced swapping the
    target for a DIFFERENT, fully valid, structurally-complete shard in
    this exact window, which the round-6 check alone accepted. ``target``
    is now ALSO re-verified against *expected_target_digest* -- a
    ``manifest_digest(target)`` snapshot the caller captured immediately
    before the rename, while the target was still trusted -- via
    ``_target_digest_still_matches``. Deliberately NOT a comparison
    against the just-trashed source: the source is legitimately allowed
    to be mutated after its own verification already succeeded (a
    documented, intentional no-op for deletion purposes -- see
    ``_verify_and_delete_source``'s round-6 fix), so comparing against it
    here would wrongly reject that harmless case. Any digest divergence
    is treated exactly like a structural failure -- restore and raise,
    never delete.
    """
    trash_path = (
        source.parent / f".{source.name}{_PENDING_DELETE_INFIX}{uuid.uuid4().hex}"
    )
    # Issue #1548 round-8, Issue 1: check immediately before the
    # rename-to-trash -- the first of this function's two destructive
    # steps.
    _abort_if_lock_lost(lock_lost_check)
    os.rename(source, trash_path)
    if post_rename_hook is not None:
        post_rename_hook()
    target_unsafe = (
        _contains_symlink(trash_path)
        or not _target_is_structurally_complete(target)
        or not _target_digest_still_matches(target, expected_target_digest)
    )
    if target_unsafe:
        _restore_source_from_trash(trash_path, source)
        raise VerificationError(
            f"refusing to delete legacy temporal shard {source}: target "
            f"{target} was found corrupted/incomplete, its content no "
            f"longer matches its pre-deletion verified snapshot, or a "
            f"symlink appeared in the trashed source immediately after "
            f"the atomic rename to trash -- source restored to its "
            f"original location, treating this migration attempt as failed"
        )
    # Issue #1548 round-8, Issue 1: check again immediately before the
    # final destructive delete of the trashed source.
    _abort_if_lock_lost(lock_lost_check)
    shutil.rmtree(trash_path)


def _restore_source_from_trash(trash_path: Path, source: Path) -> None:
    """Restore *source* from *trash_path*, durably.

    Issue #1548 round-7 hardening: explicitly verifies *source* is vacant
    before the restore rename -- an ``os.rename`` onto an occupied,
    non-empty directory already fails loudly with ``OSError``, but this
    makes the check explicit and gives an actionable error instead of a
    raw errno -- and fsyncs the parent directory after a successful
    restore, since an atomic rename is not a DURABLE one on its own
    (matching this module's ``_publish``/``_write_relocation_record_atomic``
    fsync-after-rename convention).
    """
    if os.path.lexists(source):
        # lexists (not exists) so a dangling symlink at *source* -- which
        # exists() would report as False -- is still correctly detected
        # as an occupant, never silently replaced.
        raise VerificationError(
            f"cannot restore legacy temporal shard from trash: {source} "
            f"is unexpectedly occupied -- trashed data left at {trash_path} "
            f"pending manual recovery"
        )
    os.rename(trash_path, source)
    fd = os.open(source.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _original_shard_name_from_trash(entry_name: str) -> Optional[str]:
    """Recover the original shard directory name from a trash entry name
    of the form ``.{name}{_PENDING_DELETE_INFIX}{uuid}``, or ``None`` if
    *entry_name* does not match that shape.
    """
    if not entry_name.startswith("."):
        return None
    without_dot = entry_name[1:]
    idx = without_dot.find(_PENDING_DELETE_INFIX)
    if idx <= 0:
        return None
    return without_dot[:idx]


def _trash_dir_is_safe_to_discard(entry: Path, fixed_root: Path) -> bool:
    """True iff *entry* -- a leftover trash directory -- can be positively
    confirmed safe to discard: its corresponding fixed-root target is
    independently re-verified structurally complete via the SAME
    completeness gate that authorized deletion in the first place.

    Issue #1548 round-6 finding fix: the previous implementation deleted
    every orphaned trash directory unconditionally, discarding the ONE
    surviving copy of a shard's legacy data if anything about its
    corresponding migration attempt went wrong (e.g. exploit 2's failure
    path, or any other crash before the shard was ever confirmed safely
    migrated). Never assumes "leftover = safe" -- an unparseable name or
    a target that fails re-verification is left in place.

    Issue #1548 round-7 exploit fix (CRITICAL): structural completeness
    alone proved only that SOME real, complete shard sits at the target
    path -- not that it is the SAME shard as *entry*'s own trashed data.
    Codex reproduced replacing the target with a different, fully valid,
    structurally-complete shard, which the round-6 check alone accepted
    as grounds to discard the genuinely-surviving trash. Discarding now
    ALSO requires ``_content_matches_expected_source(entry, target)`` --
    a fresh, independent, field-for-field comparison between the trash
    and the target -- so a content-divergent target can never authorize
    discarding this trash directory.
    """
    name = _original_shard_name_from_trash(entry.name)
    if name is None:
        return False
    target = fixed_root / name
    try:
        return _target_is_structurally_complete(
            target
        ) and _content_matches_expected_source(entry, target)
    except Exception:
        logger.exception(
            "failed to verify %s while deciding whether orphaned trash %s "
            "is safe to discard -- treating as unsafe (fail closed)",
            target,
            entry,
        )
        return False


def _cleanup_orphaned_trash_dirs(
    legacy_root: Path,
    fixed_root: Path,
    lock_lost_check: Optional[LockLossCheck] = None,
) -> int:
    """Remove trash directories orphaned by a crash between the atomic
    rename in ``_delete_source_atomically`` and its subsequent
    ``shutil.rmtree`` -- but ONLY those positively confirmed safe to
    discard (``_trash_dir_is_safe_to_discard``). An orphaned trash
    directory whose corresponding target cannot be reconfirmed complete
    is left in place with a WARNING rather than blindly removed.

    Issue #1548 round-9: checked (non-raising, via ``is_lost()``) after
    the safety check but immediately before EACH individual
    ``shutil.rmtree`` -- the same completeness-gap fix applied to
    ``_cleanup_orphaned_staging_dirs``. An entry left in place this way is
    NOT counted as a failure.
    """
    if not legacy_root.is_dir():
        return 0
    try:
        entries = list(legacy_root.iterdir())
    except OSError:
        logger.exception(
            "failed to list %s while sweeping orphaned trash directories",
            legacy_root,
        )
        return 1
    failures = 0
    for entry in entries:
        if not entry.is_dir():
            continue
        if not entry.name.startswith(".") or _PENDING_DELETE_INFIX not in entry.name:
            continue
        if not _trash_dir_is_safe_to_discard(entry, fixed_root):
            logger.warning(
                "leaving orphaned migration trash directory in place -- "
                "could not positively confirm its corresponding target is "
                "safely migrated: %s",
                entry,
            )
            continue
        if lock_lost_check is not None and lock_lost_check.is_lost():
            logger.error(
                "temporal legacy migration: write lock may have been "
                "lost -- leaving orphaned trash directory %s (and any "
                "remaining ones) in place rather than deleting it",
                entry,
            )
            break
        logger.warning("removing orphaned migration trash directory: %s", entry)
        try:
            shutil.rmtree(entry)
        except OSError:
            logger.exception(
                "failed to remove orphaned migration trash directory: %s", entry
            )
            failures += 1
    return failures


def migrate_temporal_shards(
    legacy_root: Path,
    fixed_root: Path,
    *,
    relocation_enabled: bool = False,
    cleanup_authorized: bool = False,
    metadata_backend_factory: Optional[
        Callable[[Path], TemporalMetadataScopeBackend]
    ] = None,
    pre_publish_hook: Optional[Callable[[], None]] = None,
    pre_delete_hook: Optional[Callable[[], None]] = None,
    post_rename_hook: Optional[Callable[[], None]] = None,
    lock_lost_check: Optional[LockLossCheck] = None,
) -> MigrationResult:
    """Relocate every legacy shard for one repo without destroying data
    that has not been positively verified as safely migrated.

    Args:
        legacy_root: The repo's in-clone ``.code-indexer/index`` directory.
        fixed_root: The repo's fixed server-owned temporal root (Bug #1529).
        relocation_enabled: Copy/publish gate (Web UI config flag).
        cleanup_authorized: Destructive legacy-deletion gate (Web UI config
            flag), independent of ``relocation_enabled``.
        metadata_backend_factory: Optional factory for the shared temporal
            metadata bookkeeping scope (see ``_sync_metadata_scope``).
        pre_publish_hook: Test-only seam fired after staging verification,
            before the atomic rename (Blocker 7).
        pre_delete_hook: Test-only seam fired immediately before the final
            post-verify symlink recheck and destructive delete (Issue
            #1548 round-5 exploit 2 -- see ``_verify_and_delete_source``).
        post_rename_hook: Test-only seam fired immediately after the
            source is renamed to its private trash path, before the final
            target re-verification (Issue #1548 round-6 exploit 2 -- see
            ``_delete_source_atomically``).
        lock_lost_check: Issue #1548 round-8 fix (Issue 1) -- checked
            immediately before every destructive operation in this
            module (via ``_abort_if_lock_lost``); when it reports the
            caller's write-lock heartbeat has failed, the CURRENT and any
            SUBSEQUENT shard/metadata destructive step in this pass is
            aborted rather than proceeding without exclusive ownership of
            the repo. Production passes
            ``locking.guarded_by_refresh_lock``'s yielded
            ``LockLossSignal``; `None` (the default, e.g. tests
            exercising this module directly) disables the check entirely.

    Per-shard failures are isolated (Blocker 8). Caller MUST hold the
    repo's write lock (``locking.guarded_by_refresh_lock``) -- this
    function performs no locking of its own.
    """
    if not legacy_root.is_dir():
        return MigrationResult()

    staging_sweep_failures = _cleanup_orphaned_staging_dirs(
        fixed_root, lock_lost_check=lock_lost_check
    )
    trash_sweep_failures = _cleanup_orphaned_trash_dirs(
        legacy_root, fixed_root, lock_lost_check=lock_lost_check
    )
    shards = _discover_shards(legacy_root)
    counts = _run_shard_pass(
        shards,
        fixed_root,
        relocation_enabled=relocation_enabled,
        cleanup_authorized=cleanup_authorized,
        pre_publish_hook=pre_publish_hook,
        pre_delete_hook=pre_delete_hook,
        post_rename_hook=post_rename_hook,
        lock_lost_check=lock_lost_check,
    )
    counts["failed"] += staging_sweep_failures + trash_sweep_failures

    # Issue #1548 round-9: a structural "don't even attempt the next
    # phase" check -- once the shard pass above has observed lock loss
    # (whether it was already lost at entry, or was lost partway through
    # this pass), the caller must NEVER proceed into the metadata-scope
    # sync phase (relocation-record write + copy/delete) at all. This is
    # deliberately IN ADDITION to (not a replacement for) the individual
    # per-operation guards inside ``_mark_repo_relocation_complete`` and
    # ``_sync_metadata_scope`` below -- this check closes the case where
    # the lock is lost strictly BETWEEN the shard pass finishing and this
    # point, which no per-operation guard downstream can retroactively
    # cover for the DECISION to enter the phase at all.
    if lock_lost_check is not None and lock_lost_check.is_lost():
        logger.error(
            "temporal legacy migration: write lock may have been lost -- "
            "refusing to proceed into the metadata-scope sync phase for %s",
            legacy_root,
        )
        return MigrationResult(
            published=counts["published"],
            already_complete=counts["already_complete"],
            deleted=counts["deleted"],
            collisions=counts["collision"],
            failed=counts["failed"],
        )

    # Blocker 5: an empty shard list must never vacuously satisfy "all
    # legacy shards gone" -- a repo with literally zero temporal shards has
    # nothing to have relocated, so its metadata scope (if any) must never
    # be treated as "cleanup complete" on that basis alone. Review-round-3
    # blocker 4: the ONE legitimate exception is a repo whose shards WERE
    # discovered and fully relocated in an earlier pass (durably recorded
    # via ``_mark_repo_relocation_complete``) and have since disappeared
    # from ``legacy_root`` entirely -- that repo's metadata cleanup must
    # still be able to converge on a later pass that sees zero shards.
    non_vacuous_all_gone = bool(shards) and all(not shard.exists() for shard in shards)
    if non_vacuous_all_gone:
        shard_digests = _collect_shard_provenance_digests(fixed_root, shards)
        # Issue #1548 round-10, Finding 3: a real OSError while durably
        # persisting this record must be counted as a failure -- an
        # operator watching MigrationResult.failed must be able to see
        # it, not a clean-looking zero despite the record never actually
        # being written.
        relocation_record_failed = _mark_repo_relocation_complete(
            fixed_root, legacy_root, shard_digests, lock_lost_check=lock_lost_check
        )
        if relocation_record_failed:
            counts["failed"] += 1
    all_legacy_shards_gone = non_vacuous_all_gone or (
        not shards and _repo_relocation_previously_completed(fixed_root, legacy_root)
    )
    metadata_failed = _sync_metadata_scope(
        legacy_root,
        fixed_root,
        metadata_backend_factory,
        relocation_enabled=relocation_enabled,
        cleanup_authorized=cleanup_authorized,
        all_legacy_shards_gone=all_legacy_shards_gone,
        withhold_copy=counts["collision"] > 0 or counts["failed"] > 0,
        lock_lost_check=lock_lost_check,
    )
    if metadata_failed:
        counts["failed"] += 1

    return MigrationResult(
        published=counts["published"],
        already_complete=counts["already_complete"],
        deleted=counts["deleted"],
        collisions=counts["collision"],
        failed=counts["failed"],
    )
