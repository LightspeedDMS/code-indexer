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
    manifest_digest,
    peek_one_vector_dimension,
    verify_shard_copy,
)

logger = logging.getLogger(__name__)

_SHARD_PREFIX = "code-indexer-temporal-"
_STAGING_INFIX = ".staging-"

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


class TemporalMetadataScopeBackend(Protocol):
    """Minimal surface this module needs from a temporal metadata backend.

    Both `TemporalMetadataSqliteBackend` and `TemporalMetadataPostgresBackend`
    satisfy this structurally -- typed here (rather than importing either
    concrete class, which would pull psycopg/fastapi into this module) so
    ``metadata_backend_factory``'s return value gets real type checking
    instead of a bare ``object`` + ``# type: ignore``.
    """

    def copy_collection_scope(self, target_collection_path: Path) -> None: ...

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
    """Real, functional validation of ``hnsw_index.bin`` -- not just
    "the file exists" or "a digest matched".

    Issue #1548 round-4 exploit fix: Codex reproduced a target with a
    zero-byte ``hnsw_index.bin`` whose digest still matched (since both
    sides were identically empty) -- the old existence-only check passed.
    This function requires the file to be non-empty AND to actually
    ``hnswlib``-load as a real index containing at least one vector, using
    a dimension read straight from the shard's own committed data
    (``peek_one_vector_dimension``) rather than any external configuration.
    Any failure (missing dimension, load error, empty index, or a failure
    while querying the loaded index) returns False -- never raises, since
    this is a safety-gated completeness check, not a hard dependency on
    hnswlib being installed correctly.
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
        logger.exception(
            "failed to peek a vector dimension for hnsw structural validation at %s",
            target,
        )
        return False
    if not dim:
        return False
    try:
        # Lazy import (Bug #1468 pattern): hnswlib is a heavy, optional-at-
        # import-time dependency this module should not force onto every
        # caller that merely imports mover.py.
        from code_indexer.storage.hnsw_index_manager import HNSWIndexManager

        manager = HNSWIndexManager(vector_dim=dim, space="cosine")
        index = manager.load_index(target)
        return index is not None and index.get_current_count() > 0
    except Exception:
        logger.exception(
            "hnsw_index.bin at %s failed to load as a structurally valid index",
            target,
        )
        return False


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


def _read_marker_digest(marker_path: Path) -> Optional[str]:
    if not marker_path.is_file():
        return None
    try:
        return marker_path.read_text().strip()
    except OSError:
        logger.exception("failed to read provenance marker %s", marker_path)
        return None


def _cleanup_orphaned_staging_dirs(target_parent: Path) -> int:
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
        if staging.exists():
            shutil.rmtree(staging)


def _classify_existing_target(source: Path, target: Path) -> str:
    """Return "already_complete" or "collision" for a target that already
    holds verified data (Blocker 1's "new shard wins" policy).

    "already_complete" requires BOTH:
      1. Content-bound digest proof: a provenance marker at *target* whose
         recorded digest equals BOTH the current legacy source's manifest
         digest AND the target's own current manifest digest -- i.e.
         neither side has changed since a verified publish by THIS
         migration mechanism, and (since ``manifest_digest`` now covers
         the full file tree, not just logical records) the target
         genuinely mirrors the source's complete directory structure.
      2. A direct, independent completeness proof
         (``_target_is_structurally_complete``) -- the target must be a
         real, queryable shard on its own terms, never trusted purely
         because a digest comparison happened to agree.

    Anything short of both (no marker, a stale marker, content that has
    since diverged on either side, or a target that is not itself
    structurally complete) is a collision, even when point-record content
    happens to match byte-for-byte -- a coincidental match is not proof of
    provenance.
    """
    # Issue #1548 round-4 exploit fix: check BOTH trees for symlinks before
    # ever trusting a digest comparison -- a symlinked file resolves
    # THROUGH to its target's bytes for hashing purposes, so a digest match
    # alone proves nothing about whether the target genuinely holds its
    # own independent data. Checked first and unconditionally, so this can
    # never be bypassed by a marker/digest coincidence on either side.
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
    verified_prior_publish = (
        marker_digest is not None
        and marker_digest == manifest_digest(source)
        and marker_digest == manifest_digest(target)
        and _target_is_structurally_complete(target)
    )
    if verified_prior_publish:
        return "already_complete"
    logger.warning(
        "temporal shard collision: fixed root %s already holds data that "
        "is not verifiably a prior publish of this migration mechanism "
        "(marker_digest=%s, structurally_complete=%s) -- destination "
        "wins, both sides left untouched pending a later, separate "
        "cleanup pass",
        target,
        marker_digest,
        _target_is_structurally_complete(target),
    )
    return "collision"


def _process_one_shard(
    source: Path,
    target: Path,
    *,
    relocation_enabled: bool,
    cleanup_authorized: bool,
    pre_publish_hook: Optional[Callable[[], None]],
) -> str:
    """Migrate/verify/cleanup one shard. Returns one of: "published",
    "already_complete", "collision", "skipped", "deleted" (deleted implies
    one of the first two also happened, tracked by the caller).
    """
    if _has_verified_data(target):
        outcome = _classify_existing_target(source, target)
        if outcome == "collision":
            return "collision"
    elif relocation_enabled:
        _publish(source, target, pre_publish_hook)
        outcome = "published"
    else:
        return "skipped"

    if cleanup_authorized:
        # Issue #1548 medium-priority item 1 (round-4 review): a defense-
        # in-depth re-check, immediately before the irreversible delete,
        # inside the SAME lock scope the caller already holds -- belt and
        # suspenders on top of the outer write-lock, which should already
        # prevent concurrent mutation between the classification above and
        # this point. Never trust that the symlink-free state observed a
        # moment ago (in ``_has_verified_data``/``_classify_existing_target``
        # or the freshly completed ``_publish``) still holds.
        if _contains_symlink(source) or _contains_symlink(target):
            raise VerificationError(
                f"refusing to delete legacy temporal shard {source}: a "
                f"symlink was detected in {source} or {target} "
                f"immediately before deletion"
            )
        # Blocker 1: re-verify field-for-field equivalence immediately
        # before destroying the legacy copy -- never trust the branch
        # above alone.
        verify_shard_copy(source, target)
        shutil.rmtree(source)
        logger.info("deleted verified legacy temporal shard %s", source)
    return outcome


def _run_shard_pass(
    shards: List[Path],
    fixed_root: Path,
    *,
    relocation_enabled: bool,
    cleanup_authorized: bool,
    pre_publish_hook: Optional[Callable[[], None]],
) -> Dict[str, int]:
    """Process every shard, isolating per-shard failures (Blocker 8)."""
    counts = {
        "published": 0,
        "already_complete": 0,
        "deleted": 0,
        "collision": 0,
        "failed": 0,
    }
    for source in shards:
        target = fixed_root / source.name
        try:
            outcome = _process_one_shard(
                source,
                target,
                relocation_enabled=relocation_enabled,
                cleanup_authorized=cleanup_authorized,
                pre_publish_hook=pre_publish_hook,
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


def _copy_metadata_scope_if_safe(
    legacy_meta_path: Path,
    fixed_meta_path: Path,
    metadata_backend_factory: Callable[[Path], TemporalMetadataScopeBackend],
    *,
    relocation_enabled: bool,
    withhold: bool,
) -> bool:
    """Attempt the metadata-scope copy. Returns True iff it was attempted
    and failed (Blocker 6). Skips entirely (returns False, no attempt) when
    relocation is disabled or ``withhold`` is True.

    Review-round-3 blocker 2: ``withhold`` must be True whenever this pass
    detected EITHER a shard collision OR a shard FAILURE (not collision
    alone) -- a failed shard copy is just as untrustworthy a signal as a
    collision: proceeding to copy the shared metadata scope in either case
    would let ``INSERT OR REPLACE`` silently overwrite rows belonging to
    whatever independently produced the collision, or rows this pass never
    actually got to verify because the shard copy itself blew up.
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
    try:
        metadata_backend_factory(legacy_meta_path).copy_collection_scope(
            fixed_meta_path
        )
        logger.info(
            "copied temporal metadata scope %s -> %s",
            legacy_meta_path,
            fixed_meta_path,
        )
        return False
    except Exception:
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


def _write_relocation_record_atomic(fixed_root: Path, record: Dict[str, Any]) -> None:
    """Durably persist *record* as JSON: temp-file write, fsync, atomic
    ``os.replace``, then fsync the parent directory.

    Issue #1548 round-4 exploit 2 fix: a bare ``write_text()`` (the
    previous implementation) can leave a partial/corrupt file on a
    mid-write crash. Atomicity here is about crash-safety, not
    forgery-resistance -- forgery-resistance comes from the CONTENT this
    record carries and how ``_repo_relocation_previously_completed``
    cross-validates it against real, currently-present shard data, never
    from the write mechanism alone.
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
        os.replace(tmp, target)
        tmp = None
        fd = os.open(fixed_root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        logger.exception(
            "failed to durably persist repo-level relocation record under %s",
            fixed_root,
        )
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
    fixed_root: Path, legacy_root: Path, shard_digests: Dict[str, str]
) -> None:
    """Durably record, as a content-bound JSON record, that THIS pass
    observed a non-empty legacy shard list for this repo and verified
    every one of them gone.

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
    """
    if not shard_digests:
        logger.error(
            "refusing to write a repo-level relocation record under %s "
            "with an empty shard-digest manifest -- this indicates a "
            "caller bug, not a legitimate migration state",
            fixed_root,
        )
        return
    record = _read_relocation_record(fixed_root)
    record["legacy_root"] = str(legacy_root)
    record["shards"] = shard_digests
    record["recorded_at"] = time.time()
    _write_relocation_record_atomic(fixed_root, record)


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
    )
    if copy_failed:
        return True

    return _delete_metadata_scope_if_safe(
        legacy_meta_path,
        fixed_meta_path,
        metadata_backend_factory,
        cleanup_authorized=cleanup_authorized,
        all_legacy_shards_gone=all_legacy_shards_gone,
    )


def _discover_shards(legacy_root: Path) -> List[Path]:
    return sorted(
        path
        for path in legacy_root.iterdir()
        if path.name.startswith(_SHARD_PREFIX) and path.is_dir()
    )


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
        pre_publish_hook: Optional callable invoked after staging verification
            but before the atomic rename -- test-only seam for crash/restart
            tests (replaces the removed env-var busy-wait, Blocker 7).

    Per-shard failures are isolated: one bad shard is logged and counted,
    never aborting the rest of the pass (Blocker 8).

    Note: this function performs filesystem mutation with no locking of
    its own -- every caller (scheduler, CLI) MUST wrap it in the repo's
    refresh-safe write lock (``locking.guarded_by_refresh_lock``) so it is
    never invoked concurrently with a live refresh writing the same
    fixed-root shard in place.
    """
    if not legacy_root.is_dir():
        return MigrationResult()

    staging_sweep_failures = _cleanup_orphaned_staging_dirs(fixed_root)
    shards = _discover_shards(legacy_root)
    counts = _run_shard_pass(
        shards,
        fixed_root,
        relocation_enabled=relocation_enabled,
        cleanup_authorized=cleanup_authorized,
        pre_publish_hook=pre_publish_hook,
    )
    counts["failed"] += staging_sweep_failures

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
        _mark_repo_relocation_complete(fixed_root, legacy_root, shard_digests)
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
